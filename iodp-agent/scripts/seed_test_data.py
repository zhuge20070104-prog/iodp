# scripts/seed_test_data.py
"""
本地开发 / CI 环境测试数据注入脚本。
在 dev 环境向 Athena（通过 S3 Parquet）和 DynamoDB 写入固定测试数据，
使 Agent 集成测试有确定性结果可断言。

两种模式：
  --mode full      （默认）直接把 Silver parsed_logs + Gold api_error_stats +
                   incident_summary + DQ reports 全部 mock 写入。
                   适合：跳过 bigdata Glue 流水线的快速 CI / 本地集成测试。
                   耗时 ~30s，零 Glue 成本。

  --mode rag-only  只 mock incident_summary（RAG 知识库源）+ DQ reports。
                   适合：搭配 bigdata `make seed-production`：Silver/Gold 由真实
                   Glue 流水线产生，incident_summary 因依赖 24h 累积数据无法走
                   流水线，DQ reports 因 demo 数据量小不一定触发阈值，靠本模式补齐。

运行方式：
  python scripts/seed_test_data.py --env dev --mode full
  python scripts/seed_test_data.py --env dev --mode rag-only
"""

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone

import boto3
import awswrangler as wr
import pandas as pd

# 每次运行独立 staging 路径前缀，避免不同表 schema 冲突时互相污染
_RUN_ID = uuid.uuid4().hex[:8]

# ─── 参数 ───
parser = argparse.ArgumentParser()
parser.add_argument("--env",    default="dev",        help="目标环境 dev|staging")
parser.add_argument("--region", default="us-east-1")
parser.add_argument("--mode",   default="full", choices=["full", "rag-only"],
                    help="full=全部 mock（默认）; rag-only=仅 incident_summary+DQ "
                         "（搭配 bigdata 真实 Glue 流水线使用）")
args_cli = parser.parse_args()

ENV    = args_cli.env
REGION = args_cli.region
MODE   = args_cli.mode

assert ENV in ("dev", "staging"), "只允许在 dev/staging 注入测试数据"

# bigdata terraform 把 account_id 嵌入 S3 bucket 名以保证全局唯一
ACCOUNT_ID = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]

GOLD_DB       = f"iodp_gold_{ENV}"
SILVER_DB     = f"iodp_silver_{ENV}"
GOLD_BUCKET   = f"s3://iodp-gold-{ENV}-{ACCOUNT_ID}"
SILVER_BUCKET = f"s3://iodp-silver-{ENV}-{ACCOUNT_ID}"
DQ_TABLE      = f"iodp-dq-reports-{ENV}"   # bigdata DynamoDB 表用连字符

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
    # cast 字符串到表 DDL 声明的 TIMESTAMP / DATE 类型，避免 awswrangler 抛 schema mismatch
    df["stat_hour"] = pd.to_datetime(df["stat_hour"])
    df["stat_date"] = pd.to_datetime(df["stat_date"]).dt.date
    wr.athena.to_iceberg(
        df=df,
        database=GOLD_DB,
        table="api_error_stats",
        temp_path=f"{GOLD_BUCKET}/_tmp/seed/api_error_stats/{_RUN_ID}/",
        partition_cols=["stat_date", "service_name"],
        keep_files=False,   # 写完自动清 staging parquet，防止下次 schema 冲突
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
    # cast 4 个时间列匹配 DDL: event_timestamp/ingest_timestamp/processing_timestamp = TIMESTAMP, event_date = DATE
    df["event_timestamp"]      = pd.to_datetime(df["event_timestamp"])
    df["ingest_timestamp"]     = pd.to_datetime(df["ingest_timestamp"])
    df["processing_timestamp"] = pd.to_datetime(df["processing_timestamp"])
    df["event_date"]           = pd.to_datetime(df["event_date"]).dt.date
    # http_status DDL 是 INT (int32), pandas 默认推断为 int64
    df["http_status"] = df["http_status"].astype("int32")
    wr.athena.to_iceberg(
        df=df,
        database=SILVER_DB,
        table="parsed_logs",
        temp_path=f"{SILVER_BUCKET}/_tmp/seed/parsed_logs/{_RUN_ID}/",
        partition_cols=["event_date"],
        keep_files=False,
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


# ─── 4. Gold: incident_summary（RAG 知识库源数据，给 index_knowledge_base.py 读）───
def seed_gold_incident_summary():
    """灌 3 条历史已解决故障案例，让 `make index-kb` 能 embed 到 S3 Vectors 供 RAG 检索"""
    rows = [
        {
            "incident_id":          "INC-2026-05-15-payment-E2001",
            "title":                "支付网关大面积超时导致 E2001",
            "service_name":         "payment-service",
            "error_codes":          json.dumps(["E2001"]),
            "severity":             "P1",
            "start_time":           (yesterday_22 - timedelta(days=2)).isoformat(),
            "end_time":             (yesterday_22 - timedelta(days=2) + timedelta(hours=1)).isoformat(),
            "peak_error_rate":      0.42,
            "total_affected_users": 1230,
            "peak_p99_ms":          5800.0,
            "symptoms":             "用户支付页面 loading 不响应，后端日志显示 Payment gateway timeout，trace 显示在调用第三方支付网关时阻塞",
            "root_cause":           "数据库连接池在大促峰值流量下被打满（max-pool-size=50 不够），导致 payment-service 调外部网关时 DB session 拿不到连接，等到 5s 后超时",
            "resolution":           "扩容连接池 max-pool-size 50→200；新增 P95 等待时间 > 200ms 时告警；payment-service 加 hystrix circuit breaker 降级",
            "resolved_at":          (yesterday_22 - timedelta(days=2) + timedelta(hours=1, minutes=30)).isoformat(),
            "sample_traces":        json.dumps(["trace-hist-0001", "trace-hist-0002"]),
            "environment":          ENV,
            "stat_date":            (yesterday_22 - timedelta(days=2)).date().isoformat(),
        },
        {
            "incident_id":          "INC-2026-04-28-payment-E2003",
            "title":                "支付回调失败 E2003",
            "service_name":         "payment-service",
            "error_codes":          json.dumps(["E2003"]),
            "severity":             "P2",
            "start_time":           (yesterday_22 - timedelta(days=18)).isoformat(),
            "end_time":             (yesterday_22 - timedelta(days=18) + timedelta(minutes=45)).isoformat(),
            "peak_error_rate":      0.08,
            "total_affected_users": 230,
            "peak_p99_ms":          2100.0,
            "symptoms":             "用户付款成功但订单状态未更新；payment-service 收到第三方回调但写订单库时报 E2003",
            "root_cause":           "订单库主从延迟，回调写入瞬间 master 还没同步到从库，订单查询走从库时找不到记录被判定为失败",
            "resolution":           "回调写入路径强制读 master；新增主从延迟监控；订单状态查询逻辑加 read-your-writes 一致性保证",
            "resolved_at":          (yesterday_22 - timedelta(days=18) + timedelta(hours=2)).isoformat(),
            "sample_traces":        json.dumps(["trace-hist-0010"]),
            "environment":          ENV,
            "stat_date":            (yesterday_22 - timedelta(days=18)).date().isoformat(),
        },
        {
            "incident_id":          "INC-2026-03-10-checkout-E5001",
            "title":                "结算页 NPE 导致 E5001",
            "service_name":         "checkout-service",
            "error_codes":          json.dumps(["E5001"]),
            "severity":             "P2",
            "start_time":           (yesterday_22 - timedelta(days=68)).isoformat(),
            "end_time":             (yesterday_22 - timedelta(days=68) + timedelta(minutes=20)).isoformat(),
            "peak_error_rate":      0.15,
            "total_affected_users": 540,
            "peak_p99_ms":          800.0,
            "symptoms":             "用户点结算按钮白屏，前端报 500；后端 checkout-service NullPointerException",
            "root_cause":           "新上线的优惠券模块，未登录用户的 coupon_list 为 null，结算计算时未做 null check",
            "resolution":           "checkout-service 加 null 防御；前端在 coupon 为空时给默认空数组；CI 加 NPE 静态扫描",
            "resolved_at":          (yesterday_22 - timedelta(days=68) + timedelta(hours=1)).isoformat(),
            "sample_traces":        json.dumps(["trace-hist-0020"]),
            "environment":          ENV,
            "stat_date":            (yesterday_22 - timedelta(days=68)).date().isoformat(),
        },
        # ─── 以下 4 条覆盖 producer pool 里剩余的 error_code ───
        # 之前只 seed 了 E2001/E2003/E5001，但 producer 随机抽到 E2002/E1xxx/E3xxx/
        # E4xxx 时 RAG filter_error_codes 精确匹配 → 0 命中。这 4 条把主要类别都覆盖。
        {
            "incident_id":          "INC-2026-05-10-payment-E2002",
            "title":                "支付下游超时雪崩 E2002",
            "service_name":         "payment-service",
            "error_codes":          json.dumps(["E2002"]),
            "severity":             "P2",
            "start_time":           (yesterday_22 - timedelta(days=8)).isoformat(),
            "end_time":             (yesterday_22 - timedelta(days=8) + timedelta(minutes=35)).isoformat(),
            "peak_error_rate":      0.12,
            "total_affected_users": 420,
            "peak_p99_ms":          3200.0,
            "symptoms":             "用户支付页面卡在加载中，最终提示 'Downstream timeout while calling backend'；payment-service 日志显示调用 risk-control 服务超时",
            "root_cause":           "风控服务 risk-control 单实例 CPU 打满（新模型上线后推理耗时 200ms→1.2s），payment-service 同步调用阻塞累积",
            "resolution":           "risk-control 横向扩容 2→6 实例；payment-service 调风控加 800ms timeout + fallback 跳过风控（事后异步审计）；新增 risk-control p99 > 500ms 告警",
            "resolved_at":          (yesterday_22 - timedelta(days=8) + timedelta(hours=1)).isoformat(),
            "sample_traces":        json.dumps(["trace-hist-0030", "trace-hist-0031"]),
            "environment":          ENV,
            "stat_date":            (yesterday_22 - timedelta(days=8)).date().isoformat(),
        },
        {
            "incident_id":          "INC-2026-04-15-auth-E1001",
            "title":                "登录大面积失败 E1001",
            "service_name":         "auth-service",
            "error_codes":          json.dumps(["E1001"]),
            "severity":             "P1",
            "start_time":           (yesterday_22 - timedelta(days=33)).isoformat(),
            "end_time":             (yesterday_22 - timedelta(days=33) + timedelta(minutes=50)).isoformat(),
            "peak_error_rate":      0.55,
            "total_affected_users": 3800,
            "peak_p99_ms":          1500.0,
            "symptoms":             "用户登录提示账号密码错误但凭证正确；auth-service 日志显示 Redis token 校验 E1001",
            "root_cause":           "Redis 集群主节点 failover，新主节点未同步全部 session（异步复制延迟 2s），用户带旧 token 来校验时找不到",
            "resolution":           "Redis 切换到半同步复制；auth-service token 校验失败时降级查 DynamoDB；前端 401 时自动触发一次 silent re-login",
            "resolved_at":          (yesterday_22 - timedelta(days=33) + timedelta(hours=2)).isoformat(),
            "sample_traces":        json.dumps(["trace-hist-0040"]),
            "environment":          ENV,
            "stat_date":            (yesterday_22 - timedelta(days=33)).date().isoformat(),
        },
        {
            "incident_id":          "INC-2026-02-20-gateway-E3001",
            "title":                "API Gateway 大面积超时 E3001",
            "service_name":         "api-gateway",
            "error_codes":          json.dumps(["E3001"]),
            "severity":             "P1",
            "start_time":           (yesterday_22 - timedelta(days=87)).isoformat(),
            "end_time":             (yesterday_22 - timedelta(days=87) + timedelta(hours=1, minutes=10)).isoformat(),
            "peak_error_rate":      0.38,
            "total_affected_users": 5600,
            "peak_p99_ms":          29800.0,
            "symptoms":             "全站 API 响应慢甚至 504；api-gateway 日志显示上游 connection refused E3001",
            "root_cause":           "下游 product-service 一个新部署的版本内存泄漏，pod 频繁 OOMKilled 重启，连接池被打满",
            "resolution":           "product-service 回滚到上一个稳定版本；新增内存使用率 > 80% 持续 5 分钟告警；CI 加 heap profile diff 检查",
            "resolved_at":          (yesterday_22 - timedelta(days=87) + timedelta(hours=2)).isoformat(),
            "sample_traces":        json.dumps(["trace-hist-0050"]),
            "environment":          ENV,
            "stat_date":            (yesterday_22 - timedelta(days=87)).date().isoformat(),
        },
        {
            "incident_id":          "INC-2026-05-02-order-E4001",
            "title":                "订单状态机错乱 E4001",
            "service_name":         "order-service",
            "error_codes":          json.dumps(["E4001"]),
            "severity":             "P2",
            "start_time":           (yesterday_22 - timedelta(days=16)).isoformat(),
            "end_time":             (yesterday_22 - timedelta(days=16) + timedelta(minutes=25)).isoformat(),
            "peak_error_rate":      0.06,
            "total_affected_users": 180,
            "peak_p99_ms":          600.0,
            "symptoms":             "用户已支付的订单显示 '待支付'，再次支付报错 E4001 'invalid order state transition'",
            "root_cause":           "支付成功回调和用户主动查询订单状态走两条路径，并发写订单状态时无版本号控制，发生 lost update",
            "resolution":           "order_status 字段加 version 列 + CAS 更新；回调与查询统一走 saga；新增订单状态变更审计日志",
            "resolved_at":          (yesterday_22 - timedelta(days=16) + timedelta(hours=1, minutes=30)).isoformat(),
            "sample_traces":        json.dumps(["trace-hist-0060"]),
            "environment":          ENV,
            "stat_date":            (yesterday_22 - timedelta(days=16)).date().isoformat(),
        },
    ]
    df = pd.DataFrame(rows)
    # incident_summary DDL 里 stat_date 是 STRING（不是 DATE）。但 awswrangler 看到
    # ISO 日期格式字符串会 auto-infer 成 date32 → parquet INT32 → 与 VARCHAR schema 冲突。
    # 强制声明为 string dtype 阻止 auto-infer。
    df["stat_date"] = df["stat_date"].astype("string")
    wr.athena.to_iceberg(
        df=df,
        database=GOLD_DB,
        table="incident_summary",
        temp_path=f"{GOLD_BUCKET}/_tmp/seed/incident_summary/{_RUN_ID}/",
        partition_cols=["stat_date"],
        keep_files=False,
        boto3_session=boto3.Session(region_name=REGION),
    )
    print(f"Seeded {len(rows)} incidents -> {GOLD_DB}.incident_summary")


if __name__ == "__main__":
    print(f"Seeding test data into env={ENV}, region={REGION}, mode={MODE}")
    if MODE == "full":
        # 跳过 bigdata 真实 Glue 流水线，所有数据都 mock 直写
        seed_gold_api_error_stats()
        seed_silver_parsed_logs()
        seed_dq_reports()
        seed_gold_incident_summary()
    else:  # rag-only
        # 搭配 bigdata `make seed-production` 使用：
        # Silver parsed_logs + Gold api_error_stats 已由真实 Glue 流水线产生，
        # 这里只补 Glue 跑不出来的两份数据：
        #   - incident_summary：需 24h 累积数据，demo 内攒不出来
        #   - dq_reports：demo 数据量小，DQ 阈值可能不触发
        seed_dq_reports()
        seed_gold_incident_summary()
    print("Done. Integration tests can now run against deterministic data.")
