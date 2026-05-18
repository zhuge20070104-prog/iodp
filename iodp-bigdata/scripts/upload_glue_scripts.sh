#!/usr/bin/env bash
# scripts/upload_glue_scripts.sh
# 将 Glue PySpark 脚本和公共库上传到 S3 Scripts Bucket
#
# 用法：
#   ./upload_glue_scripts.sh <scripts-bucket-name>
#   例: ./upload_glue_scripts.sh iodp-glue-scripts-prod-987654321098
#
# 上传结构：
#   s3://<bucket>/batch/silver_enrich_clicks.py
#   s3://<bucket>/batch/...
#   s3://<bucket>/lib.zip        ← 公共库打包
#
# 方案 D 后 Glue Streaming 已删除（Firehose 直接落 Bronze），不再上传 streaming/。

set -euo pipefail

SCRIPTS_BUCKET="${1:?Usage: $0 <scripts-bucket-name>}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GLUE_DIR="$PROJECT_ROOT/glue_jobs"

echo "=== Uploading Glue scripts to s3://$SCRIPTS_BUCKET ==="

# ─── 1. 打包公共库 lib/ → lib.zip ───
# 必须把 lib/ 目录本身打进 zip（含 __init__.py），让 Glue Python 把它识别为 package。
# 旧版在 lib/ 内部 zip *.py 会让 import lib.data_quality 报 "No module named 'lib'"。
echo "--- Packaging lib/ into lib.zip ---"
LIB_ZIP=$(mktemp -d)/lib.zip
(cd "$GLUE_DIR" && zip -r "$LIB_ZIP" lib -x 'lib/__pycache__/*' 'lib/*.pyc')

aws s3 cp "$LIB_ZIP" "s3://$SCRIPTS_BUCKET/lib.zip"
echo "  [OK] lib.zip uploaded"

# ─── 2. 上传 Batch 脚本 ───
echo "--- Uploading batch scripts ---"
for script in "$GLUE_DIR"/batch/*.py; do
  filename=$(basename "$script")
  aws s3 cp "$script" "s3://$SCRIPTS_BUCKET/batch/$filename"
  echo "  [OK] batch/$filename"
done

echo "=== Done: all scripts uploaded to s3://$SCRIPTS_BUCKET ==="
