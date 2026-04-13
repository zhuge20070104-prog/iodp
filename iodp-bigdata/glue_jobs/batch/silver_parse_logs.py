# glue_jobs/batch/silver_parse_logs.py
"""
Glue Batch Job: Bronze app_logs → Silver parsed_logs
每小时运行，对上一小时的 Bronze 数据做：
  1. 去重（log_id 唯一）
  2. 类型校正（event_timestamp 为 TIMESTAMP，req_duration_ms 为 DOUBLE）
  3. 写入 Silver Iceberg 表，按 service_name + log_level 分区
"""

import sys
from datetime import datetime, timedelta, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    col, current_timestamp, lit, row_number, to_timestamp,
)
from pyspark.sql.window import Window

from lib.lineage import write_lineage_event
from lib.iceberg_utils import configure_iceberg, iceberg_merge_dedup

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "BRONZE_BUCKET", "SILVER_BUCKET",
    "GLUE_DATABASE_BRONZE", "GLUE_DATABASE_SILVER",
    "LINEAGE_TABLE", "ENVIRONMENT",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

configure_iceberg(spark, args["SILVER_BUCKET"])

now_utc = datetime.now(timezone.utc)
hour_end   = now_utc.replace(minute=0, second=0, microsecond=0)
hour_start = hour_end - timedelta(hours=1)

print(f"Processing window: {hour_start} ~ {hour_end}")

# ─── 1. 读取 Bronze 上一小时数据 ───
bronze_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_BRONZE']}.app_logs"
).filter(
    (col("ingest_timestamp") >= lit(hour_start.isoformat())) &
    (col("ingest_timestamp") <  lit(hour_end.isoformat()))
)

input_count = bronze_df.count()
print(f"Bronze records read: {input_count}")

# ─── 2. 去重：同一 log_id 保留 ingest_timestamp 最早的那条 ───
window = Window.partitionBy("log_id").orderBy(col("ingest_timestamp").asc())
deduped_df = bronze_df \
    .withColumn("_rn", row_number().over(window)) \
    .filter(col("_rn") == 1) \
    .drop("_rn")

# ─── 3. 类型标准化（Bronze 已做 to_timestamp，这里补充其他类型校正）───
silver_df = deduped_df \
    .withColumn("req_duration_ms", col("req_duration_ms").cast("double")) \
    .withColumn("http_status",     col("http_status").cast("integer")) \
    .withColumn("processing_timestamp", current_timestamp()) \
    .withColumn("event_date", col("event_timestamp").cast("date"))

# ─── 4. 写入 Silver Iceberg（MERGE 去重，幂等重跑）───
silver_df.createOrReplaceTempView("silver_source")
iceberg_merge_dedup(
    spark=spark,
    source_view="silver_source",
    target_table=f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.parsed_logs",
    merge_keys=["log_id"],
)

output_count = silver_df.count()
print(f"Silver records written: {output_count}")

# ─── 5. 血缘记录 ───
write_lineage_event(
    source_table=f"s3://iodp-bronze-{args['ENVIRONMENT']}/app_logs/",
    target_table=f"s3://iodp-silver-{args['ENVIRONMENT']}/parsed_logs/",
    transformation="DEDUP(log_id) + TYPE_CAST",
    job_name=args["JOB_NAME"],
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    record_count_in=input_count,
    record_count_out=output_count,
    record_count_dead_letter=input_count - output_count,
    lineage_table=args["LINEAGE_TABLE"],
)

job.commit()
