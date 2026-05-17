#!/usr/bin/env bash
# Nuke agent residual AWS resources (state-orphaned)
#
# 用法：bash scripts/nuke-residuals.sh
#
# 删除以下残留资源（agent state 不再管理它们）:
#   - CloudFront distribution + OAI（最慢 ~10-15 min: disable → wait → delete）
#   - S3 Frontend bucket（含文件）
#   - ECR repository iodp-agent
#   - IAM Role iodp-agent-dev-lambda-role（先 detach policies）
#   - DynamoDB iodp-agent-state-dev, iodp-bug-tickets-dev, iodp-agent-jobs-dev
#   - S3 Vectors bucket iodp-rag-dev（含 indexes）
#   - Lambda function iodp-agent-dev（如果存在）
#   - API Gateway HTTP API（如果存在）

set -uo pipefail   # 不加 -e：删除已不存在的资源失败时不要终止脚本

REGION=${AWS_REGION:-ap-southeast-1}
ENV=${ENV:-dev}
PREFIX="iodp-agent-${ENV}"

echo "════════════════════════════════════════════"
echo " Nuke agent residuals: region=$REGION env=$ENV"
echo "════════════════════════════════════════════"

# ─── 1. 触发 CloudFront disable（异步，立即返回；同时跑其他删除）───
echo ""
echo "[1/9] 触发 CloudFront disable（异步）..."
CF_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Origins.Items[?contains(DomainName, '${PREFIX}-frontend')]] | [0].Id" \
  --output text 2>/dev/null)

if [ -n "$CF_ID" ] && [ "$CF_ID" != "None" ]; then
    echo "    CF_ID=$CF_ID"
    ETAG=$(aws cloudfront get-distribution-config --id "$CF_ID" --query 'ETag' --output text)
    aws cloudfront get-distribution-config --id "$CF_ID" --query 'DistributionConfig' > /tmp/cf-config.json
    ENABLED=$(python3 -c "import json; print(json.load(open('/tmp/cf-config.json'))['Enabled'])")
    if [ "$ENABLED" = "True" ]; then
        python3 -c "import json; c=json.load(open('/tmp/cf-config.json')); c['Enabled']=False; json.dump(c, open('/tmp/cf-config-d.json','w'))"
        aws cloudfront update-distribution --id "$CF_ID" \
            --if-match "$ETAG" \
            --distribution-config file:///tmp/cf-config-d.json >/dev/null
        echo "    ✓ Disable 已触发"
    else
        echo "    ✓ 已经是 disabled 状态"
    fi
else
    echo "    (无 CloudFront 残留)"
fi

# ─── 2. ECR Repository ───
echo ""
echo "[2/9] ECR repository"
aws ecr delete-repository --repository-name iodp-agent --force --region "$REGION" 2>&1 | head -1

# ─── 3. IAM Role（先 detach managed + delete inline policies）───
echo ""
echo "[3/9] IAM Role ${PREFIX}-lambda-role"
ROLE_NAME="${PREFIX}-lambda-role"
for p in $(aws iam list-attached-role-policies --role-name "$ROLE_NAME" \
        --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
    aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$p"
    echo "    ✓ Detached $p"
done
for p in $(aws iam list-role-policies --role-name "$ROLE_NAME" \
        --query 'PolicyNames[]' --output text 2>/dev/null); do
    aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$p"
    echo "    ✓ Deleted inline $p"
done
aws iam delete-role --role-name "$ROLE_NAME" 2>&1 | head -1

# ─── 4. S3 Frontend bucket ───
echo ""
echo "[4/9] S3 ${PREFIX}-frontend"
aws s3 rb "s3://${PREFIX}-frontend" --force --region "$REGION" 2>&1 | head -1

# ─── 5. DynamoDB tables ───
echo ""
echo "[5/9] DynamoDB tables"
for t in "iodp-agent-state-${ENV}" "iodp-bug-tickets-${ENV}" "iodp-agent-jobs-${ENV}"; do
    aws dynamodb delete-table --table-name "$t" --region "$REGION" >/dev/null 2>&1 \
        && echo "    ✓ Deleted $t" || echo "    (skip $t)"
done

# ─── 6. S3 Vectors bucket ───
echo ""
echo "[6/9] S3 Vectors bucket iodp-rag-${ENV}"
for idx in $(aws s3vectors list-indexes --vector-bucket-name "iodp-rag-${ENV}" \
        --region "$REGION" --query 'indexes[].indexName' --output text 2>/dev/null); do
    aws s3vectors delete-index --vector-bucket-name "iodp-rag-${ENV}" \
        --index-name "$idx" --region "$REGION"
    echo "    ✓ Deleted index $idx"
done
aws s3vectors delete-vector-bucket --vector-bucket-name "iodp-rag-${ENV}" --region "$REGION" 2>&1 | head -1

# ─── 7. Lambda function（如果存在）───
echo ""
echo "[7/9] Lambda function ${PREFIX}"
aws lambda delete-function --function-name "${PREFIX}" --region "$REGION" 2>&1 | head -1

# ─── 8. API Gateway HTTP API（如果存在）───
echo ""
echo "[8/9] API Gateway HTTP API"
for api in $(aws apigatewayv2 get-apis --region "$REGION" \
        --query "Items[?Name=='${PREFIX}-api'].ApiId" --output text 2>/dev/null); do
    aws apigatewayv2 delete-api --api-id "$api" --region "$REGION"
    echo "    ✓ Deleted API $api"
done

# ─── 9. 等 CloudFront 完成 disable，然后 delete + 删 OAI ───
echo ""
echo "[9/9] 等 CloudFront disable 完成（~10-15 min）..."
if [ -n "$CF_ID" ] && [ "$CF_ID" != "None" ]; then
    aws cloudfront wait distribution-deployed --id "$CF_ID"
    ETAG=$(aws cloudfront get-distribution --id "$CF_ID" --query 'ETag' --output text)
    aws cloudfront delete-distribution --id "$CF_ID" --if-match "$ETAG"
    echo "    ✓ CloudFront $CF_ID deleted"

    # Delete OAI (要在 CloudFront delete 之后)
    for oai in $(aws cloudfront list-cloud-front-origin-access-identities \
            --query "CloudFrontOriginAccessIdentityList.Items[?contains(Comment, '${PREFIX}-frontend')].Id" \
            --output text 2>/dev/null); do
        ETAG=$(aws cloudfront get-cloud-front-origin-access-identity --id "$oai" --query 'ETag' --output text)
        aws cloudfront delete-cloud-front-origin-access-identity --id "$oai" --if-match "$ETAG" 2>&1 | head -1
        echo "    ✓ OAI $oai deleted"
    done
fi

echo ""
echo "════════════════════════════════════════════"
echo " ✅ Nuke complete"
echo "════════════════════════════════════════════"
echo ""
echo "下一步："
echo "  1. 清 agent state 残留：aws s3 rm s3://iodp-terraform-state-prod/agent/terraform.tfstate --region us-east-1"
echo "  2. 重新部署：cd /mnt/c/code1/iodp && make init ENV=dev AWS_REGION=ap-southeast-1"
