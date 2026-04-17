-- athena/ddl/silver_parsed_logs.sql
-- Silver 层去重后的应用日志（silver_parse_logs.py 通过 MERGE INTO 写入，log_id 保证唯一）

CREATE TABLE IF NOT EXISTS iodp_silver_${ENVIRONMENT}.parsed_logs (
    log_id               STRING        COMMENT '去重后唯一日志 ID',
    trace_id             STRING        COMMENT '分布式追踪 trace ID',
    span_id              STRING        COMMENT '分布式追踪 span ID',
    service_name         STRING        COMMENT '服务名称',
    instance_id          STRING        COMMENT '实例 ID',
    log_level            STRING        COMMENT 'DEBUG|INFO|WARN|ERROR|FATAL',
    event_timestamp      TIMESTAMP     COMMENT '事件发生时间（UTC）',
    message              STRING        COMMENT '日志消息正文',
    error_code           STRING        COMMENT '错误码',
    error_type           STRING        COMMENT '错误类型',
    http_status          INT           COMMENT 'HTTP 状态码（类型校正后）',
    stack_trace          STRING        COMMENT '堆栈追踪信息',
    req_method           STRING        COMMENT 'HTTP 请求方法',
    req_path             STRING        COMMENT '请求路径',
    user_id              STRING        COMMENT '发起请求的用户 ID',
    req_duration_ms      DOUBLE        COMMENT '请求耗时（毫秒，类型校正后）',
    environment          STRING        COMMENT 'prod|staging|dev',
    event_date           DATE          COMMENT '分区键 = event_timestamp::date',
    ingest_timestamp     TIMESTAMP,
    processing_timestamp TIMESTAMP
)
PARTITIONED BY (event_date)
LOCATION 's3://iodp-silver-${ENVIRONMENT}-${ACCOUNT_ID}/parsed_logs/'
TBLPROPERTIES (
    'table_type'                        = 'ICEBERG',
    'format'                            = 'parquet',
    'write.parquet.compression-codec'   = 'snappy',
    'write.delete.mode'                 = 'merge-on-read',
    'write.update.mode'                 = 'merge-on-read'
);
