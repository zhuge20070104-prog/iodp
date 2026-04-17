#!/usr/bin/env bash
# scripts/bootstrap_kafka_topics.sh
# 创建 MSK Serverless Kafka Topics
#
# 用法（由 Terraform null_resource 自动调用，也可手动执行）：
#   ./bootstrap_kafka_topics.sh \
#     --bootstrap-servers "boot-xxxxx.kafka-serverless.us-east-1.amazonaws.com:9098" \
#     --topics '[{"name":"user_clickstream","partitions":6,"retention":168}]'
#
# 前置条件：
#   - 安装 kafka-topics.sh（Kafka CLI）或通过 Docker 运行
#   - AWS CLI 已配置 IAM 凭证
#   - 已安装 jq

set -euo pipefail

BOOTSTRAP_SERVERS=""
TOPICS_JSON=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bootstrap-servers) BOOTSTRAP_SERVERS="$2"; shift 2 ;;
    --topics)            TOPICS_JSON="$2";       shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$BOOTSTRAP_SERVERS" || -z "$TOPICS_JSON" ]]; then
  echo "Usage: $0 --bootstrap-servers <servers> --topics '<json>'" >&2
  exit 1
fi

# MSK Serverless IAM 认证配置文件
CLIENT_PROPERTIES=$(mktemp)
trap 'rm -f "$CLIENT_PROPERTIES"' EXIT

cat > "$CLIENT_PROPERTIES" <<EOF
security.protocol=SASL_SSL
sasl.mechanism=AWS_MSK_IAM
sasl.jaas.config=software.amazon.msk.auth.iam.IAMLoginModule required;
sasl.client.callback.handler.class=software.amazon.msk.auth.iam.IAMClientCallbackHandler
EOF

echo "=== Creating Kafka topics on MSK Serverless ==="
echo "Bootstrap servers: $BOOTSTRAP_SERVERS"

TOPIC_COUNT=$(echo "$TOPICS_JSON" | jq length)

for i in $(seq 0 $((TOPIC_COUNT - 1))); do
  TOPIC_NAME=$(echo "$TOPICS_JSON" | jq -r ".[$i].name")
  PARTITIONS=$(echo "$TOPICS_JSON" | jq -r ".[$i].partitions")
  RETENTION_HOURS=$(echo "$TOPICS_JSON" | jq -r ".[$i].retention")
  RETENTION_MS=$((RETENTION_HOURS * 3600 * 1000))

  echo "--- Creating topic: $TOPIC_NAME (partitions=$PARTITIONS, retention=${RETENTION_HOURS}h) ---"

  kafka-topics.sh \
    --bootstrap-server "$BOOTSTRAP_SERVERS" \
    --command-config "$CLIENT_PROPERTIES" \
    --create \
    --if-not-exists \
    --topic "$TOPIC_NAME" \
    --partitions "$PARTITIONS" \
    --config "retention.ms=$RETENTION_MS" \
    2>&1 || echo "  [WARN] Topic $TOPIC_NAME may already exist, skipping."

  echo "  [OK] $TOPIC_NAME"
done

echo "=== Done: $TOPIC_COUNT topics processed ==="
