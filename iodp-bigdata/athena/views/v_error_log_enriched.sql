-- athena/views/v_error_log_enriched.sql
-- 供 Agent Log Analyzer 工具调用
-- 输入参数：user_id (可选), time_start, time_end, service_name (可选)

CREATE OR REPLACE VIEW iodp_gold_prod.v_error_log_enriched AS
SELECT
    aes.stat_hour,
    aes.service_name,
    aes.error_code,
    aes.error_rate,
    aes.error_count,
    aes.total_requests,
    aes.p99_duration_ms,
    aes.unique_users,
    aes.sample_trace_ids,
    -- 关联 Silver 层获取具体用户受影响情况
    sl.user_id,
    sl.req_path,
    sl.req_method,
    sl.http_status,
    sl.message         AS error_message,
    sl.stack_trace,
    sl.trace_id,
    sl.event_timestamp
FROM iodp_gold_prod.api_error_stats   aes
JOIN iodp_silver_prod.parsed_logs     sl
  ON  aes.service_name = sl.service_name
  AND aes.stat_hour    = DATE_TRUNC('hour', sl.event_timestamp)
  AND aes.error_code   = sl.error_code
WHERE sl.log_level IN ('ERROR', 'FATAL')
;
