# src/tools/schema_tool.py
"""
封装 Glue Data Catalog 查询工具。
Log Analyzer Agent 在生成 SQL 前可以调用此工具，
获取目标表的字段列表和类型，避免 LLM 生成使用不存在字段的 SQL。
"""

from typing import Any, Dict, List

import boto3

from src.config import settings


def get_table_schema(
    database: str,
    table: str,
    aws_region: str = "us-east-1",
) -> Dict[str, Any]:
    """
    从 Glue Data Catalog 获取指定表的字段信息。
    返回格式：
    {
        "database": "iodp_gold_prod",
        "table":    "api_error_stats",
        "columns":  [{"name": "stat_hour", "type": "timestamp"}, ...]
    }
    """
    glue = boto3.client("glue", region_name=aws_region)
    response = glue.get_table(DatabaseName=database, Name=table)
    storage_desc = response["Table"]["StorageDescriptor"]

    columns: List[Dict[str, str]] = [
        {"name": col["Name"], "type": col["Type"]}
        for col in storage_desc.get("Columns", [])
    ]
    # 追加分区键
    partition_keys = response["Table"].get("PartitionKeys", [])
    for pk in partition_keys:
        columns.append({"name": pk["Name"], "type": pk["Type"], "is_partition": True})

    return {
        "database": database,
        "table":    table,
        "columns":  columns,
        "location": storage_desc.get("Location", ""),
        "table_type": response["Table"].get("TableType", ""),
    }


def list_available_tables(
    database: str,
    aws_region: str = "us-east-1",
) -> List[str]:
    """列出指定 Glue Database 中的所有表名"""
    glue = boto3.client("glue", region_name=aws_region)
    paginator = glue.get_paginator("get_tables")
    tables = []
    for page in paginator.paginate(DatabaseName=database):
        tables.extend([t["Name"] for t in page.get("TableList", [])])
    return tables


def schema_summary_for_llm(database: str, table: str) -> str:
    """
    生成适合直接注入 LLM Prompt 的 Schema 摘要文本。
    格式：
      Table: iodp_gold_prod.api_error_stats
      Columns:
        - stat_hour (timestamp) [partition]
        - service_name (string)
        ...
    """
    info = get_table_schema(database, table)
    lines = [f"Table: {info['database']}.{info['table']}", "Columns:"]
    for col in info["columns"]:
        suffix = " [partition]" if col.get("is_partition") else ""
        lines.append(f"  - {col['name']} ({col['type']}){suffix}")
    return "\n".join(lines)
