# lambda/vector_indexer/handler.py
"""
事件驱动 S3 Vectors 索引器
触发方式：Gold S3 incident_summary/*.parquet 文件写入时 → S3 Event → Lambda

职责：
  1. 从 S3 event 解析 bucket/key
  2. 读取 Parquet 文件（incident_summary 表）
  3. 对每条记录调用 Bedrock Titan Embeddings V2 生成向量
  4. 批量写入 S3 Vectors 的 incident_solutions 索引

Response:
  {"indexed": N, "failed": M, "source_key": "..."}
"""

import json
import logging
import os
from typing import Any, Dict, List
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

VECTOR_BUCKET_NAME = os.environ["VECTOR_BUCKET_NAME"]
INDEX_NAME         = os.environ.get("INDEX_NAME", "incident_solutions")
BEDROCK_REGION     = os.environ.get("BEDROCK_REGION", "us-east-1")
ENVIRONMENT        = os.environ.get("ENVIRONMENT", "prod")
# put_vectors 单次最多 500 条，默认 50 安全
BATCH_SIZE         = int(os.environ.get("BATCH_SIZE", "50"))

bedrock    = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
s3         = boto3.client("s3")
s3vectors  = boto3.client("s3vectors", region_name=BEDROCK_REGION)


def _get_embedding(text: str) -> List[float]:
    """调用 Bedrock Titan Embeddings V2 生成 1024 维向量"""
    response = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text[:8000], "dimensions": 1024, "normalize": True}),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())["embedding"]


def _read_parquet_from_s3(bucket: str, key: str):
    """从 S3 下载 Parquet 文件并解析为 DataFrame（pandas）"""
    import io
    import pyarrow.parquet as pq

    response = s3.get_object(Bucket=bucket, Key=key)
    buffer   = io.BytesIO(response["Body"].read())
    table    = pq.read_table(buffer)
    return table.to_pandas()


def _build_vector_record(row: Dict[str, Any], vector: List[float]) -> Dict[str, Any]:
    """将 incident_summary 行转换为 S3 Vectors put_vectors 记录格式"""
    error_codes = row.get("error_codes", "[]")
    if isinstance(error_codes, str):
        try:
            error_codes = json.loads(error_codes)
        except (json.JSONDecodeError, TypeError):
            error_codes = []

    content = " ".join(filter(None, [
        str(row.get("title", "")),
        str(row.get("symptoms", "")),
        str(row.get("root_cause", "")),
        str(row.get("resolution", "")),
    ]))

    return {
        "key": str(row.get("incident_id", "")),
        "data": {"float32": vector},
        "metadata": {
            # 可过滤字段（在 index resource 中未列入 nonFilterableMetadataKeys）
            "doc_type":         "incident_solution",
            "error_codes":      error_codes,
            "source_env":       ENVIRONMENT,
            # 不可过滤字段（仅 returnMetadata 时回传，定义见 terraform vector_store 模块）
            "incident_id":      str(row.get("incident_id", "")),
            "title":            str(row.get("title", "")),
            "content":          content,
            "affected_service": str(row.get("service_name", "")),
            "resolution":       str(row.get("resolution", "")),
            "severity":         str(row.get("severity", "P3")),
            "resolved_at":      str(row.get("resolved_at", "")),
        },
    }


def _flush_batch(records: List[Dict[str, Any]]) -> tuple[int, int]:
    """提交一批 vector 到 S3 Vectors。返回 (indexed, failed)"""
    if not records:
        return 0, 0
    try:
        s3vectors.put_vectors(
            vectorBucketName=VECTOR_BUCKET_NAME,
            indexName=INDEX_NAME,
            vectors=records,
        )
        return len(records), 0
    except Exception as e:
        logger.warning("put_vectors batch of %d failed: %s", len(records), e)
        return 0, len(records)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """主入口"""
    records = event.get("Records", [])
    if not records:
        logger.warning("No S3 records in event")
        return {"indexed": 0, "failed": 0, "source_key": None}

    record = records[0]
    bucket = record["s3"]["bucket"]["name"]
    key    = unquote_plus(record["s3"]["object"]["key"])

    logger.info("Processing S3 event: s3://%s/%s", bucket, key)

    try:
        df = _read_parquet_from_s3(bucket, key)
    except Exception as e:
        logger.error("Failed to read Parquet file s3://%s/%s: %s", bucket, key, e)
        raise  # Raise to trigger SQS DLQ

    indexed = 0
    failed  = 0
    batch: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        try:
            content = " ".join(filter(None, [
                str(row.get("title", "")),
                str(row.get("symptoms", "")),
                str(row.get("root_cause", "")),
                str(row.get("resolution", "")),
            ]))
            vector = _get_embedding(content)
            batch.append(_build_vector_record(row.to_dict(), vector))
        except Exception as e:
            logger.warning("Failed to generate embedding for incident %s: %s", row.get("incident_id"), e)
            failed += 1

        if len(batch) >= BATCH_SIZE:
            ok, ko = _flush_batch(batch)
            indexed += ok
            failed  += ko
            batch    = []

    if batch:
        ok, ko = _flush_batch(batch)
        indexed += ok
        failed  += ko

    logger.info(
        "Indexing complete: %d indexed, %d failed, source=s3://%s/%s",
        indexed, failed, bucket, key,
    )

    return {
        "indexed":      indexed,
        "failed":       failed,
        "source_key":   key,
        "vector_index": INDEX_NAME,
    }
