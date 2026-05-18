#!/usr/bin/env bash
# scripts/backfill_past_hour.sh
#
# 一次性验证脚本：绕过 Firehose 直接把"过去某小时"的数据灌进 Bronze，
# 再触发 Silver + Gold 用 --TARGET_HOUR 跑那一小时。
#
# 用途：验证 Gold 改 MERGE 后跨小时不互相覆盖。
# 流程：先跑这个灌 hour=H1 → 再跑 make seed-production 灌当前小时 H2 →
#       查 Gold：应该同时存在 H1 和 H2 两个小时的行。
#
# 用法：
#   bash scripts/backfill_past_hour.sh <env> <YYYY-MM-DD-HH>  (UTC)
# 示例：
#   bash scripts/backfill_past_hour.sh dev 2026-05-18-03
#
# 跟 seed_production_pipeline.sh 的差异：
#   - 它推 Firehose（必然落到当前小时）；本脚本直接写 S3，可指定历史小时
#   - 它跑 produce + Silver + Gold；本脚本只灌 1 个小时所以也跑一遍 Silver/Gold

set -euo pipefail

ENV="${1:?Usage: $0 <env> <YYYY-MM-DD-HH>}"
TARGET_HOUR="${2:?Usage: $0 <env> <YYYY-MM-DD-HH>}"
COUNT="${COUNT:-500}"
ERROR_RATE="${ERROR_RATE:-0.10}"
REGION="${AWS_REGION:-ap-southeast-1}"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BRONZE_BUCKET="iodp-bronze-${ENV}-${ACCOUNT_ID}"

# Parse YYYY-MM-DD-HH
YEAR="${TARGET_HOUR:0:4}"
MONTH="${TARGET_HOUR:5:2}"
DAY="${TARGET_HOUR:8:2}"
HOUR="${TARGET_HOUR:11:2}"

SILVER_JOBS=("iodp-silver-enrich-clicks-${ENV}" "iodp-silver-parse-logs-${ENV}")
GOLD_JOBS=("iodp-gold-api-error-stats-${ENV}" "iodp-gold-hourly-active-users-${ENV}")

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   Backfill 历史小时 ($ENV, hour=$TARGET_HOUR UTC)             "
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║   Bronze bucket : $BRONZE_BUCKET                              "
echo "║   Records       : $COUNT clicks + $COUNT logs                 "
echo "║   App-log error : $ERROR_RATE                                 "
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# ─── 1. 生成 gzipped NDJSON（event_timestamp 落在目标小时内）───
echo "--- Step 1: 生成 ${COUNT} 条 clickstream + ${COUNT} 条 app_logs ---"
python3 - <<PY
import gzip, json, os, random, uuid
from datetime import datetime, timezone, timedelta

target_hour = datetime.strptime("${TARGET_HOUR}", "%Y-%m-%d-%H").replace(tzinfo=timezone.utc)
count       = int("${COUNT}")
err_rate    = float("${ERROR_RATE}")
tmpdir      = "${TMPDIR}"

VALID_EVENT_TYPES = ["click","view","scroll","purchase","add_to_cart","checkout"]
VALID_ERROR_CODES = ["E1001","E1002","E2001","E2002","E3001","E4001"]
VALID_ERROR_TYPES = ["TimeoutException","NullPointerException","ConnectionRefused",
                     "IllegalArgumentException","ResourceExhausted","DownstreamUnavailable"]
SERVICES          = ["payment-service","user-service","search-service","checkout-service"]

def random_ts_in_hour():
    offset = random.uniform(0, 3600)
    return (target_hour + timedelta(seconds=offset)).isoformat()

def clickstream():
    return {
        "event_id":        str(uuid.uuid4()),
        "user_id":         f"usr_{random.randint(10_000_000, 99_999_999)}",
        "session_id":      str(uuid.uuid4()),
        "event_type":      random.choice(VALID_EVENT_TYPES),
        "event_timestamp": random_ts_in_hour(),
        "page_url":        f"/page/{random.randint(1,100)}",
        "referrer_url":    "/home",
        "device_info":     {"device_type":"mobile","os":"iOS","browser":"Safari"},
        "geo_info":        {"country_code":"US","city":"NYC","ip_hash":"abc123"},
        "properties":      {"product_id":f"prod_{random.randint(1,1000)}","amount":round(random.uniform(0,500),2)},
        "environment":     "demo",
    }

def app_log():
    is_err = random.random() < err_rate
    return {
        "log_id":          str(uuid.uuid4()),
        "trace_id":        str(uuid.uuid4()),
        "span_id":         uuid.uuid4().hex[:16],
        "service_name":    random.choice(SERVICES),
        "instance_id":     f"i-{uuid.uuid4().hex[:8]}",
        "log_level":       "ERROR" if is_err else "INFO",
        "event_timestamp": random_ts_in_hour(),
        "message":         "Downstream timeout while calling backend" if is_err else "Request OK",
        "error_code":      random.choice(VALID_ERROR_CODES) if is_err else None,
        "error_type":      random.choice(VALID_ERROR_TYPES) if is_err else None,
        "http_status":     500 if is_err else 200,
        "stack_trace":     "Traceback...\n  ..." if is_err else None,
        "req_method":      random.choice(["GET","POST","PUT"]),
        "req_path":        f"/api/v1/resource/{random.randint(1,50)}",
        "user_id":         f"usr_{random.randint(10_000_000, 99_999_999)}",
        "req_duration_ms": random.randint(50,800),
        "environment":     "demo",
    }

with gzip.open(os.path.join(tmpdir, "clicks.ndjson.gz"), "wt", encoding="utf-8") as f:
    for _ in range(count):
        f.write(json.dumps(clickstream()) + "\n")

with gzip.open(os.path.join(tmpdir, "logs.ndjson.gz"), "wt", encoding="utf-8") as f:
    for _ in range(count):
        f.write(json.dumps(app_log()) + "\n")

print(f"  生成完毕: {count} clicks + {count} logs")
PY

# ─── 2. 上传到 Bronze 的目标小时分区（绕过 Firehose）───
echo "--- Step 2: 上传到 Bronze hour=${TARGET_HOUR} 分区 ---"
STAMP=$(date +%s)
CLICKS_KEY="clickstream/year=${YEAR}/month=${MONTH}/day=${DAY}/hour=${HOUR}/backfill-${STAMP}.ndjson.gz"
LOGS_KEY="app_logs/year=${YEAR}/month=${MONTH}/day=${DAY}/hour=${HOUR}/backfill-${STAMP}.ndjson.gz"

aws s3 cp "$TMPDIR/clicks.ndjson.gz" "s3://${BRONZE_BUCKET}/${CLICKS_KEY}" --region "$REGION"
aws s3 cp "$TMPDIR/logs.ndjson.gz"   "s3://${BRONZE_BUCKET}/${LOGS_KEY}"   --region "$REGION"
echo "  [OK] 上传完成"

# ─── 3. 触发 Silver / Gold（带 --TARGET_HOUR override）───
start_and_wait() {
    local job="$1"
    echo "  ► $job"
    local run_id
    # 注意：aws cli --arguments 不能直接 "--TARGET_HOUR=xxx"，因为 "--" 开头会被
    # 解析为新 flag。必须用 JSON 形式 '{"--TARGET_HOUR":"xxx"}'。
    run_id=$(aws glue start-job-run \
        --job-name "$job" \
        --arguments "{\"--TARGET_HOUR\":\"${TARGET_HOUR}\"}" \
        --region "$REGION" \
        --query 'JobRunId' --output text)
    echo "    JobRunId: $run_id"

    local state
    for i in $(seq 1 80); do  # 最多等 80 × 15s = 20 min
        state=$(aws glue get-job-run \
            --job-name "$job" \
            --run-id "$run_id" \
            --region "$REGION" \
            --query 'JobRun.JobRunState' --output text)
        case "$state" in
            SUCCEEDED) echo "    [OK] $job ($state)"; return 0 ;;
            FAILED|TIMEOUT|STOPPED|ERROR)
                echo "    [FAIL] $job state=$state" >&2
                aws glue get-job-run --job-name "$job" --run-id "$run_id" \
                    --region "$REGION" --query 'JobRun.ErrorMessage' --output text >&2
                return 1 ;;
        esac
        sleep 15
    done
    echo "    [TIMEOUT] $job still $state after 20 min" >&2
    return 1
}

echo "--- Step 3: 触发 Silver（--TARGET_HOUR=${TARGET_HOUR}）---"
for j in "${SILVER_JOBS[@]}"; do
    start_and_wait "$j"
done

echo "--- Step 4: 触发 Gold（--TARGET_HOUR=${TARGET_HOUR}）---"
for j in "${GOLD_JOBS[@]}"; do
    start_and_wait "$j"
done

echo ""
echo "✅ Backfill 完成 (env=$ENV, hour=$TARGET_HOUR)"
echo ""
echo "下一步建议："
echo "  1. cd /mnt/c/code1/iodp && make seed-production  # 灌当前小时"
echo "  2. 用 Athena 查 Gold，应同时看到 ${TARGET_HOUR} 和当前小时的行："
echo ""
echo "     SELECT date_trunc('hour', stat_hour) AS h, service_name, COUNT(*)"
echo "     FROM iodp_gold_${ENV}.api_error_stats"
echo "     WHERE stat_date = DATE '${YEAR}-${MONTH}-${DAY}'"
echo "     GROUP BY 1, 2 ORDER BY 1;"
