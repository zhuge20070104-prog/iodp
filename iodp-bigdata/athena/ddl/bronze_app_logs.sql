-- athena/ddl/bronze_app_logs.sql
-- Bronze 层 app_logs Iceberg 表（Glue Streaming Job stream_app_logs.py 写入前需先建表）

CREATE TABLE IF NOT EXISTS iodp_bronze_${ENVIRONMENT}.app_logs (
    log_id               STRING        COMMENT '日志唯一 ID（UUID）',
    trace_id             STRING        COMMENT '分布式追踪 trace ID',
    span_id              STRING        COMMENT '分布式追踪 span ID',
    service_name         STRING        COMMENT '服务名称',
    instance_id          STRING        COMMENT '实例 ID',
    log_level            STRING        COMMENT 'DEBUG|INFO|WARN|ERROR|FATAL',
    event_timestamp      TIMESTAMP     COMMENT '事件发生时间（UTC）',
    message              STRING        COMMENT '日志消息正文',
    error_code           STRING        COMMENT '错误码（如 E1001）',
    error_type           STRING        COMMENT '错误类型',
    http_status          INT           COMMENT 'HTTP 状态码',
    stack_trace          STRING        COMMENT '堆栈追踪信息',
    req_method           STRING        COMMENT 'HTTP 请求方法',
    req_path             STRING        COMMENT '请求路径',
    user_id              STRING        COMMENT '发起请求的用户 ID',
    req_duration_ms      DOUBLE        COMMENT '请求耗时（毫秒）',
    environment          STRING        COMMENT 'prod|staging|dev',
    ingest_timestamp     TIMESTAMP     COMMENT 'Kafka 消费时间',
    processing_timestamp TIMESTAMP     COMMENT 'Glue Job 处理时间',
    event_date           STRING        COMMENT '分区键 = date_format(event_timestamp, yyyy-MM-dd)'
)
PARTITIONED BY (event_date, log_level)
LOCATION 's3://iodp-bronze-${ENVIRONMENT}-${ACCOUNT_ID}/app_logs/'
TBLPROPERTIES (
    'table_type'                        = 'ICEBERG',
    'format'                            = 'parquet',
    'write.parquet.compression-codec'   = 'snappy',
    'write.metadata.delete-after-commit.enabled' = 'true',
    'write.metadata.previous-versions-max'       = '10'
);
