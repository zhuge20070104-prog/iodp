# src/tools/athena_tool.py
"""
Athena 查询工具 v2
改进：
  - 新增 max_rows 参数，默认截断到 settings.athena_max_rows（50行）
  - 返回 rows_truncated 字段，便于上层节点记录
  - 使用 logger 替换 print
  - 加强 SQL 安全校验（WITH 子句也允许）
"""

import logging
import re
import time
from typing import Any, Dict, Optional

import boto3

from src.config import settings

logger = logging.getLogger(__name__)

_PROHIBITED_KEYWORDS = re.compile(
    r"\b(DROP|DELETE|INSERT|UPDATE|CREATE|ALTER|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _validate_sql_safety(sql: str) -> None:
    """拒绝非 SELECT/WITH 语句，防止 Agent 生成危险 SQL"""
    stripped = sql.strip().upper()
    if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
        raise ValueError(f"Only SELECT/WITH queries are allowed. Got: {stripped[:50]}")
    if _PROHIBITED_KEYWORDS.search(sql):
        match = _PROHIBITED_KEYWORDS.search(sql)
        raise ValueError(f"SQL contains prohibited keyword: {match.group()}")


def execute_athena_query(
    sql: str,
    database: str,
    output_bucket: str,
    workgroup: str = "primary",
    max_wait_seconds: int = 60,
    max_rows: int = 50,
) -> Dict[str, Any]:
    """
    执行 Athena 查询，等待完成，返回结果行列表（截断到 max_rows）

    返回格式：
    {
      "rows": [{"col1": "val1", ...}, ...],
      "query_execution_id": "...",
      "rows_truncated": False,     # True 表示结果被截断
      "total_rows_before_truncation": N
    }
    """
    _validate_sql_safety(sql)

    client = boto3.client("athena", region_name=settings.aws_region)

    response = client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": database},
        ResultConfiguration={"OutputLocation": f"s3://{output_bucket}/athena-results/"},
        WorkGroup=workgroup,
    )
    query_execution_id = response["QueryExecutionId"]

    # 轮询等待完成
    waited       = 0
    poll_interval = 2
    while waited < max_wait_seconds:
        status_response = client.get_query_execution(QueryExecutionId=query_execution_id)
        state = status_response["QueryExecution"]["Status"]["State"]

        if state == "SUCCEEDED":
            break
        elif state in ("FAILED", "CANCELLED"):
            reason = status_response["QueryExecution"]["Status"].get("StateChangeReason", "")
            raise RuntimeError(f"Athena query {state}: {reason}")

        time.sleep(poll_interval)
        waited += poll_interval
    else:
        raise TimeoutError(f"Athena query timed out after {max_wait_seconds}s")

    # 获取结果（只取第一页，避免内存/Token 溢出）
    result_response = client.get_query_results(
        QueryExecutionId=query_execution_id,
        MaxResults=max_rows + 1,   # +1 用于判断是否被截断
    )
    column_info = result_response["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
    columns     = [col["Name"] for col in column_info]

    all_rows = []
    # 第一行是列头，跳过
    for row in result_response["ResultSet"]["Rows"][1:]:
        row_data = {}
        for i, cell in enumerate(row["Data"]):
            if i < len(columns):
                row_data[columns[i]] = cell.get("VarCharValue")
        all_rows.append(row_data)

    total_rows    = len(all_rows)
    rows_truncated = total_rows > max_rows

    if rows_truncated:
        logger.warning(
            "Athena result truncated: fetched %d rows, returning first %d "
            "(query_execution_id=%s). Increase athena_max_rows or add stricter WHERE clause.",
            total_rows, max_rows, query_execution_id,
        )

    return {
        "rows":                         all_rows[:max_rows],
        "query_execution_id":           query_execution_id,
        "rows_truncated":               rows_truncated,
        "total_rows_before_truncation": total_rows,
    }
