# scripts/seed_test_data.py
"""
本地开发 / CI 环境测试数据注入脚本。
在 dev 环境向 Athena（通过 S3 Parquet）和 DynamoDB 写入固定测试数据，
使 Agent 集成测试有确定性结果可断言。

运行方式：
  python scripts/seed_test_data.py --env dev
"""

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone

import boto3
import awswrangler as wr
import pandas as pd

# ─── 参数 ───
parser = argparse.ArgumentParser()
parser.add_argument("--env",    default="dev",        help="目标环境 dev|staging")
parser.add_argument("--region", default="us-east-1")
args_cli = parser.parse_args()

ENV    = args_cli.env
REGION = args_cli.region

assert ENV in ("dev", "staging"), "只允许在 dev/staging 注入测试数据"

GOLD_DB       = f"iodp_gold_{ENV}"
SILVER_DB     = f"iodp_silver_{ENV}"
GOLD_BUCKET   = f"s3://iodp-gold-{ENV}"
SILVER_BUCKET = f"s3://iodp-silver-{ENV}"
DQ_TABLE      = f"iodp_dq_reports_{ENV}"

now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
yesterday_22 = (now - timedelta(days=1)).replace(hour=22)


# ─── 1. Gold: api_error_stats（确定性数据，供 Agent 查询测试）───
def seed_gold_api_error_stats():
    rows = [
        {
            "stat_hour":       yesterday_22.isoformat(),
            "stat_date":       yesterday_22.date().isoformat(),
            "service_name":    "payment-service",
            "error_code":      "E2001",
            "total_requests":  10000,
            "error_count":     3400,
            "error_rate":      0.34,
            "p99_duration_ms": 4500.0,
            "unique_users":    980,
            "sample_trace_ids": ["trace-seed-001", "trace-seed-002", "trace-seed-003"],
            "environment":     ENV,
        },
        {
            "stat_hour":       (yesterday_22 + timedelta(hours=1)).isoformat(),
            "stat_date":       yesterday_22.date().isoformat(),
            "service_name":    "payment-service",
            "error_code":      "E2001",
            "total_requests":  9800,
            "error_count":     294,
            "error_rate":      0.03,
            "p99_duration_ms": 1200.0,
            "unique_users":    87,
            "sample_trace_ids": ["trace-seed-004"],
            "environment":     ENV,
        },
    ]
    df = pd.DataFrame(rows)
    wr.athena.to_iceberg(
        df=df,
        database=GOLD_DB,
        table="api_error_stats",
        temp_path=f"{GOLD_BUCKET}/_tmp/seed/",
        partition_cols=["stat_date", "service_name"],
        boto3_session=boto3.Session(region_name=REGION),
    )
    print(f"Seeded {len(rows)} rows -> {GOLD_DB}.api_error_stats")


# ─── 2. Silver: parsed_logs（供 v_error_log_enriched 视图 JOIN 测试）───
def seed_silver_parsed_logs():
    rows = []
    base_time = yesterday_22
    for i in range(20):
        rows.append({
            "log_id":          f"seed-log-{i:04d}",
            "trace_id":        f"trace-seed-{i:03d}",
            "span_id":         f"span-{i:03d}",
            "service_name":    "payment-service",
            "instance_id":     "i-seed001",
            "log_level":       "ERROR",
            "event_timestamp": (base_time + timedelta(minutes=i * 3)).isoformat(),
            "message":         "Payment gateway timeout",
            "error_code":      "E2001",
            "error_type":      "TimeoutException",
            "http_status":     503,
            "stack_trace":     "at com.iodp.pay.gateway.call(GW.java:88)",
            "req_method":      "POST",
            "req_path":        "/api/v1/payments",
            "user_id":         f"usr_seed_{i % 5:04d}",  # 5个不同用户循环
            "req_duration_ms": 4200.0 + i * 50,
            "environment":     ENV,
            "event_date":      base_time.date().isoformat(),
            "ingest_timestamp": base_time.isoformat(),
            "processing_timestamp": base_time.isoformat(),
        })
    df = pd.DataFrame(rows)
    wr.athena.to_iceberg(
        df=df,
        database=SILVER_DB,
        table="parsed_logs",
        temp_path=f"{SILVER_BUCKET}/_tmp/seed/",
        partition_cols=["event_date"],
        boto3_session=boto3.Session(region_name=REGION),
    )
    print(f"Seeded {len(rows)} rows -> {SILVER_DB}.parsed_logs")


# ─── 3. DynamoDB: dq_reports（数据质量正常，不触发假告警）───
def seed_dq_reports():
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table = dynamodb.Table(DQ_TABLE)
    table.put_item(Item={
        "table_name":         "bronze_app_logs",
        "report_timestamp":   yesterday_22.isoformat(),
        "job_run_id":         f"jr_seed_{uuid.uuid4().hex[:8]}",
        "batch_id":           "seed_batch_001",
        "error_type":         "NULL_USER_ID",
        "total_records":      50000,
        "failed_records":     150,
        "failure_rate":       "0.003",
        "threshold_breached": False,
        "dead_letter_path":   f"{GOLD_BUCKET}/dead_letter/seed/",
        "environment":        ENV,
        "TTL":                int(datetime.now(timezone.utc).timestamp()) + 7 * 86400,
    })
    print(f"Seeded 1 DQ report -> {DQ_TABLE}")


if __name__ == "__main__":
    print(f"Seeding test data into env={ENV}, region={REGION}")
    seed_gold_api_error_stats()
    seed_silver_parsed_logs()
    seed_dq_reports()
    print("Done. Integration tests can now run against deterministic data.")
