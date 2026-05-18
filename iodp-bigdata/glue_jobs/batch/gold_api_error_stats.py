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

from lib.iceberg_utils import configure_iceberg, iceberg_merge_upsert

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "SILVER_BUCKET", "GOLD_BUCKET",
    "ENVIRONMENT", "GLUE_DATABASE_SILVER", "GLUE_DATABASE_GOLD",
])


def _get_optional_arg(name):
    """getResolvedOptions 会因缺参直接抛异常；用这个包一层做可选参数。"""
    try:
        return getResolvedOptions(sys.argv, [name])[name]
    except Exception:
        return None


sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

# 注册 glue_catalog（其他 6 个 batch job 都调了，唯独这里之前漏了，导致
# spark.read.format("iceberg").load("glue_catalog.x.y") 时 catalog 未注册，
# Iceberg 掉回 Hive Metastore，报 None.get / MissingTableException: VERSION）
configure_iceberg(spark, args["GOLD_BUCKET"])

# 计算处理窗口（cron 默认上一小时；--TARGET_HOUR=YYYY-MM-DD-HH UTC 覆盖）
target_hour_str = _get_optional_arg("TARGET_HOUR")
if target_hour_str:
    processing_hour_start = datetime.strptime(target_hour_str, "%Y-%m-%d-%H").replace(tzinfo=timezone.utc)
    processing_hour_end   = processing_hour_start + timedelta(hours=1)
    print(f"TARGET_HOUR override: {processing_hour_start.isoformat()}")
else:
    now_utc = datetime.now(timezone.utc)
    processing_hour_end   = now_utc.replace(minute=0, second=0, microsecond=0)
    processing_hour_start = processing_hour_end - timedelta(hours=1)

print(f"Processing window: {processing_hour_start} ~ {processing_hour_end}")

# ─── 读取 Silver 层 app_logs ───
error_logs_df = spark.read.format("iceberg").load(
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

error_stats_df = error_logs_df.groupBy(
    date_trunc("hour", col("event_timestamp")).alias("stat_hour"),
    col("service_name"),
    col("error_code"),
).agg(
    count("*").alias("error_count"),
    # 注意：p99 只覆盖"出错的请求"（error_logs_df 已按 log_level 过滤），
    # 不是服务整体 p99。用于诊断"出错时有多慢"（接近超时阈值往往=超时引发的错）。
    # 若需全量请求延迟趋势，应另起一个 Job 读 all_logs_df。
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

# ─── 写入 Gold Iceberg 表（MERGE UPSERT，键命中则 UPDATE，否则 INSERT）───
# 历史上这里是 .overwritePartitions()，bug：分区粒度 (stat_date, service_name) 比
# 任务粒度（每小时）粗，hour=N 跑完会把同分区 hour=0..N-1 的数据全擦掉。
# 改成 MERGE，键为 (stat_hour, service_name, error_code)，避免跨小时互相覆盖。
gold_df.createOrReplaceTempView("gold_api_error_stats_source")
iceberg_merge_upsert(
    spark=spark,
    source_view="gold_api_error_stats_source",
    target_table=f"glue_catalog.{args['GLUE_DATABASE_GOLD']}.api_error_stats",
    merge_keys=["stat_hour", "service_name", "error_code"],
)

print(f"Gold api_error_stats merged: {gold_df.count()} source rows for window {processing_hour_start}")

job.commit()
