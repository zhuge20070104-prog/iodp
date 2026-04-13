# glue_jobs/batch/silver_enrich_clicks.py
"""
Glue Batch Job: Bronze clickstream → Silver enriched_clicks
每小时运行，对上一小时的点击流 Bronze 数据做：
  1. 去重（event_id 唯一）
  2. 补充城市级地理维度（此处示意，实际可查 MaxMind IP 库）
  3. 写入 Silver Iceberg 表，按 event_type + event_date 分区
"""

import sys
from datetime import datetime, timedelta, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    col, current_timestamp, lit, row_number, when,
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

# ─── 1. 读取 Bronze ───
bronze_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_BRONZE']}.clickstream"
).filter(
    (col("ingest_timestamp") >= lit(hour_start.isoformat())) &
    (col("ingest_timestamp") <  lit(hour_end.isoformat()))
)

input_count = bronze_df.count()

# ─── 2. 去重：同一 event_id 保留最早的一条 ───
window = Window.partitionBy("event_id").orderBy(col("ingest_timestamp").asc())
deduped_df = bronze_df \
    .withColumn("_rn", row_number().over(window)) \
    .filter(col("_rn") == 1) \
    .drop("_rn")

# ─── 3. 补充维度：对缺失城市的记录用 country_code 兜底 ───
enriched_df = deduped_df.withColumn(
    "city",
    when(col("city").isNull(), lit("unknown")).otherwise(col("city"))
).withColumn(
    "event_date", col("event_timestamp").cast("date")
).withColumn(
    "processing_timestamp", current_timestamp()
)

# ─── 4. 写入 Silver（MERGE 去重）───
enriched_df.createOrReplaceTempView("silver_clicks_source")
iceberg_merge_dedup(
    spark=spark,
    source_view="silver_clicks_source",
    target_table=f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.enriched_clicks",
    merge_keys=["event_id"],
)

output_count = enriched_df.count()

# ─── 5. 血缘 ───
write_lineage_event(
    source_table=f"s3://iodp-bronze-{args['ENVIRONMENT']}/clickstream/",
    target_table=f"s3://iodp-silver-{args['ENVIRONMENT']}/enriched_clicks/",
    transformation="DEDUP(event_id) + ENRICH_CITY",
    job_name=args["JOB_NAME"],
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    record_count_in=input_count,
    record_count_out=output_count,
    record_count_dead_letter=input_count - output_count,
    lineage_table=args["LINEAGE_TABLE"],
)

job.commit()
