# glue_jobs/batch/gold_incident_summary.py
"""
Glue Batch Job: Gold api_error_stats → Gold incident_summary
每天运行（凌晨），识别过去 24 小时内的故障事件，生成自然语言摘要。
输出 JSON Lines 格式，供 vector_indexer Lambda 向量化后写入 S3 Vectors RAG 知识库。

识别规则：同一 service_name + error_code 连续 ≥ 2 小时 error_rate > 5%，
          视为一次 incident。
"""

import json
import sys
from datetime import datetime, timedelta, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    col, collect_list, lit, max as spark_max,
    min as spark_min, round as spark_round, sum as spark_sum,
    count,
)

from lib.lineage import write_lineage_event
from lib.iceberg_utils import configure_iceberg

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "GOLD_BUCKET",
    "GLUE_DATABASE_GOLD", "LINEAGE_TABLE", "ENVIRONMENT",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

configure_iceberg(spark, args["GOLD_BUCKET"])

now_utc    = datetime.now(timezone.utc)
day_end    = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
day_start  = day_end - timedelta(days=1)

INCIDENT_ERROR_RATE_THRESHOLD = 0.05   # 5% 才算故障
INCIDENT_MIN_HOURS = 2                  # 至少持续 2 小时

# ─── 读取昨天的 api_error_stats ───
stats_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_GOLD']}.api_error_stats"
).filter(
    (col("stat_hour") >= lit(day_start.isoformat())) &
    (col("stat_hour") <  lit(day_end.isoformat())) &
    (col("error_rate") >= lit(INCIDENT_ERROR_RATE_THRESHOLD))
)

# ─── 按 service + error_code 聚合：只保留持续 ≥ 2 小时的 ───
incident_candidates = stats_df.groupBy(
    col("service_name"),
    col("error_code"),
).agg(
    count("stat_hour").alias("affected_hours"),
    spark_min("stat_hour").alias("start_time"),
    spark_max("stat_hour").alias("end_time"),
    spark_max("error_rate").alias("peak_error_rate"),
    spark_sum("unique_users").alias("total_affected_users"),
    spark_max("p99_duration_ms").alias("peak_p99_ms"),
    collect_list("sample_trace_ids").alias("all_trace_samples"),
).filter(
    col("affected_hours") >= INCIDENT_MIN_HOURS
)

rows = incident_candidates.collect()
print(f"Incidents identified: {len(rows)}")

# ─── 生成自然语言摘要，写入 Gold incident_summary（JSON Lines）───
incident_records = []
for row in rows:
    incident_id = (
        f"INC-{day_start.strftime('%Y-%m-%d')}"
        f"-{row['service_name']}-{row['error_code']}"
    ).replace("/", "-")

    # 展平 trace samples（list of lists → list）
    flat_traces = [t for sublist in (row["all_trace_samples"] or []) for t in (sublist or [])]

    severity = "P0" if row["peak_error_rate"] >= 0.20 else \
               "P1" if row["peak_error_rate"] >= 0.05 else "P2"

    record = {
        "incident_id":         incident_id,
        "title":               f"{row['service_name']} {row['error_code']} 故障",
        "service_name":        row["service_name"],
        "error_codes":         json.dumps([row["error_code"]]),
        "severity":            severity,
        "start_time":          str(row["start_time"]),
        "end_time":            str(row["end_time"]),
        "peak_error_rate":     round(row["peak_error_rate"], 4),
        "total_affected_users": int(row["total_affected_users"] or 0),
        "peak_p99_ms":         round(row["peak_p99_ms"] or 0, 1),
        "symptoms": (
            f"{row['service_name']} 在 {row['start_time']} ~ {row['end_time']} 出现 "
            f"{row['error_code']} 错误，错误率峰值 {row['peak_error_rate']:.1%}，"
            f"P99 响应时间 {row['peak_p99_ms']:.0f}ms，"
            f"累计影响 {row['total_affected_users']} 名用户。"
        ),
        "root_cause":   "待人工补充或历史经验推断",
        "resolution":   "待人工补充",
        "resolved_at":  str(day_end),
        "sample_traces": json.dumps(flat_traces[:5]),
        "stat_date":    day_start.strftime("%Y-%m-%d"),
        "environment":  args["ENVIRONMENT"],
    }
    incident_records.append(record)

if incident_records:
    incidents_rdd = spark.sparkContext.parallelize(incident_records)
    incidents_df = spark.read.json(incidents_rdd)
    incidents_df.writeTo(
        f"glue_catalog.{args['GLUE_DATABASE_GOLD']}.incident_summary"
    ).using("iceberg") \
     .partitionedBy("stat_date") \
     .tableProperty("write.parquet.compression-codec", "snappy") \
     .overwritePartitions()

    print(f"incident_summary written: {len(incident_records)} rows")
else:
    print("No incidents detected for this period.")

write_lineage_event(
    source_table=f"s3://iodp-gold-{args['ENVIRONMENT']}/api_error_stats/",
    target_table=f"s3://iodp-gold-{args['ENVIRONMENT']}/incident_summary/",
    transformation="INCIDENT_DETECTION + SUMMARY_GENERATION",
    job_name=args["JOB_NAME"],
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    record_count_in=stats_df.count(),
    record_count_out=len(incident_records),
    record_count_dead_letter=0,
    lineage_table=args["LINEAGE_TABLE"],
)

job.commit()
