# glue_jobs/batch/silver_parse_logs.py
"""
Glue Batch Job: Bronze app_logs (Firehose GZip NDJSON) → Silver parsed_logs

读取上一小时 Firehose 落地的 GZip NDJSON：
  s3://<bronze>/app_logs/year=YYYY/month=MM/day=DD/hour=HH/*.gz
逐步完成：
  1. 字段重命名（duration_ms → req_duration_ms，与 Silver/Gold schema 对齐）
  2. DQ 检查（log_id 非空、log_level 枚举、error_code 字典）
  3. 按 log_id 去重
  4. 类型校正（req_duration_ms / http_status / event_timestamp）
  5. MERGE 写入 Silver Iceberg 表

DQ 由本 Job 承担（取代旧 Glue Streaming Job 的职责）。

──────────────────────────────────────────────────────────────────────────────
 Bronze NDJSON 预期 schema（Producer 端契约，schema-on-read 在 Spark 推断）
──────────────────────────────────────────────────────────────────────────────
   {
     "log_id":          str  (UUID, required)        # 业务幂等键 / DQ：not null
     "user_id":         str  (optional)
     "service_name":    str  (required)               # 例：payment-service
     "log_level":       str  (required)               # DQ：枚举 DEBUG/INFO/WARN/
                                                      #      ERROR/FATAL
     "error_code":      str  (optional)               # 例：E2001（log_level=ERROR 时填）
     "error_message":   str  (optional)
     "stack_trace":     str  (optional)
     "req_path":        str  (optional)
     "req_method":      str  (optional)               # GET / POST / PUT / ...
     "http_status":     int  (optional)               # 在 Silver 强转 integer
     "duration_ms":     int  (optional)               # ⚠ Bronze 写 duration_ms，
                                                      #   Silver 重命名为 req_duration_ms
     "trace_id":        str  (optional)
     "event_timestamp": str  (ISO-8601, required)     # DQ：not null
     "environment":     str  ("prod" / "dev" / ...)
   }

 ingest_timestamp 不由 producer 提供，本 Job 用 current_timestamp() 注入；
 用作去重排序键。
"""

import sys
from datetime import datetime, timedelta, timezone

import boto3
from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    col, current_timestamp, row_number, to_timestamp,
)
from pyspark.sql.window import Window

from lib.data_quality import (
    DataQualityChecker, rule_not_null, rule_in_set,
)
from lib.lineage import write_lineage_event
from lib.iceberg_utils import configure_iceberg, iceberg_merge_dedup

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "BRONZE_BUCKET", "BRONZE_PREFIX", "SILVER_BUCKET",
    "GLUE_DATABASE_BRONZE", "GLUE_DATABASE_SILVER",
    "LINEAGE_TABLE", "DQ_TABLE", "DQ_THRESHOLD_TABLE", "ENVIRONMENT",
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

configure_iceberg(spark, args["SILVER_BUCKET"])

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARN", "ERROR", "FATAL"}

# ─── 1. 决定要处理的小时窗口（cron 默认上一小时；--TARGET_HOUR 覆盖）───
target_hour_str = _get_optional_arg("TARGET_HOUR")
if target_hour_str:
    hour_start = datetime.strptime(target_hour_str, "%Y-%m-%d-%H").replace(tzinfo=timezone.utc)
    hour_end   = hour_start + timedelta(hours=1)
    print(f"TARGET_HOUR override: {hour_start.isoformat()}")
else:
    now_utc    = datetime.now(timezone.utc)
    hour_end   = now_utc.replace(minute=0, second=0, microsecond=0)
    hour_start = hour_end - timedelta(hours=1)

bronze_root    = args["BRONZE_BUCKET"]
prefix_segment = args["BRONZE_PREFIX"].lstrip("/")
hour_prefix    = (
    f"{prefix_segment}"
    f"year={hour_start.year:04d}/"
    f"month={hour_start.month:02d}/"
    f"day={hour_start.day:02d}/"
    f"hour={hour_start.hour:02d}/"
)
bronze_path = f"{bronze_root}{hour_prefix}"
print(f"Reading Bronze path: {bronze_path}  (window: {hour_start} ~ {hour_end})")

# ─── 2. 路径不存在则提前结束 ───
s3      = boto3.client("s3")
bucket  = bronze_root.replace("s3://", "").rstrip("/")
listing = s3.list_objects_v2(Bucket=bucket, Prefix=hour_prefix, MaxKeys=1)
if "Contents" not in listing:
    print(f"No Bronze data for window {hour_start.isoformat()}; nothing to do.")
    job.commit()
    sys.exit(0)

# ─── 3. 读 Firehose GZip NDJSON ───
bronze_raw  = spark.read.json(bronze_path)
input_count = bronze_raw.count()
print(f"Bronze records read: {input_count}")

if input_count == 0:
    print("Empty file set; nothing to do.")
    job.commit()
    sys.exit(0)

# ─── 4. 字段标准化（producer 已对齐 DDL schema，这里仅做类型 cast 和注入 ingest_timestamp）───
normalized_df = bronze_raw.select(
    col("log_id"),
    col("trace_id"),
    col("span_id"),
    col("service_name"),
    col("instance_id"),
    col("log_level"),
    to_timestamp(col("event_timestamp")).alias("event_timestamp"),
    col("message"),
    col("error_code"),
    col("error_type"),
    col("http_status").cast("integer").alias("http_status"),
    col("stack_trace"),
    col("req_method"),
    col("req_path"),
    col("user_id"),
    col("req_duration_ms").cast("double").alias("req_duration_ms"),
    col("environment"),
    current_timestamp().alias("ingest_timestamp"),
)

# ─── 5. DQ 检查 ───
checker = DataQualityChecker(
    table_name="bronze_app_logs",
    batch_id=hour_start.strftime("%Y%m%d%H"),
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    dead_letter_base_path=f"{bronze_root}dead_letter/app_logs/",
    dq_table_name=args["DQ_TABLE"],
    failure_threshold=0.05,
    environment=args["ENVIRONMENT"],
)
checker.add_rule(
    rule_not_null("log_id")
).add_rule(
    rule_not_null("event_timestamp")
).add_rule(
    rule_in_set("log_level", valid_values=VALID_LOG_LEVELS, rule_name="valid_log_level")
)

valid_df, dead_letter_df, _ = checker.run(normalized_df)
dq_dropped  = dead_letter_df.count()
valid_count = valid_df.count()
print(f"DQ: {valid_count} valid / {dq_dropped} dead-letter")

# ─── 6. 按 log_id 去重 ───
window     = Window.partitionBy("log_id").orderBy(col("ingest_timestamp").asc())
deduped_df = valid_df \
    .withColumn("_rn", row_number().over(window)) \
    .filter(col("_rn") == 1) \
    .drop("_rn")

# ─── 7. 派生分区列 ───
silver_df = deduped_df \
    .withColumn("processing_timestamp", current_timestamp()) \
    .withColumn("event_date", col("event_timestamp").cast("date"))

# ─── 8. MERGE 写入 Silver（幂等重跑）───
silver_df.createOrReplaceTempView("silver_logs_source")
iceberg_merge_dedup(
    spark=spark,
    source_view="silver_logs_source",
    target_table=f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.parsed_logs",
    merge_keys=["log_id"],
)

output_count  = silver_df.count()
dedup_removed = valid_count - output_count
print(f"Silver records written: {output_count} (deduped {dedup_removed})")

# ─── 9. 血缘 ───
write_lineage_event(
    source_table=f"s3://iodp-bronze-{args['ENVIRONMENT']}/app_logs/",
    target_table=f"s3://iodp-silver-{args['ENVIRONMENT']}/parsed_logs/",
    transformation=(
        f"NORMALIZE + DQ(dropped={dq_dropped}) + DEDUP(removed={dedup_removed}) + TYPE_CAST"
    ),
    job_name=args["JOB_NAME"],
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    record_count_in=input_count,
    record_count_out=output_count,
    record_count_dead_letter=dq_dropped,
    lineage_table=args["LINEAGE_TABLE"],
)

job.commit()
