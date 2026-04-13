# lambda/dlq_replay/handler.py
"""
DLQ 死信数据重处理 Lambda
触发方式：手动 CLI 调用 或 EventBridge（默认 DISABLED）

Event payload:
  {
    "table_name": "bronze_app_logs",   # 要重处理的表名
    "batch_date": "2026-04-06",        # 要重处理的日期
    "dry_run": true                    # true=只统计，false=实际复制
  }

Response:
  {
    "replayed_files": 12,
    "total_bytes": 45678901,
    "dry_run": true,
    "source_prefix": "dead_letter/table=bronze_app_logs/batch_id=*/date=2026-04-06/",
    "dest_prefix": "replay/bronze_app_logs/2026-04-06/"
  }
"""

import json
import logging
import os
from typing import Any, Dict, List

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BRONZE_BUCKET = os.environ["BRONZE_BUCKET"]
DLQ_PREFIX    = os.environ.get("DLQ_PREFIX", "dead_letter/")
REPLAY_PREFIX = os.environ.get("REPLAY_PREFIX", "replay/")
ENVIRONMENT   = os.environ.get("ENVIRONMENT", "prod")

s3 = boto3.client("s3")


def _list_dead_letter_files(table_name: str, batch_date: str) -> List[Dict[str, Any]]:
    """
    列出指定表名和日期下所有死信 Parquet 文件。
    路径格式：dead_letter/table={table_name}/batch_id=*/date={batch_date}/*.parquet
    """
    prefix = f"{DLQ_PREFIX}table={table_name}/"
    files = []

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BRONZE_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # 过滤出目标日期的文件
            if f"date={batch_date}/" in key and key.endswith(".parquet"):
                files.append({
                    "key": key,
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                })

    logger.info(
        "Found %d dead-letter files for table=%s date=%s",
        len(files), table_name, batch_date,
    )
    return files


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    主入口函数。
    dry_run=True：只统计文件数量和大小，不移动任何数据。
    dry_run=False：将死信文件复制到 replay/ 前缀，供 Glue Job 重新消费。
    """
    table_name = event.get("table_name")
    batch_date = event.get("batch_date")
    dry_run    = event.get("dry_run", True)  # 默认安全：dry_run 模式

    if not table_name or not batch_date:
        raise ValueError("Event must contain 'table_name' and 'batch_date'")

    logger.info(
        "DLQ Replay started: table=%s date=%s dry_run=%s env=%s",
        table_name, batch_date, dry_run, ENVIRONMENT,
    )

    files = _list_dead_letter_files(table_name, batch_date)

    if not files:
        logger.warning("No dead-letter files found for table=%s date=%s", table_name, batch_date)
        return {
            "replayed_files": 0,
            "total_bytes": 0,
            "dry_run": dry_run,
            "source_prefix": f"{DLQ_PREFIX}table={table_name}/batch_id=*/date={batch_date}/",
            "dest_prefix": f"{REPLAY_PREFIX}{table_name}/{batch_date}/",
            "message": "No files found",
        }

    total_bytes = sum(f["size"] for f in files)

    if dry_run:
        logger.info(
            "[DRY RUN] Would replay %d files (%.2f MB) for table=%s date=%s",
            len(files), total_bytes / 1_048_576, table_name, batch_date,
        )
        for f in files:
            logger.info("  [DRY RUN] %s (%d bytes)", f["key"], f["size"])
        return {
            "replayed_files": len(files),
            "total_bytes": total_bytes,
            "dry_run": True,
            "files": [f["key"] for f in files],
            "source_prefix": f"{DLQ_PREFIX}table={table_name}/",
            "dest_prefix": f"{REPLAY_PREFIX}{table_name}/{batch_date}/",
        }

    # ─── 实际复制 ───
    replayed = 0
    failed_files = []
    dest_prefix = f"{REPLAY_PREFIX}{table_name}/{batch_date}/"

    for file_info in files:
        src_key = file_info["key"]
        # 保留原始文件名，去掉 dead_letter/ 前缀
        relative_key = src_key.replace(f"{DLQ_PREFIX}table={table_name}/", "")
        dest_key = f"{dest_prefix}{relative_key}"

        try:
            s3.copy_object(
                Bucket=BRONZE_BUCKET,
                CopySource={"Bucket": BRONZE_BUCKET, "Key": src_key},
                Key=dest_key,
            )
            logger.info("Copied %s → %s", src_key, dest_key)
            replayed += 1
        except Exception as e:
            logger.error("Failed to copy %s: %s", src_key, e)
            failed_files.append(src_key)

    logger.info(
        "DLQ Replay completed: %d/%d files replayed to s3://%s/%s",
        replayed, len(files), BRONZE_BUCKET, dest_prefix,
    )

    if failed_files:
        logger.warning("Failed to replay %d files: %s", len(failed_files), failed_files)

    return {
        "replayed_files": replayed,
        "failed_files": len(failed_files),
        "total_bytes": total_bytes,
        "dry_run": False,
        "dest_prefix": dest_prefix,
        "message": (
            f"Replayed {replayed}/{len(files)} files to {dest_prefix}. "
            f"Downstream Glue Job should be triggered manually to reprocess."
        ),
    }
