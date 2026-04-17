-- athena/ddl/silver_enriched_clicks.sql
-- Silver 层去重后的点击流（结构与 Bronze 一致，但 event_id 保证唯一）

CREATE TABLE IF NOT EXISTS iodp_silver_${ENVIRONMENT}.enriched_clicks (
    event_id             STRING        COMMENT '去重后唯一事件 ID',
    user_id              STRING        COMMENT '用户 ID（非空）',
    session_id           STRING,
    event_type           STRING,
    event_timestamp      TIMESTAMP     COMMENT '类型正确的事件时间',
    page_url             STRING,
    referrer_url         STRING,
    device_type          STRING,
    os                   STRING,
    browser              STRING,
    country_code         STRING,
    city                 STRING,
    ip_hash              STRING,
    product_id           STRING,
    amount               DOUBLE,
    environment          STRING,
    event_date           DATE          COMMENT '分区键 = event_timestamp::date',
    ingest_timestamp     TIMESTAMP,
    processing_timestamp TIMESTAMP
)
PARTITIONED BY (event_date, event_type)
LOCATION 's3://iodp-silver-${ENVIRONMENT}-${ACCOUNT_ID}/enriched_clicks/'
TBLPROPERTIES (
    'table_type'                        = 'ICEBERG',
    'format'                            = 'parquet',
    'write.parquet.compression-codec'   = 'snappy',
    'write.delete.mode'                 = 'merge-on-read',
    'write.update.mode'                 = 'merge-on-read'
);
