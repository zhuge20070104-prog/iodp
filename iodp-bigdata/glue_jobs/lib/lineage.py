# glue_jobs/lib/lineage.py
"""
数据血缘事件写入工具
所有 Glue Job 在处理完一个批次后调用 write_lineage_event()，
将 source→target 的数据流转记录写入 DynamoDB lineage_events 表。
"""

import logging
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)


def write_lineage_event(
    source_table: str,
    target_table: str,
    transformation: str,
    job_name: str,
    job_run_id: str,
    record_count_in: int,
    record_count_out: int,
    record_count_dead_letter: int,
    lineage_table: str,
    duration_seconds: int = 0,
    aws_region: str = "us-east-1",
) -> None:
    """
    写入一条血缘事件到 DynamoDB。

    PK: source_table
    SK: event_time#target_table  （保证同一 source 的多次 target 可区分）
    """
    dynamodb = boto3.resource("dynamodb", region_name=aws_region)
    table = dynamodb.Table(lineage_table)

    now_iso = datetime.now(timezone.utc).isoformat()
    sk = f"{now_iso}#{target_table.split('/')[-2] if '/' in target_table else target_table}"
    ttl_seconds = int(datetime.now(timezone.utc).timestamp()) + 180 * 86400  # 180 天 TTL

    try:
        table.put_item(Item={
            "source_table":       source_table,
            "event_time":         sk,
            "target_table":       target_table,
            "transformation":     transformation,
            "job_name":           job_name,
            "job_run_id":         job_run_id,
            "record_count_in":    record_count_in,
            "record_count_out":   record_count_out,
            "record_count_dl":    record_count_dead_letter,
            "duration_seconds":   duration_seconds,
            "TTL":                ttl_seconds,
        })
        logger.info(
            "Lineage event written: %s → %s (%d → %d records)",
            source_table, target_table, record_count_in, record_count_out,
        )
    except Exception as e:
        # 血缘写入失败不中断主流程
        logger.error("Failed to write lineage event: %s", e)
