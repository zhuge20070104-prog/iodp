#!/usr/bin/env bash
# scripts/upload_glue_scripts.sh
# 将 Glue PySpark 脚本和公共库上传到 S3 Scripts Bucket
#
# 用法：
#   ./upload_glue_scripts.sh <scripts-bucket-name>
#   例: ./upload_glue_scripts.sh iodp-glue-scripts-prod-987654321098
#
# 上传结构：
#   s3://<bucket>/streaming/stream_clickstream.py
#   s3://<bucket>/streaming/stream_app_logs.py
#   s3://<bucket>/batch/silver_enrich_clicks.py
#   s3://<bucket>/batch/...
#   s3://<bucket>/lib.zip        ← 公共库打包

set -euo pipefail

SCRIPTS_BUCKET="${1:?Usage: $0 <scripts-bucket-name>}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GLUE_DIR="$PROJECT_ROOT/glue_jobs"

echo "=== Uploading Glue scripts to s3://$SCRIPTS_BUCKET ==="

# ─── 1. 打包公共库 lib/ → lib.zip ───
echo "--- Packaging lib/ into lib.zip ---"
LIB_ZIP=$(mktemp -d)/lib.zip
(cd "$GLUE_DIR/lib" && zip -r "$LIB_ZIP" *.py)

aws s3 cp "$LIB_ZIP" "s3://$SCRIPTS_BUCKET/lib.zip"
echo "  [OK] lib.zip uploaded"

# ─── 2. 上传 Streaming 脚本 ───
echo "--- Uploading streaming scripts ---"
for script in "$GLUE_DIR"/streaming/*.py; do
  filename=$(basename "$script")
  aws s3 cp "$script" "s3://$SCRIPTS_BUCKET/streaming/$filename"
  echo "  [OK] streaming/$filename"
done

# ─── 3. 上传 Batch 脚本 ───
echo "--- Uploading batch scripts ---"
for script in "$GLUE_DIR"/batch/*.py; do
  filename=$(basename "$script")
  aws s3 cp "$script" "s3://$SCRIPTS_BUCKET/batch/$filename"
  echo "  [OK] batch/$filename"
done

echo "=== Done: all scripts uploaded to s3://$SCRIPTS_BUCKET ==="
