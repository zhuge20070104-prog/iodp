# glue_jobs/lib/schema_definitions.py
"""
集中管理所有 Kafka Topic 和各层表的 PySpark Schema。
各 Glue Job 从此处 import，避免重复定义导致不一致。
"""

from pyspark.sql.types import (
    ArrayType, DoubleType, IntegerType, LongType,
    StringType, StructField, StructType, TimestampType,
)

# ─── Kafka Raw Schema（所有 Topic 共用）───
KAFKA_RAW_SCHEMA = StructType([
    StructField("key",       StringType(),  True),
    StructField("value",     StringType(),  True),
    StructField("topic",     StringType(),  True),
    StructField("partition", IntegerType(), True),
    StructField("offset",    LongType(),    True),
    StructField("timestamp", StringType(),  True),
])

# ─── user_clickstream Payload Schema ───
CLICKSTREAM_SCHEMA = StructType([
    StructField("event_id",        StringType(),  False),
    StructField("user_id",         StringType(),  True),
    StructField("session_id",      StringType(),  True),
    StructField("event_type",      StringType(),  True),
    StructField("event_timestamp", StringType(),  False),
    StructField("page_url",        StringType(),  True),
    StructField("referrer_url",    StringType(),  True),
    StructField("device_info", StructType([
        StructField("device_type",       StringType(), True),
        StructField("os",                StringType(), True),
        StructField("browser",           StringType(), True),
        StructField("screen_resolution", StringType(), True),
    ]), True),
    StructField("geo_info", StructType([
        StructField("country_code", StringType(), True),
        StructField("city",         StringType(), True),
        StructField("ip_hash",      StringType(), True),
    ]), True),
    StructField("properties", StructType([
        StructField("product_id", StringType(), True),
        StructField("amount",     DoubleType(), True),
    ]), True),
])

# ─── system_app_logs Payload Schema ───
APP_LOG_SCHEMA = StructType([
    StructField("log_id",          StringType(),  False),
    StructField("trace_id",        StringType(),  True),
    StructField("span_id",         StringType(),  True),
    StructField("service_name",    StringType(),  False),
    StructField("instance_id",     StringType(),  True),
    StructField("log_level",       StringType(),  False),
    StructField("event_timestamp", StringType(),  False),
    StructField("message",         StringType(),  False),
    StructField("error_details", StructType([
        StructField("error_code",  StringType(),  True),
        StructField("error_type",  StringType(),  True),
        StructField("stack_trace", StringType(),  True),
        StructField("http_status", IntegerType(), True),
    ]), True),
    StructField("request_info", StructType([
        StructField("method",      StringType(), True),
        StructField("path",        StringType(), True),
        StructField("user_id",     StringType(), True),
        StructField("duration_ms", DoubleType(), True),
    ]), True),
    StructField("environment", StringType(), True),
])

# ─── Silver parsed_logs 扁平化 Schema ───
SILVER_APP_LOG_SCHEMA = StructType([
    StructField("log_id",               StringType(),    False),
    StructField("trace_id",             StringType(),    True),
    StructField("span_id",              StringType(),    True),
    StructField("service_name",         StringType(),    False),
    StructField("instance_id",          StringType(),    True),
    StructField("log_level",            StringType(),    False),
    StructField("event_timestamp",      TimestampType(), False),
    StructField("message",              StringType(),    True),
    StructField("error_code",           StringType(),    True),
    StructField("error_type",           StringType(),    True),
    StructField("http_status",          IntegerType(),   True),
    StructField("stack_trace",          StringType(),    True),
    StructField("req_method",           StringType(),    True),
    StructField("req_path",             StringType(),    True),
    StructField("user_id",              StringType(),    True),
    StructField("req_duration_ms",      DoubleType(),    True),
    StructField("environment",          StringType(),    True),
    StructField("ingest_timestamp",     TimestampType(), True),
    StructField("processing_timestamp", TimestampType(), True),
])

# ─── Silver enriched_clicks 扁平化 Schema ───
SILVER_CLICKSTREAM_SCHEMA = StructType([
    StructField("event_id",             StringType(),    False),
    StructField("user_id",              StringType(),    False),
    StructField("session_id",           StringType(),    True),
    StructField("event_type",           StringType(),    True),
    StructField("event_timestamp",      TimestampType(), False),
    StructField("page_url",             StringType(),    True),
    StructField("referrer_url",         StringType(),    True),
    StructField("device_type",          StringType(),    True),
    StructField("os",                   StringType(),    True),
    StructField("browser",              StringType(),    True),
    StructField("country_code",         StringType(),    True),
    StructField("city",                 StringType(),    True),
    StructField("product_id",           StringType(),    True),
    StructField("amount",               DoubleType(),    True),
    StructField("environment",          StringType(),    True),
    StructField("ingest_timestamp",     TimestampType(), True),
    StructField("processing_timestamp", TimestampType(), True),
])
