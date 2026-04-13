# glue_jobs/streaming/stream_app_logs.py
"""
Glue Streaming Job: MSK system_app_logs → S3 Bronze (Apache Iceberg)

分区改进（v2）：
  原来：partitionedBy("service_name", "log_level")
  问题：低流量服务产生大量小文件，Athena 扫描开销高
  改为：partitionedBy("event_date", "log_level")
  优化：service_name 作为 WHERE 谓词过滤，而非分区键；Iceberg 元数据驱动跳过不相关分区

其他改进：
  - DataQualityChecker 传入 threshold_config_table 参数，支持按表动态阈值
"""

import sys
import uuid
from datetime import datetime, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, date_format, from_json, lit,
    to_timestamp,
)
from pyspark.sql.types import (
    IntegerType, StringType, StructField, StructType,
)

from lib.data_quality import (
    DataQualityChecker, VALID_ERROR_CODES,
    rule_not_null, rule_timestamp_in_range, rule_in_set,
)
from lib.lineage import write_lineage_event
from lib.iceberg_utils import ensure_iceberg_table

# ─── 解析 Glue Job 参数 ───
args = getResolvedOptions(sys.argv, [
    "JOB_NAME",
    "MSK_BOOTSTRAP_SERVERS",
    "BRONZE_BUCKET",
    "DQ_TABLE",
    "LINEAGE_TABLE",
    "ENVIRONMENT",
])
# 可选参数：DQ 阈值配置表（新增）
DQ_THRESHOLD_CONFIG_TABLE = args.get("DQ_THRESHOLD_CONFIG_TABLE", "")

sc           = SparkContext()
glueContext  = GlueContext(sc)
spark        = glueContext.spark_session
job          = Job(glueContext)
job.init(args["JOB_NAME"], args)

BRONZE_PATH  = f"{args['BRONZE_BUCKET']}app_logs/"
DEAD_LETTER  = f"{args['BRONZE_BUCKET']}dead_letter/"
ENVIRONMENT  = args["ENVIRONMENT"]
JOB_RUN_ID   = args.get("JOB_RUN_ID", str(uuid.uuid4()))

spark.conf.set(
    "spark.sql.extensions",
    "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
)
spark.conf.set("spark.sql.catalog.glue_catalog", "org.apache.iceberg.aws.glue.GlueCatalog")
spark.conf.set("spark.sql.catalog.glue_catalog.warehouse", args["BRONZE_BUCKET"])

APP_LOG_SCHEMA = StructType([
    StructField("log_id",          StringType(), False),
    StructField("trace_id",        StringType(), True),
    StructField("span_id",         StringType(), True),
    StructField("service_name",    StringType(), False),
    StructField("instance_id",     StringType(), True),
    StructField("log_level",       StringType(), False),
    StructField("event_timestamp", StringType(), False),
    StructField("message",         StringType(), False),
    StructField("error_details", StructType([
        StructField("error_code",  StringType(), True),
        StructField("error_type",  StringType(), True),
        StructField("stack_trace", StringType(), True),
        StructField("http_status", IntegerType(), True),
    ]), True),
    StructField("request_info", StructType([
        StructField("method",      StringType(), True),
        StructField("path",        StringType(), True),
        StructField("user_id",     StringType(), True),
        StructField("duration_ms", StringType(), True),
    ]), True),
    StructField("environment", StringType(), True),
])


def parse_and_flatten(df: DataFrame) -> DataFrame:
    """从 Kafka value (JSON string) 解析并展开嵌套字段"""
    parsed = df.select(
        from_json(col("value").cast("string"), APP_LOG_SCHEMA).alias("payload"),
        col("timestamp").alias("kafka_ingest_timestamp"),
        col("partition"),
        col("offset"),
    )

    flattened = parsed.select(
        col("payload.log_id"),
        col("payload.trace_id"),
        col("payload.span_id"),
        col("payload.service_name"),
        col("payload.instance_id"),
        col("payload.log_level"),
        to_timestamp(col("payload.event_timestamp")).alias("event_timestamp"),
        col("payload.message"),
        col("payload.error_details.error_code").alias("error_code"),
        col("payload.error_details.error_type").alias("error_type"),
        col("payload.error_details.http_status").alias("http_status"),
        col("payload.error_details.stack_trace").alias("stack_trace"),
        col("payload.request_info.method").alias("req_method"),
        col("payload.request_info.path").alias("req_path"),
        col("payload.request_info.user_id").alias("user_id"),
        col("payload.request_info.duration_ms").cast("double").alias("req_duration_ms"),
        col("payload.environment"),
        col("kafka_ingest_timestamp").alias("ingest_timestamp"),
        current_timestamp().alias("processing_timestamp"),
    )
    return flattened


def process_batch(batch_df: DataFrame, batch_id: int):
    """Structured Streaming foreachBatch 处理函数"""
    if batch_df.isEmpty():
        return

    str_batch_id = str(batch_id)
    parsed_df    = parse_and_flatten(batch_df)

    # ─── DQ 校验（支持动态阈值）───
    checker = DataQualityChecker(
        table_name="bronze_app_logs",
        batch_id=str_batch_id,
        job_run_id=JOB_RUN_ID,
        dead_letter_base_path=DEAD_LETTER,
        dq_table_name=args["DQ_TABLE"],
        failure_threshold=0.05,               # 兜底默认值
        threshold_config_table=DQ_THRESHOLD_CONFIG_TABLE or None,  # 动态配置（v2）
        environment=ENVIRONMENT,
    )

    checker.add_rule(
        rule_not_null("log_id")
    ).add_rule(
        rule_not_null("service_name")
    ).add_rule(
        rule_timestamp_in_range("event_timestamp", max_lag_hours=24)
    ).add_rule(
        rule_in_set(
            "error_code",
            valid_values=VALID_ERROR_CODES | {None},
            rule_name="valid_error_code",
        )
    )

    valid_df, dead_letter_df, dq_results = checker.run(parsed_df)

    # ─── 添加分区列 event_date（v2 改进：用日期分区替代 service_name）───
    if not valid_df.isEmpty():
        valid_df_with_partition = valid_df.withColumn(
            "event_date",
            date_format(col("event_timestamp"), "yyyy-MM-dd"),
        )

        # 按 event_date + log_level 分区
        # service_name 作为普通列，Athena 查询时用 WHERE service_name = '...' 过滤
        # Iceberg metadata filtering 会自动跳过无关分区
        valid_df_with_partition.writeTo(
            f"glue_catalog.iodp_bronze_{ENVIRONMENT}.app_logs"
        ).using("iceberg") \
         .partitionedBy("event_date", "log_level") \
         .tableProperty("write.parquet.compression-codec", "snappy") \
         .append()

    write_lineage_event(
        source_table="msk::system_app_logs",
        target_table=f"s3://bronze/app_logs/",
        transformation="JSON_PARSE + FLATTEN + DQ_CHECK",
        job_name=args["JOB_NAME"],
        job_run_id=JOB_RUN_ID,
        record_count_in=batch_df.count(),
        record_count_out=valid_df.count(),
        record_count_dead_letter=dead_letter_df.count(),
        lineage_table=args["LINEAGE_TABLE"],
    )


kafka_df = spark \
    .readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", args["MSK_BOOTSTRAP_SERVERS"]) \
    .option("subscribe", "system_app_logs") \
    .option("startingOffsets", "latest") \
    .option("kafka.security.protocol", "SASL_SSL") \
    .option("kafka.sasl.mechanism", "AWS_MSK_IAM") \
    .option("kafka.sasl.jaas.config",
            "software.amazon.msk.auth.iam.IAMLoginModule required;") \
    .option("kafka.sasl.client.callback.handler.class",
            "software.amazon.msk.auth.iam.IAMClientCallbackHandler") \
    .option("failOnDataLoss", "false") \
    .load()

query = kafka_df \
    .writeStream \
    .foreachBatch(process_batch) \
    .option("checkpointLocation",
            f"s3://{args['BRONZE_BUCKET']}checkpoints/app_logs/") \
    .trigger(processingTime="60 seconds") \
    .start()

query.awaitTermination()
job.commit()
