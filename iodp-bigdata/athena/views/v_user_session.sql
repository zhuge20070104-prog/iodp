-- athena/views/v_user_session.sql
-- 用户会话视图：把同一 session_id 的行为序列聚合成一行
-- 供 BI 工具分析用户路径和会话时长

CREATE OR REPLACE VIEW iodp_silver_prod.v_user_session AS
SELECT
    session_id,
    user_id,
    MIN(event_timestamp)                                        AS session_start,
    MAX(event_timestamp)                                        AS session_end,
    -- 会话时长（秒）
    DATE_DIFF('second', MIN(event_timestamp), MAX(event_timestamp)) AS session_duration_sec,
    COUNT(*)                                                    AS total_events,
    -- 漏斗标记：该 session 是否到达过各阶段
    MAX(CASE WHEN event_type = 'view'         THEN 1 ELSE 0 END) AS has_view,
    MAX(CASE WHEN event_type = 'add_to_cart'  THEN 1 ELSE 0 END) AS has_add_to_cart,
    MAX(CASE WHEN event_type = 'checkout'     THEN 1 ELSE 0 END) AS has_checkout,
    MAX(CASE WHEN event_type = 'purchase'     THEN 1 ELSE 0 END) AS has_purchase,
    -- 设备和地理信息（取 session 内第一条记录）
    MIN_BY(device_type,  event_timestamp)                      AS device_type,
    MIN_BY(country_code, event_timestamp)                      AS country_code,
    MIN_BY(city,         event_timestamp)                      AS city,
    -- 购买总金额
    SUM(CASE WHEN event_type = 'purchase' THEN amount ELSE 0 END) AS total_purchase_amount,
    CAST(event_timestamp AS DATE)                               AS event_date
FROM iodp_silver_prod.enriched_clicks
GROUP BY session_id, user_id, CAST(event_timestamp AS DATE)
;
