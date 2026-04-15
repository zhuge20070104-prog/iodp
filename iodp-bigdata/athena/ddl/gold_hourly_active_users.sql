-- athena/ddl/gold_hourly_active_users.sql
-- Gold 层每小时活跃用户漏斗统计（gold_hourly_active_users.py 按 stat_date 分区覆盖写入）

CREATE TABLE IF NOT EXISTS iodp_gold_prod.hourly_active_users (
    stat_hour           TIMESTAMP    COMMENT '统计小时（整点，UTC）',
    active_users        BIGINT       COMMENT '该小时有任意行为的去重用户数',
    view_users          BIGINT       COMMENT '有浏览行为的用户数',
    add_to_cart_users   BIGINT       COMMENT '有加购行为的用户数',
    checkout_users      BIGINT       COMMENT '进入结算的用户数',
    purchase_users      BIGINT       COMMENT '完成购买的用户数',
    new_users           BIGINT       COMMENT '首次出现的用户数（该小时前无历史记录）',
    environment         STRING,
    stat_date           DATE         COMMENT '分区键 = stat_hour::date'
)
PARTITIONED BY (stat_date)
LOCATION 's3://iodp-gold-prod/hourly_active_users/'
TBLPROPERTIES (
    'table_type'                        = 'ICEBERG',
    'format'                            = 'parquet',
    'write.parquet.compression-codec'   = 'snappy'
);
