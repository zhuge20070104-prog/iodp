# src/tools/dynamodb_tool.py
"""
封装 DynamoDB 查询工具，供 Log Analyzer Agent 查询项目一的 dq_reports 表，
判断指定时段的数据质量是否异常（排除假告警）。
"""

from typing import Any, Dict, Optional
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

from src.config import settings


def query_dq_reports(
    table_name: str,
    time_start: str,
    time_end: str,
    dynamodb_table: str = "",
    aws_region: str = "us-east-1",
) -> Optional[Dict[str, Any]]:
    """
    查询指定表在指定时段内是否有 DQ 阈值突破的告警记录。
    返回最严重的一条记录，若无异常则返回 None。

    table_name:  Bronze 表名，如 "bronze_app_logs"
    time_start/end: ISO 格式时间字符串
    """
    ddb_table_name = dynamodb_table or settings.dq_reports_table
    dynamodb = boto3.resource("dynamodb", region_name=aws_region)
    table = dynamodb.Table(ddb_table_name)

    try:
        response = table.query(
            KeyConditionExpression=(
                Key("table_name").eq(table_name) &
                Key("report_timestamp").between(time_start, time_end)
            ),
            FilterExpression="threshold_breached = :val",
            ExpressionAttributeValues={":val": True},
            ScanIndexForward=False,  # 最新的在前
            Limit=1,
        )
        items = response.get("Items", [])
        if items:
            item = items[0]
            return {
                "table_name":         item.get("table_name"),
                "report_timestamp":   item.get("report_timestamp"),
                "error_type":         item.get("error_type"),
                "failure_rate":       float(item.get("failure_rate", 0)),
                "total_records":      int(item.get("total_records", 0)),
                "failed_records":     int(item.get("failed_records", 0)),
                "threshold_breached": item.get("threshold_breached", False),
                "dead_letter_path":   item.get("dead_letter_path", ""),
            }
        return None
    except Exception as e:
        # DQ 查询失败不中断 Agent 主流程，记录日志后返回 None
        import logging
        logging.getLogger(__name__).error("DQ report query failed: %s", e)
        return None
