#!/usr/bin/env python3
"""
向 Kinesis Data Firehose 推送示例事件（替代旧 Kafka producer）。

Direct PUT 模式：producer 直接 boto3 调 Firehose put_record_batch，无需 VPC、
无需 broker bootstrap、无需 SASL/IAM 认证握手 —— 纯 HTTPS。

用法：
    # 推 1000 条点击流到 iodp-clickstream-dev
    python scripts/produce_sample_events.py \\
        --stream clickstream --env dev --count 1000

    # 推 app_logs，含 5% 错误率（用于触发 DQ 演示）
    python scripts/produce_sample_events.py \\
        --stream app_logs --env dev --count 500 --error-rate 0.05

每条 record 末尾追加 "\\n"，让 Athena 后续可以按 NDJSON 直读。
"""

import argparse
import json
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3


VALID_EVENT_TYPES = ["click", "view", "scroll", "purchase", "add_to_cart", "checkout"]
VALID_ERROR_CODES = ["E1001", "E1002", "E2001", "E2002", "E3001", "E4001"]
VALID_ERROR_TYPES = [
    "TimeoutException", "NullPointerException", "ConnectionRefused",
    "IllegalArgumentException", "ResourceExhausted", "DownstreamUnavailable",
]
SERVICES = ["payment-service", "user-service", "search-service", "checkout-service"]


def _make_clickstream_event() -> Dict[str, Any]:
    return {
        "event_id":        str(uuid.uuid4()),
        "user_id":         f"usr_{random.randint(10_000_000, 99_999_999)}",
        "session_id":      str(uuid.uuid4()),
        "event_type":      random.choice(VALID_EVENT_TYPES),
        "event_timestamp": datetime.now(timezone.utc).isoformat(),
        "page_url":        f"/page/{random.randint(1, 100)}",
        "referrer_url":    "/home",
        "device_info":     {"device_type": "mobile", "os": "iOS", "browser": "Safari"},
        "geo_info":        {"country_code": "US", "city": "NYC", "ip_hash": "abc123"},
        "properties":      {"product_id": f"prod_{random.randint(1, 1000)}", "amount": round(random.uniform(0, 500), 2)},
        "environment":     "demo",
    }


def _make_app_log_event(error_rate: float) -> Dict[str, Any]:
    """字段名与 Silver DDL parsed_logs 表完全对齐。
    历史字段名（error_message / duration_ms）已废弃，Silver 侧仍通过 coalesce 兼容旧 Bronze 数据。
    """
    is_error = random.random() < error_rate
    return {
        "log_id":          str(uuid.uuid4()),
        "trace_id":        str(uuid.uuid4()),
        "span_id":         uuid.uuid4().hex[:16],
        "service_name":    random.choice(SERVICES),
        "instance_id":     f"i-{uuid.uuid4().hex[:8]}",
        "log_level":       "ERROR" if is_error else "INFO",
        "event_timestamp": datetime.now(timezone.utc).isoformat(),
        "message":         "Downstream timeout while calling backend" if is_error else "Request OK",
        "error_code":      random.choice(VALID_ERROR_CODES) if is_error else None,
        "error_type":      random.choice(VALID_ERROR_TYPES) if is_error else None,
        "http_status":     500 if is_error else 200,
        "stack_trace":     "Traceback...\n  ..." if is_error else None,
        "req_method":      random.choice(["GET", "POST", "PUT"]),
        "req_path":        f"/api/v1/resource/{random.randint(1, 50)}",
        "user_id":         f"usr_{random.randint(10_000_000, 99_999_999)}",
        "req_duration_ms": random.randint(50, 800),
        "environment":     "demo",
    }


def _flush(firehose, stream_name: str, batch: List[Dict[str, bytes]]) -> None:
    """Firehose put_record_batch 单次最多 500 条 / 4 MB。"""
    if not batch:
        return
    resp = firehose.put_record_batch(DeliveryStreamName=stream_name, Records=batch)
    failed = resp.get("FailedPutCount", 0)
    if failed:
        print(f"  ⚠️  {failed} records failed; will retry", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--stream", choices=["clickstream", "app_logs"], required=True)
    p.add_argument("--env", required=True, help="dev / staging / prod")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--error-rate", type=float, default=0.02, help="app_logs only")
    p.add_argument("--batch-size", type=int, default=500)
    args = p.parse_args()

    stream_name = f"iodp-{args.stream}-{args.env}"
    firehose = boto3.client("firehose", region_name=args.region)

    print(f"▶ Producing {args.count} events → {stream_name} (region={args.region})")
    sent = 0
    batch: List[Dict[str, bytes]] = []

    for i in range(args.count):
        event = (
            _make_clickstream_event()
            if args.stream == "clickstream"
            else _make_app_log_event(args.error_rate)
        )
        # 行尾 "\n" 让 S3 文件成为 NDJSON，方便 Athena/Glue 直读
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        batch.append({"Data": data})

        if len(batch) >= args.batch_size:
            _flush(firehose, stream_name, batch)
            sent += len(batch)
            print(f"  sent {sent}/{args.count}")
            batch = []
            time.sleep(0.05)  # 让 Firehose 平稳消化

    _flush(firehose, stream_name, batch)
    sent += len(batch)
    print(f"✅ Done. Sent {sent} records.")


if __name__ == "__main__":
    main()
