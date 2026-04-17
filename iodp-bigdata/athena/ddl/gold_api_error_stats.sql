-- athena/ddl/gold_api_error_stats.sql
-- Gold 层 API 错误率聚合表

CREATE TABLE IF NOT EXISTS iodp_gold_${ENVIRONMENT}.api_error_stats (
    stat_hour         TIMESTAMP    COMMENT '统计小时（整点，UTC）',
    service_name      STRING       COMMENT '服务名称',
    error_code        STRING       COMMENT '错误码',
    total_requests    BIGINT       COMMENT '该小时该服务总请求数',
    error_count       BIGINT       COMMENT '该错误码出现次数',
    error_rate        DOUBLE       COMMENT '错误率 = error_count / total_requests',
    p99_duration_ms   DOUBLE       COMMENT 'P99 响应时间（毫秒，仅统计出错的请求，不是服务整体 p99）',
    unique_users      BIGINT       COMMENT '受影响的去重用户数',
    sample_trace_ids  ARRAY<STRING> COMMENT '最多 5 个 trace_id 样本',
    environment       STRING,
    stat_date         DATE         COMMENT '分区键'
)
PARTITIONED BY (stat_date, service_name)
LOCATION 's3://iodp-gold-${ENVIRONMENT}-${ACCOUNT_ID}/api_error_stats/'
TBLPROPERTIES (
    'table_type'                        = 'ICEBERG',
    'format'                            = 'parquet',
    'write.parquet.compression-codec'   = 'snappy'
);
