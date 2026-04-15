# glue_jobs/batch/replay_app_logs_to_bronze.py
"""
Glue Batch Job: replay/ → Bronze app_logs (Iceberg)

手动触发，将 DLQ Replay Lambda 复制到 replay/ 目录的死信数据重新写入 Bronze Iceberg 表。

前置条件：
  1. DLQ Replay Lambda 已将死信文件从 dead_letter/ 复制到 replay/bronze_app_logs/{batch_date}/
  2. 数据工程师已审查死信原因，确认可以重灌（例如 DQ 规则已修正）

触发方式：
  aws glue start-job-run \
    --job-name iodp-replay-app-logs-to-bronze-{env} \
    --arguments '{"--TABLE_NAME":"bronze_app_logs","--BATCH_DATE":"2026-04-06"}'

数据流：
  s3://{BRONZE_BUCKET}/replay/bronze_app_logs/{batch_date}/**/*.parquet
    → 读取已展平的 parquet（dead letter 格式）
    → 去除 _dq_error_type 标记列
    → 补充 event_date 分区列
    → APPEND 到 Bronze Iceberg app_logs 表
    → 下游 silver_parse_logs 自动消费（iceberg_merge_dedup 保证幂等）
"""

import sys
import uuid

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import col, current_timestamp, date_format

from lib.iceberg_utils import configure_iceberg
from lib.lineage import write_lineage_event

args = getResolvedOptions(sys.argv, [
    "JOB_NAME",
    "BRONZE_BUCKET",
    "LINEAGE_TABLE",
    "ENVIRONMENT",
    "TABLE_NAME",
    "BATCH_DATE",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

ENVIRONMENT = args["ENVIRONMENT"]
TABLE_NAME  = args["TABLE_NAME"]    # e.g. "bronze_app_logs"
BATCH_DATE  = args["BATCH_DATE"]    # e.g. "2026-04-06"
JOB_RUN_ID  = args.get("JOB_RUN_ID", str(uuid.uuid4()))

configure_iceberg(spark, f"s3://{args['BRONZE_BUCKET']}/")

REPLAY_PATH = f"s3://{args['BRONZE_BUCKET']}/replay/{TABLE_NAME}/{BATCH_DATE}/"
TARGET_TABLE = f"glue_catalog.iodp_bronze_{ENVIRONMENT}.app_logs"

print(f"Replay app_logs: reading from {REPLAY_PATH}")
print(f"Target Iceberg table: {TARGET_TABLE}")

# ─── 1. 读取 replay 目录下的 parquet 文件 ───
replay_df = spark.read.parquet(REPLAY_PATH)
input_count = replay_df.count()
print(f"Replay records read: {input_count}")

if input_count == 0:
    print("No records to replay. Exiting.")
    job.commit()
    sys.exit(0)

# ─── 2. 去除 DQ 标记列，补充分区列 ───
# dead letter 数据包含 _dq_error_type 列，写入 Bronze 前需要移除
columns_to_drop = [c for c in replay_df.columns if c.startswith("_dq_")]
clean_df = replay_df.drop(*columns_to_drop)

# 刷新 ingest_timestamp 为当前时间，否则下游 silver_parse_logs 按小时窗口过滤会漏掉这些记录
# 补充 event_date 分区列（与 stream_app_logs.py 一致：partitionedBy("event_date", "log_level")）
clean_df = clean_df.withColumn(
    "ingest_timestamp",
    current_timestamp(),
).withColumn(
    "processing_timestamp",
    current_timestamp(),
).withColumn(
    "event_date",
    date_format(col("event_timestamp"), "yyyy-MM-dd"),
)

# ─── 3. 写入 Bronze Iceberg 表（append，不覆盖现有数据）───
clean_df.writeTo(TARGET_TABLE) \
    .using("iceberg") \
    .partitionedBy("event_date", "log_level") \
    .tableProperty("write.parquet.compression-codec", "snappy") \
    .append()

output_count = clean_df.count()
print(f"Replayed {output_count} records to {TARGET_TABLE}")

# ─── 4. 血缘记录 ───
write_lineage_event(
    source_table=f"s3://{args['BRONZE_BUCKET']}/replay/{TABLE_NAME}/{BATCH_DATE}/",
    target_table=f"s3://{args['BRONZE_BUCKET']}/app_logs/",
    transformation="DLQ_REPLAY: DROP_DQ_COLS + ADD_PARTITION_COL + APPEND",
    job_name=args["JOB_NAME"],
    job_run_id=JOB_RUN_ID,
    record_count_in=input_count,
    record_count_out=output_count,
    record_count_dead_letter=0,
    lineage_table=args["LINEAGE_TABLE"],
)

print(f"Replay complete. Downstream silver_parse_logs will pick up data automatically.")
job.commit()
