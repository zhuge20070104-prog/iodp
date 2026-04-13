# glue_jobs/batch/gold_api_error_stats.py
"""
Glue Batch Job: Silver app_logs → Gold api_error_stats
每小时运行一次，输出数据供 Agent Log Analyzer 通过 Athena 查询

输出 Schema:
  stat_hour         TIMESTAMP   统计小时（整点）
  service_name      STRING      服务名
  error_code        STRING      错误码
  total_requests    BIGINT      该小时该服务总请求数
  error_count       BIGINT      错误数
  error_rate        DOUBLE      错误率
  p99_duration_ms   DOUBLE      P99 响应时间
  unique_users      BIGINT      受影响的唯一用户数
  sample_trace_ids  ARRAY<STR>  最多 5 个 trace_id 样本（供 Agent 关联）
"""

import sys
from datetime import datetime, timedelta, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    col, collect_list, count, countDistinct, date_trunc,
    expr, lit, percentile_approx, slice as spark_slice,
    sum as spark_sum, when,
)

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "SILVER_BUCKET", "GOLD_BUCKET",
    "ENVIRONMENT", "GLUE_DATABASE_SILVER", "GLUE_DATABASE_GOLD",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

# 计算处理窗口：上一个完整小时
now_utc = datetime.now(timezone.utc)
processing_hour_end   = now_utc.replace(minute=0, second=0, microsecond=0)
processing_hour_start = processing_hour_end - timedelta(hours=1)

print(f"Processing window: {processing_hour_start} ~ {processing_hour_end}")

# ─── 读取 Silver 层 app_logs ───
silver_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.parsed_logs"
).filter(
    (col("event_timestamp") >= lit(processing_hour_start.isoformat())) &
    (col("event_timestamp") <  lit(processing_hour_end.isoformat())) &
    (col("log_level").isin(["ERROR", "FATAL"]))
)

# ─── 聚合计算 ───
# 先计算总请求数（包含所有 log_level）
all_logs_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.parsed_logs"
).filter(
    (col("event_timestamp") >= lit(processing_hour_start.isoformat())) &
    (col("event_timestamp") <  lit(processing_hour_end.isoformat()))
).groupBy("service_name").agg(
    count("*").alias("total_requests")
)

error_stats_df = silver_df.groupBy(
    date_trunc("hour", col("event_timestamp")).alias("stat_hour"),
    col("service_name"),
    col("error_code"),
).agg(
    count("*").alias("error_count"),
    percentile_approx("req_duration_ms", 0.99).alias("p99_duration_ms"),
    countDistinct("user_id").alias("unique_users"),
    # 采样 trace_id 供 Agent 关联（最多取 5 个）
    spark_slice(collect_list("trace_id"), 1, 5).alias("sample_trace_ids"),
)

# ─── Join 计算错误率 ───
gold_df = error_stats_df.join(
    all_logs_df, on="service_name", how="left"
).withColumn(
    "error_rate",
    when(col("total_requests") > 0,
         col("error_count") / col("total_requests")
    ).otherwise(0.0)
).withColumn(
    "stat_date", col("stat_hour").cast("date")
).withColumn(
    "environment", lit(args["ENVIRONMENT"])
)

# ─── 写入 Gold Iceberg 表（按 stat_date 分区）───
gold_df.writeTo(
    f"glue_catalog.{args['GLUE_DATABASE_GOLD']}.api_error_stats"
).using("iceberg") \
 .partitionedBy("stat_date", "service_name") \
 .tableProperty("write.parquet.compression-codec", "snappy") \
 .overwritePartitions()   # 幂等写入，支持重跑

print(f"Gold api_error_stats written: {gold_df.count()} rows")

job.commit()
