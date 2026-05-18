#!/usr/bin/env bash
# scripts/seed_production_pipeline.sh
#
# Production-like seed：跑一遍真实的 Medallion 流水线：
#   1. producer 推数据 → Firehose
#   2. 等 Firehose buffer flush (~60s) 到 Bronze S3
#   3. 用 --TARGET_HOUR=<当前小时> 触发 silver_enrich_clicks + silver_parse_logs
#   4. 轮询直到两个 Silver Job 完成
#   5. 用 --TARGET_HOUR=<当前小时> 触发 gold_api_error_stats + gold_hourly_active_users
#   6. 轮询直到两个 Gold Job 完成
#
# 不包含：
#   - gold_incident_summary：它聚合「过去 24h 的 api_error_stats」，demo 无法
#     在期内攒满 24h 数据，由 agent 端 seed_test_data.py --mode rag-only 单独 mock。
#   - DQ reports：本流水线确实会写 DQ，但量小不一定触发阈值；agent 端 seed 补充。
#
# 用法：
#   bash scripts/seed_production_pipeline.sh <env> <region>
#   bash scripts/seed_production_pipeline.sh dev ap-southeast-1
#
# 时间窗口选择：
#   默认用「当前小时」做 TARGET_HOUR。Producer 推完 → Firehose buffer (60s) →
#   数据落到 Bronze 的 year=/month=/day=/hour=<当前小时>/ 下 → Silver 立即处理。

set -euo pipefail

ENV="${1:?Usage: $0 <env> <region>}"
REGION="${2:?Usage: $0 <env> <region>}"
COUNT="${COUNT:-1000}"
ERROR_RATE="${ERROR_RATE:-0.10}"   # 默认 10% 错误率以制造 ERROR 日志供 Gold api_error_stats 统计

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 锁定窗口：用当前 UTC 小时（Firehose dynamic partitioning 写到的就是当前 hour=）
TARGET_HOUR=$(date -u +'%Y-%m-%d-%H')

SILVER_JOBS=("iodp-silver-enrich-clicks-${ENV}" "iodp-silver-parse-logs-${ENV}")
GOLD_JOBS=("iodp-gold-api-error-stats-${ENV}" "iodp-gold-hourly-active-users-${ENV}")

echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║   IODP Production-like Seed (env=${ENV}, region=${REGION})  "
echo "╠════════════════════════════════════════════════════════════╣"
echo "║   TARGET_HOUR     : ${TARGET_HOUR} (UTC)                    "
echo "║   Records         : ${COUNT} clicks + ${COUNT} logs         "
echo "║   App-log err rate: ${ERROR_RATE}                           "
echo "║   Pipeline        : Firehose → Bronze → Silver → Gold       "
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# ─── 1. Producer 推数据到 Firehose ───
echo "▶ [1/6] Producing ${COUNT} clickstream + ${COUNT} app_logs events to Firehose..."
python "$PROJECT_ROOT/scripts/produce_sample_events.py" \
    --stream clickstream --env "$ENV" --region "$REGION" --count "$COUNT"
python "$PROJECT_ROOT/scripts/produce_sample_events.py" \
    --stream app_logs --env "$ENV" --region "$REGION" \
    --count "$COUNT" --error-rate "$ERROR_RATE"

# ─── 2. 等 Firehose buffer flush（默认 60s / 5 MB，先满者触发）───
WAIT_BUFFER=75
echo ""
echo "▶ [2/6] Waiting ${WAIT_BUFFER}s for Firehose buffer to flush to Bronze S3..."
sleep "$WAIT_BUFFER"

# ─── 3. 启动 Silver 层 2 个 Job（带 --TARGET_HOUR）───
# 注意：aws cli --arguments 不能直接 "--TARGET_HOUR=xxx"，因为 "--" 开头会被
# 解析为新 flag。必须用 JSON 形式 '{"--TARGET_HOUR":"xxx"}'。
echo ""
echo "▶ [3/6] Starting Silver jobs with --TARGET_HOUR=${TARGET_HOUR}..."
declare -A RUN_IDS
for job in "${SILVER_JOBS[@]}"; do
    run_id=$(aws glue start-job-run \
        --region "$REGION" \
        --job-name "$job" \
        --arguments "{\"--TARGET_HOUR\":\"${TARGET_HOUR}\"}" \
        --output text --query 'JobRunId')
    RUN_IDS[$job]=$run_id
    echo "  → $job  (JobRunId: $run_id)"
done

# ─── 4. 轮询 Silver 完成 ───
echo ""
echo "▶ [4/6] Polling Silver jobs (timeout 10 min)..."
poll_until_done() {
    local job="$1"
    local run_id="$2"
    local max_polls=60   # 60 * 10s = 10 min
    local state=""
    for i in $(seq 1 $max_polls); do
        state=$(aws glue get-job-run \
            --region "$REGION" \
            --job-name "$job" --run-id "$run_id" \
            --output text --query 'JobRun.JobRunState')
        case "$state" in
            SUCCEEDED) echo "  ✅ $job"; return 0 ;;
            FAILED|STOPPED|TIMEOUT|ERROR)
                local reason=$(aws glue get-job-run \
                    --region "$REGION" \
                    --job-name "$job" --run-id "$run_id" \
                    --output text --query 'JobRun.ErrorMessage')
                echo "  ❌ $job: $state — $reason" >&2
                return 1
                ;;
        esac
        sleep 10
    done
    echo "  ⏱  $job: timed out (last state=$state)" >&2
    return 1
}

silver_failed=0
for job in "${SILVER_JOBS[@]}"; do
    poll_until_done "$job" "${RUN_IDS[$job]}" || silver_failed=1
done
if [ "$silver_failed" -ne 0 ]; then
    echo ""
    echo "❌ Silver 阶段有失败，跳过 Gold（流水线无新数据可聚合）"
    exit 1
fi

# ─── 5. 启动 Gold 层 2 个 Job（带 --TARGET_HOUR）───
echo ""
echo "▶ [5/6] Starting Gold jobs with --TARGET_HOUR=${TARGET_HOUR}..."
unset RUN_IDS
declare -A RUN_IDS
for job in "${GOLD_JOBS[@]}"; do
    run_id=$(aws glue start-job-run \
        --region "$REGION" \
        --job-name "$job" \
        --arguments "{\"--TARGET_HOUR\":\"${TARGET_HOUR}\"}" \
        --output text --query 'JobRunId')
    RUN_IDS[$job]=$run_id
    echo "  → $job  (JobRunId: $run_id)"
done

# ─── 6. 轮询 Gold 完成 ───
echo ""
echo "▶ [6/6] Polling Gold jobs (timeout 10 min)..."
gold_failed=0
for job in "${GOLD_JOBS[@]}"; do
    poll_until_done "$job" "${RUN_IDS[$job]}" || gold_failed=1
done

echo ""
if [ "$gold_failed" -eq 0 ]; then
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║  ✅ Production-like seed 完成                                "
    echo "║                                                            "
    echo "║  下一步：seed mock 数据补 incident_summary + DQ reports：    "
    echo "║    cd ../iodp-agent && python scripts/seed_test_data.py \\   "
    echo "║      --env ${ENV} --region ${REGION} --mode rag-only        "
    echo "╚════════════════════════════════════════════════════════════╝"
else
    echo "❌ Gold 阶段有失败，检查 CloudWatch /aws-glue/jobs/output"
    exit 1
fi
