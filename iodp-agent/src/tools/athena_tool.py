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
from src.log_utils import Timer, compress_sql, log_event

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

    # 兼容两种格式：
    #   1) "iodp-agent-dev-athena-results"        （纯 bucket name）
    #   2) "s3://iodp-agent-dev-athena-results/"  （完整 URL，env var 注入时通常是这格式）
    # 之前直接 f"s3://{output_bucket}/..." 会拼成 "s3://s3://..."，Athena 报 InvalidBucketName。
    if output_bucket.startswith("s3://"):
        out_location = output_bucket.rstrip("/") + "/athena-results/"
    else:
        out_location = f"s3://{output_bucket.rstrip('/')}/athena-results/"

    compressed_sql = compress_sql(sql)
    log_event(
        "athena", "start",
        database=database, workgroup=workgroup,
        output_location=out_location, max_rows=max_rows,
        sql=compressed_sql,
    )

    with Timer() as total_t:
        response = client.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": database},
            ResultConfiguration={"OutputLocation": out_location},
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
                log_event(
                    "athena", "error",
                    query_execution_id=query_execution_id,
                    state=state, reason=reason,
                    elapsed_ms=int((time.perf_counter() - total_t._start) * 1000),
                )
                raise RuntimeError(f"Athena query {state}: {reason}")

            time.sleep(poll_interval)
            waited += poll_interval
        else:
            log_event(
                "athena", "timeout",
                query_execution_id=query_execution_id,
                max_wait_seconds=max_wait_seconds,
            )
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

    log_event(
        "athena", "success",
        query_execution_id=query_execution_id,
        rows=min(total_rows, max_rows),
        rows_truncated=rows_truncated,
        total_rows_fetched=total_rows,
        elapsed_ms=total_t.elapsed_ms,
    )

    return {
        "rows":                         all_rows[:max_rows],
        "query_execution_id":           query_execution_id,
        "rows_truncated":               rows_truncated,
        "total_rows_before_truncation": total_rows,
    }
