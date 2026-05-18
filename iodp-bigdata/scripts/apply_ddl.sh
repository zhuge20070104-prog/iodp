#!/usr/bin/env bash
# scripts/apply_ddl.sh
# 渲染 DDL 模板中的 ${ENVIRONMENT} 和 ${ACCOUNT_ID} 占位符，然后通过 Athena 执行建表。
#
# 用法：
#   ./apply_ddl.sh <environment> <account_id> [aws-region]
#   例: ./apply_ddl.sh prod 987654321098
#       ./apply_ddl.sh dev  123456789012 us-west-2
#
# 前置要求：
#   - AWS CLI v2 已配置好对应账号凭据
#   - Athena workgroup "primary" 可用（或设 ATHENA_WORKGROUP 环境变量）

set -euo pipefail

ENVIRONMENT="${1:?Usage: $0 <environment> <account_id> [aws-region]}"
ACCOUNT_ID="${2:?Usage: $0 <environment> <account_id> [aws-region]}"
AWS_REGION="${3:-us-east-1}"
ATHENA_WORKGROUP="${ATHENA_WORKGROUP:-primary}"

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DDL_DIR="$PROJECT_ROOT/athena/ddl"
VIEWS_DIR="$PROJECT_ROOT/athena/views"

echo "=== Applying DDL templates ==="
echo "  ENVIRONMENT : $ENVIRONMENT"
echo "  ACCOUNT_ID  : $ACCOUNT_ID"
echo "  REGION      : $AWS_REGION"
echo "  WORKGROUP   : $ATHENA_WORKGROUP"
echo ""

# Athena 查询结果输出位置（用 bronze bucket 下的 athena-results/ 目录）
OUTPUT_LOCATION="s3://iodp-bronze-${ENVIRONMENT}-${ACCOUNT_ID}/athena-results/"

# 提交一条 Athena 语句并轮询结果。
# 用法：run_athena <label> <sql> [allow_fail]
#   allow_fail=1 时 FAILED 不视为致命错误（用于 DROP TABLE IF EXISTS 容错）
run_athena() {
    local label="$1"
    local sql="$2"
    local allow_fail="${3:-0}"

    local qid
    qid=$(aws athena start-query-execution \
        --query-string "$sql" \
        --work-group "$ATHENA_WORKGROUP" \
        --result-configuration "OutputLocation=${OUTPUT_LOCATION}" \
        --region "$AWS_REGION" \
        --output text \
        --query "QueryExecutionId")

    echo "  [SUBMITTED] $label QueryExecutionId: $qid"

    local state=""
    for i in $(seq 1 12); do
        state=$(aws athena get-query-execution \
            --query-execution-id "$qid" \
            --region "$AWS_REGION" \
            --output text \
            --query "QueryExecution.Status.State")

        if [[ "$state" == "SUCCEEDED" ]]; then
            echo "  [OK] $label"
            return 0
        elif [[ "$state" == "FAILED" || "$state" == "CANCELLED" ]]; then
            local reason
            reason=$(aws athena get-query-execution \
                --query-execution-id "$qid" \
                --region "$AWS_REGION" \
                --output text \
                --query "QueryExecution.Status.StateChangeReason")
            if [[ "$allow_fail" == "1" ]]; then
                echo "  [SKIP] $label failed (tolerated): $reason"
                return 0
            fi
            echo "  [FAILED] $label: $reason" >&2
            return 1
        fi
        sleep 5
    done

    echo "  [TIMEOUT] $label still $state after 60s, check Athena console" >&2
    return 1
}

# 先建表（DDL），再建视图（views 引用底层表）
# 之前只跑 ddl/*.sql，views/*.sql 被遗漏，导致 v_error_log_enriched 从未建立，
# agent log_analyzer 一调就报 "view not found"。
#
# 幂等策略（破坏性）：
#   对 ddl/*.sql 先发 DROP TABLE IF EXISTS 再 CREATE TABLE。
#   原因：Glue Catalog 残留的孤儿 Iceberg 表会让 CREATE TABLE IF NOT EXISTS
#         报 "Iceberg cannot find the requested entity"。
#   代价：每次 deploy-ddl 会清空 Silver/Gold 表数据，仅适合 dev/demo 环境。
#   注意：首次启用此脚本前，必须先用 `aws glue delete-table` 清掉 metadata 丢失
#         的孤儿条目（DROP TABLE 自身也会触发 Iceberg metadata 读取）。
for ddl_file in "$DDL_DIR"/*.sql "$VIEWS_DIR"/*.sql; do
    filename=$(basename "$ddl_file")
    echo "--- Processing $filename ---"

    # 渲染占位符
    rendered_sql=$(sed \
        -e "s/\${ENVIRONMENT}/${ENVIRONMENT}/g" \
        -e "s/\${ACCOUNT_ID}/${ACCOUNT_ID}/g" \
        "$ddl_file")

    # 如果是 CREATE TABLE 类 DDL，先 DROP 同名表（容错失败）
    if grep -qiE '^[[:space:]]*CREATE[[:space:]]+TABLE' <<< "$rendered_sql"; then
        table_fqn=$(grep -oiE 'CREATE[[:space:]]+TABLE([[:space:]]+IF[[:space:]]+NOT[[:space:]]+EXISTS)?[[:space:]]+[A-Za-z0-9_.]+' <<< "$rendered_sql" \
            | head -1 \
            | awk '{print $NF}')
        if [[ -n "$table_fqn" ]]; then
            run_athena "DROP $table_fqn" "DROP TABLE IF EXISTS $table_fqn" 1
        fi
    fi

    # 执行原始 DDL
    run_athena "$filename" "$rendered_sql" 0 || true
done

echo ""
echo "=== DDL apply complete ==="
