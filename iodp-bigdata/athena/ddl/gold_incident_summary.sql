-- athena/ddl/gold_incident_summary.sql
-- Gold 层故障事件摘要（gold_incident_summary.py 按 stat_date 分区覆盖写入）
-- 下游：S3 Event → vector_indexer Lambda → Bedrock Embedding → S3 Vectors RAG 知识库

CREATE TABLE IF NOT EXISTS iodp_gold_${ENVIRONMENT}.incident_summary (
    incident_id          STRING       COMMENT '故障 ID，格式: INC-{date}-{service}-{error_code}',
    title                STRING       COMMENT '故障标题',
    service_name         STRING       COMMENT '故障服务名称',
    error_codes          STRING       COMMENT '相关错误码 JSON 数组',
    severity             STRING       COMMENT '严重级别: P0(>=20%) / P1(>=5%) / P2',
    start_time           STRING       COMMENT '故障开始时间',
    end_time             STRING       COMMENT '故障结束时间',
    peak_error_rate      DOUBLE       COMMENT '峰值错误率',
    total_affected_users BIGINT       COMMENT '累计受影响用户数',
    peak_p99_ms          DOUBLE       COMMENT '峰值 P99 响应时间（毫秒）',
    symptoms             STRING       COMMENT '故障现象自然语言描述',
    root_cause           STRING       COMMENT '根因分析（待人工补充）',
    resolution           STRING       COMMENT '解决方案（待人工补充）',
    resolved_at          STRING       COMMENT '解决时间',
    sample_traces        STRING       COMMENT '样本 trace_id JSON 数组（最多 5 个）',
    environment          STRING,
    stat_date            STRING       COMMENT '分区键 = 故障发生日期'
)
PARTITIONED BY (stat_date)
LOCATION 's3://iodp-gold-${ENVIRONMENT}-${ACCOUNT_ID}/incident_summary/'
TBLPROPERTIES (
    'table_type'                        = 'ICEBERG',
    'format'                            = 'parquet',
    'write.parquet.compression-codec'   = 'snappy'
);
