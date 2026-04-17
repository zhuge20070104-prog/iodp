# glue_jobs/streaming/stream_clickstream.py
"""
Glue Streaming Job: MSK user_clickstream → S3 Bronze (Apache Iceberg)
与 stream_app_logs.py 结构一致，处理点击流数据。
DQ 规则：user_id 非空、event_timestamp 在 24 小时内、event_type 合法枚举值。
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
    col, current_timestamp, from_json, lit, to_timestamp,
)

from lib.data_quality import (
    DataQualityChecker, rule_not_null, rule_timestamp_in_range, rule_in_set,
)
from lib.lineage import write_lineage_event
from lib.iceberg_utils import configure_iceberg
from lib.schema_definitions import CLICKSTREAM_SCHEMA

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "MSK_BOOTSTRAP_SERVERS", "BRONZE_BUCKET",
    "DQ_TABLE", "LINEAGE_TABLE", "ENVIRONMENT",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

BRONZE_PATH = f"{args['BRONZE_BUCKET']}clickstream/"
DEAD_LETTER = f"{args['BRONZE_BUCKET']}dead_letter/"
ENVIRONMENT = args["ENVIRONMENT"]
JOB_RUN_ID  = args.get("JOB_RUN_ID", str(uuid.uuid4()))

configure_iceberg(spark, args["BRONZE_BUCKET"])

VALID_EVENT_TYPES = {"click", "view", "scroll", "purchase", "add_to_cart", "checkout"}


def parse_and_flatten(df: DataFrame) -> DataFrame:
    """解析 Kafka value JSON，展开嵌套的 device_info / geo_info / properties"""
    parsed = df.select(
        from_json(col("value").cast("string"), CLICKSTREAM_SCHEMA).alias("payload"),
        col("timestamp").alias("kafka_ingest_timestamp"),
        col("partition"),
        col("offset"),
    )
    return parsed.select(
        col("payload.event_id"),
        col("payload.user_id"),
        col("payload.session_id"),
        col("payload.event_type"),
        to_timestamp(col("payload.event_timestamp")).alias("event_timestamp"),
        col("payload.page_url"),
        col("payload.referrer_url"),
        # device_info 展开
        col("payload.device_info.device_type").alias("device_type"),
        col("payload.device_info.os").alias("os"),
        col("payload.device_info.browser").alias("browser"),
        # geo_info 展开
        col("payload.geo_info.country_code").alias("country_code"),
        col("payload.geo_info.city").alias("city"),
        col("payload.geo_info.ip_hash").alias("ip_hash"),
        # properties 展开（业务自定义字段）
        col("payload.properties.product_id").alias("product_id"),
        col("payload.properties.amount").alias("amount"),
        col("payload.environment"),
        col("kafka_ingest_timestamp").alias("ingest_timestamp"),
        current_timestamp().alias("processing_timestamp"),
    )


def process_batch(batch_df: DataFrame, batch_id: int):
    if batch_df.isEmpty():
        return

    str_batch_id = str(batch_id)
    parsed_df = parse_and_flatten(batch_df)

    checker = DataQualityChecker(
        table_name="bronze_clickstream",
        batch_id=str_batch_id,
        job_run_id=JOB_RUN_ID,
        dead_letter_base_path=DEAD_LETTER,
        dq_table_name=args["DQ_TABLE"],
        failure_threshold=0.05,
        environment=ENVIRONMENT,
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

    valid_df, dead_letter_df, _ = checker.run(parsed_df)

    if not valid_df.isEmpty():
        valid_df.writeTo(
            f"glue_catalog.iodp_bronze_{ENVIRONMENT}.clickstream"
        ).using("iceberg") \
         .partitionedBy("event_type") \
         .tableProperty("write.parquet.compression-codec", "snappy") \
         .append()

    write_lineage_event(
        source_table="msk::user_clickstream",
        target_table=f"s3://iodp-bronze-{ENVIRONMENT}/clickstream/",
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
    .option("subscribe", "user_clickstream") \
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
            f"{args['BRONZE_BUCKET']}checkpoints/clickstream/") \
    .trigger(processingTime="60 seconds") \
    .start()

query.awaitTermination()
job.commit()
