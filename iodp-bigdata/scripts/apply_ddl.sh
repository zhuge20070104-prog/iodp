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

echo "=== Applying DDL templates ==="
echo "  ENVIRONMENT : $ENVIRONMENT"
echo "  ACCOUNT_ID  : $ACCOUNT_ID"
echo "  REGION      : $AWS_REGION"
echo "  WORKGROUP   : $ATHENA_WORKGROUP"
echo ""

# Athena 查询结果输出位置（用 bronze bucket 下的 athena-results/ 目录）
OUTPUT_LOCATION="s3://iodp-bronze-${ENVIRONMENT}-${ACCOUNT_ID}/athena-results/"

for ddl_file in "$DDL_DIR"/*.sql; do
    filename=$(basename "$ddl_file")
    echo "--- Processing $filename ---"

    # 渲染占位符
    rendered_sql=$(sed \
        -e "s/\${ENVIRONMENT}/${ENVIRONMENT}/g" \
        -e "s/\${ACCOUNT_ID}/${ACCOUNT_ID}/g" \
        "$ddl_file")

    # 通过 Athena 执行
    query_execution_id=$(aws athena start-query-execution \
        --query-string "$rendered_sql" \
        --work-group "$ATHENA_WORKGROUP" \
        --result-configuration "OutputLocation=${OUTPUT_LOCATION}" \
        --region "$AWS_REGION" \
        --output text \
        --query "QueryExecutionId")

    echo "  [SUBMITTED] QueryExecutionId: $query_execution_id"

    # 等待完成（最多 60 秒）
    for i in $(seq 1 12); do
        state=$(aws athena get-query-execution \
            --query-execution-id "$query_execution_id" \
            --region "$AWS_REGION" \
            --output text \
            --query "QueryExecution.Status.State")

        if [[ "$state" == "SUCCEEDED" ]]; then
            echo "  [OK] $filename"
            break
        elif [[ "$state" == "FAILED" || "$state" == "CANCELLED" ]]; then
            reason=$(aws athena get-query-execution \
                --query-execution-id "$query_execution_id" \
                --region "$AWS_REGION" \
                --output text \
                --query "QueryExecution.Status.StateChangeReason")
            echo "  [FAILED] $filename: $reason" >&2
            break
        fi
        sleep 5
    done

    if [[ "$state" == "RUNNING" || "$state" == "QUEUED" ]]; then
        echo "  [TIMEOUT] $filename still $state after 60s, check Athena console" >&2
    fi
done

echo ""
echo "=== DDL apply complete ==="
