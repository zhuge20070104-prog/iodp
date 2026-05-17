# glue_jobs/batch/silver_enrich_clicks.py
"""
Glue Batch Job: Bronze clickstream (Firehose GZip NDJSON) → Silver enriched_clicks

读取上一小时 Firehose 落地的 GZip NDJSON：
  s3://<bronze>/clickstream/year=YYYY/month=MM/day=DD/hour=HH/*.gz
逐步完成：
  1. Flatten 嵌套 device_info / geo_info / properties
  2. DQ 检查（user_id/event_id 非空、event_timestamp 时间窗口、event_type 枚举）
  3. 按 event_id 去重
  4. 城市维度 enrich（city → country_code → "unknown" 兜底）
  5. MERGE 写入 Silver Iceberg 表

DQ 由本 Job 承担（取代旧 Glue Streaming Job 的职责）。

──────────────────────────────────────────────────────────────────────────────
 Bronze NDJSON 预期 schema（Producer 端契约，schema-on-read 在 Spark 推断）
──────────────────────────────────────────────────────────────────────────────
   {
     "event_id":        str  (UUID, required)        # 业务幂等键
     "user_id":         str  (required)               # DQ：not null
     "session_id":      str  (optional)
     "event_type":      str  (required)               # DQ：枚举 click/view/scroll/
                                                      #      purchase/add_to_cart/checkout
     "event_timestamp": str  (ISO-8601, required)     # DQ：与 now 偏差 ≤ 24h
     "page_url":        str  (optional)
     "referrer_url":    str  (optional)
     "device_info": {                                 # nested → flatten 到 Silver
       "device_type":       str,
       "os":                str,
       "browser":           str,
       "screen_resolution": str  (optional)
     },
     "geo_info": {                                    # nested → flatten 到 Silver
       "country_code": str,
       "city":         str,
       "ip_hash":      str
     },
     "properties": {                                  # nested → flatten 到 Silver
       "product_id": str,
       "amount":     double  (optional)
     },
     "environment":     str  ("prod" / "dev" / ...)
   }

 多余字段（Producer 抢跑加的新字段）会被 spark.read.json 推断进 DataFrame，
 但若本 Job 没显式 select 出来则会丢弃 —— 想保留新字段需在下面 select() 中加。
"""

import sys
from datetime import datetime, timedelta, timezone

import boto3
from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    coalesce, col, current_timestamp, lit, row_number, to_timestamp,
)
from pyspark.sql.window import Window

from lib.data_quality import (
    DataQualityChecker, rule_not_null, rule_timestamp_in_range, rule_in_set,
)
from lib.lineage import write_lineage_event
from lib.iceberg_utils import configure_iceberg, iceberg_merge_dedup

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "BRONZE_BUCKET", "BRONZE_PREFIX", "SILVER_BUCKET",
    "GLUE_DATABASE_BRONZE", "GLUE_DATABASE_SILVER",
    "LINEAGE_TABLE", "DQ_TABLE", "DQ_THRESHOLD_TABLE", "ENVIRONMENT",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

configure_iceberg(spark, args["SILVER_BUCKET"])

VALID_EVENT_TYPES = {"click", "view", "scroll", "purchase", "add_to_cart", "checkout"}

# ─── 1. 拼出上一小时 Bronze 路径（Firehose dynamic partitioning 落点）───
now_utc    = datetime.now(timezone.utc)
hour_end   = now_utc.replace(minute=0, second=0, microsecond=0)
hour_start = hour_end - timedelta(hours=1)

bronze_root    = args["BRONZE_BUCKET"]               # 形如 s3://iodp-bronze-dev-XXX/
prefix_segment = args["BRONZE_PREFIX"].lstrip("/")   # 形如 clickstream/
hour_prefix    = (
    f"{prefix_segment}"
    f"year={hour_start.year:04d}/"
    f"month={hour_start.month:02d}/"
    f"day={hour_start.day:02d}/"
    f"hour={hour_start.hour:02d}/"
)
bronze_path = f"{bronze_root}{hour_prefix}"
print(f"Reading Bronze path: {bronze_path}")

# ─── 2. 路径不存在则提前结束（Firehose 在无数据小时不创建分区）───
s3       = boto3.client("s3")
bucket   = bronze_root.replace("s3://", "").rstrip("/")
listing  = s3.list_objects_v2(Bucket=bucket, Prefix=hour_prefix, MaxKeys=1)
if "Contents" not in listing:
    print(f"No Bronze data for window {hour_start.isoformat()}; nothing to do.")
    job.commit()
    sys.exit(0)

# ─── 3. 读 Firehose GZip NDJSON（Spark 按扩展名自动解压）───
bronze_raw  = spark.read.json(bronze_path)
input_count = bronze_raw.count()
print(f"Bronze records read: {input_count}")

if input_count == 0:
    print("Empty file set; nothing to do.")
    job.commit()
    sys.exit(0)

# ─── 4. Flatten 嵌套结构 + 注入 ingest_timestamp（Firehose 不自动写）───
flattened_df = bronze_raw.select(
    col("event_id"),
    col("user_id"),
    col("session_id"),
    col("event_type"),
    to_timestamp(col("event_timestamp")).alias("event_timestamp"),
    col("page_url"),
    col("referrer_url"),
    col("device_info.device_type").alias("device_type"),
    col("device_info.os").alias("os"),
    col("device_info.browser").alias("browser"),
    col("geo_info.country_code").alias("country_code"),
    col("geo_info.city").alias("city"),
    col("geo_info.ip_hash").alias("ip_hash"),
    col("properties.product_id").alias("product_id"),
    col("properties.amount").alias("amount"),
    col("environment"),
    current_timestamp().alias("ingest_timestamp"),
)

# ─── 5. DQ 检查（从旧 stream job 迁移至此）───
checker = DataQualityChecker(
    table_name="bronze_clickstream",
    batch_id=hour_start.strftime("%Y%m%d%H"),
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    dead_letter_base_path=f"{bronze_root}dead_letter/clickstream/",
    dq_table_name=args["DQ_TABLE"],
    failure_threshold=0.05,
    environment=args["ENVIRONMENT"],
)
checker.add_rule(
    rule_not_null("event_id")
).add_rule(
    rule_not_null("user_id")
).add_rule(
    rule_timestamp_in_range("event_timestamp", max_lag_hours=24)
).add_rule(
    rule_in_set("event_type", valid_values=VALID_EVENT_TYPES, rule_name="valid_event_type")
)

valid_df, dead_letter_df, _ = checker.run(flattened_df)
dq_dropped   = dead_letter_df.count()
valid_count  = valid_df.count()
print(f"DQ: {valid_count} valid / {dq_dropped} dead-letter")

# ─── 6. 按 event_id 去重 ───
window     = Window.partitionBy("event_id").orderBy(col("ingest_timestamp").asc())
deduped_df = valid_df \
    .withColumn("_rn", row_number().over(window)) \
    .filter(col("_rn") == 1) \
    .drop("_rn")

# ─── 7. Enrich：city 缺失退到 country_code → "unknown" ───
enriched_df = deduped_df.withColumn(
    "city",
    coalesce(col("city"), col("country_code"), lit("unknown"))
).withColumn(
    "event_date", col("event_timestamp").cast("date")
).withColumn(
    "processing_timestamp", current_timestamp()
)

# ─── 8. MERGE 写入 Silver（幂等重跑）───
enriched_df.createOrReplaceTempView("silver_clicks_source")
iceberg_merge_dedup(
    spark=spark,
    source_view="silver_clicks_source",
    target_table=f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.enriched_clicks",
    merge_keys=["event_id"],
)

output_count  = enriched_df.count()
dedup_removed = valid_count - output_count

# ─── 9. 血缘 ───
write_lineage_event(
    source_table=f"s3://iodp-bronze-{args['ENVIRONMENT']}/clickstream/",
    target_table=f"s3://iodp-silver-{args['ENVIRONMENT']}/enriched_clicks/",
    transformation=(
        f"FLATTEN + DQ(dropped={dq_dropped}) + DEDUP(removed={dedup_removed}) + ENRICH_CITY"
    ),
    job_name=args["JOB_NAME"],
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    record_count_in=input_count,
    record_count_out=output_count,
    record_count_dead_letter=dq_dropped,
    lineage_table=args["LINEAGE_TABLE"],
)

job.commit()
