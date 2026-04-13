# glue_jobs/batch/gold_hourly_active_users.py
"""
Glue Batch Job: Silver enriched_clicks → Gold hourly_active_users
每小时运行，统计各漏斗阶段的去重用户数，供 BI Dashboard 画转化漏斗。

输出 Schema:
  stat_hour           TIMESTAMP  统计小时（整点）
  stat_date           DATE       分区键
  active_users        BIGINT     该小时有任意行为的去重用户数
  view_users          BIGINT     浏览用户数
  add_to_cart_users   BIGINT     加购用户数
  checkout_users      BIGINT     进入结算用户数
  purchase_users      BIGINT     完成购买用户数
  new_users           BIGINT     首次出现的用户数（该小时前无记录）
  environment         STRING
"""

import sys
from datetime import datetime, timedelta, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    col, count, countDistinct, date_trunc, lit, when,
)

from lib.lineage import write_lineage_event
from lib.iceberg_utils import configure_iceberg

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "SILVER_BUCKET", "GOLD_BUCKET",
    "GLUE_DATABASE_SILVER", "GLUE_DATABASE_GOLD",
    "LINEAGE_TABLE", "ENVIRONMENT",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

configure_iceberg(spark, args["GOLD_BUCKET"])

now_utc    = datetime.now(timezone.utc)
hour_end   = now_utc.replace(minute=0, second=0, microsecond=0)
hour_start = hour_end - timedelta(hours=1)

# ─── 读取 Silver 上一小时点击流 ───
silver_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.enriched_clicks"
).filter(
    (col("event_timestamp") >= lit(hour_start.isoformat())) &
    (col("event_timestamp") <  lit(hour_end.isoformat()))
)

input_count = silver_df.count()

# ─── 查询历史，识别 new_users（当前小时前从未出现的 user_id）───
# 简化实现：查 Silver 全表中 event_timestamp < hour_start 的 user_id 集合
existing_users_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.enriched_clicks"
).filter(
    col("event_timestamp") < lit(hour_start.isoformat())
).select("user_id").distinct()

current_users_df = silver_df.select("user_id").distinct()
new_users_df = current_users_df.subtract(existing_users_df)
new_user_count = new_users_df.count()

# ─── 漏斗聚合（按小时统计各 event_type 的去重用户数）───
gold_df = silver_df.groupBy(
    date_trunc("hour", col("event_timestamp")).alias("stat_hour"),
).agg(
    countDistinct("user_id").alias("active_users"),
    countDistinct(when(col("event_type") == "view",         col("user_id"))).alias("view_users"),
    countDistinct(when(col("event_type") == "add_to_cart",  col("user_id"))).alias("add_to_cart_users"),
    countDistinct(when(col("event_type") == "checkout",     col("user_id"))).alias("checkout_users"),
    countDistinct(when(col("event_type") == "purchase",     col("user_id"))).alias("purchase_users"),
).withColumn(
    "new_users", lit(new_user_count)
).withColumn(
    "stat_date", col("stat_hour").cast("date")
).withColumn(
    "environment", lit(args["ENVIRONMENT"])
)

# ─── 写入 Gold Iceberg（覆盖当前分区，支持幂等重跑）───
gold_df.writeTo(
    f"glue_catalog.{args['GLUE_DATABASE_GOLD']}.hourly_active_users"
).using("iceberg") \
 .partitionedBy("stat_date") \
 .tableProperty("write.parquet.compression-codec", "snappy") \
 .overwritePartitions()

output_count = gold_df.count()
print(f"Gold hourly_active_users written: {output_count} rows for {hour_start}")

write_lineage_event(
    source_table=f"s3://iodp-silver-{args['ENVIRONMENT']}/enriched_clicks/",
    target_table=f"s3://iodp-gold-{args['ENVIRONMENT']}/hourly_active_users/",
    transformation="FUNNEL_AGGREGATION",
    job_name=args["JOB_NAME"],
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    record_count_in=input_count,
    record_count_out=output_count,
    record_count_dead_letter=0,
    lineage_table=args["LINEAGE_TABLE"],
)

job.commit()
