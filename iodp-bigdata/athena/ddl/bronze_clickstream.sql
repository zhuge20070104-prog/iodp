-- athena/ddl/bronze_clickstream.sql
-- Bronze 层 clickstream Iceberg 表（Glue Streaming Job 写入前需先建表）

CREATE TABLE IF NOT EXISTS iodp_bronze_${ENVIRONMENT}.clickstream (
    event_id             STRING        COMMENT 'Kafka 消息唯一 ID',
    user_id              STRING        COMMENT '用户 ID',
    session_id           STRING        COMMENT '会话 ID',
    event_type           STRING        COMMENT 'click|view|scroll|purchase|add_to_cart|checkout',
    event_timestamp      TIMESTAMP     COMMENT '事件发生时间（UTC）',
    page_url             STRING        COMMENT '当前页面 URL',
    referrer_url         STRING        COMMENT '来源页面 URL',
    device_type          STRING        COMMENT 'mobile|desktop|tablet',
    os                   STRING        COMMENT '操作系统',
    browser              STRING        COMMENT '浏览器',
    country_code         STRING        COMMENT '国家代码 ISO 3166-1 alpha-2',
    city                 STRING        COMMENT '城市',
    ip_hash              STRING        COMMENT 'IP 地址哈希（脱敏）',
    product_id           STRING        COMMENT '商品 ID（如有）',
    amount               DOUBLE        COMMENT '金额（如有）',
    environment          STRING        COMMENT 'prod|staging|dev',
    ingest_timestamp     TIMESTAMP     COMMENT 'Kafka 消费时间',
    processing_timestamp TIMESTAMP     COMMENT 'Glue Job 处理时间'
)
PARTITIONED BY (event_type)
LOCATION 's3://iodp-bronze-${ENVIRONMENT}-${ACCOUNT_ID}/clickstream/'
TBLPROPERTIES (
    'table_type'                        = 'ICEBERG',
    'format'                            = 'parquet',
    'write.parquet.compression-codec'   = 'snappy',
    'write.metadata.delete-after-commit.enabled' = 'true',
    'write.metadata.previous-versions-max'       = '10'
);
