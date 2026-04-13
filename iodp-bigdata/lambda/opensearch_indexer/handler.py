# lambda/opensearch_indexer/handler.py
"""
事件驱动 OpenSearch 索引器
触发方式：Gold S3 incident_summary/*.parquet 文件写入时 → S3 Event → Lambda

职责：
  1. 从 S3 event 解析 bucket/key
  2. 读取 Parquet 文件（incident_summary 表）
  3. 对每条记录调用 Bedrock Titan Embeddings V2 生成向量
  4. 批量写入 OpenSearch incident_solutions 索引

Response:
  {"indexed": N, "failed": M, "source_key": "..."}
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

OPENSEARCH_ENDPOINT = os.environ["OPENSEARCH_ENDPOINT"]
INDEX_NAME          = os.environ.get("INDEX_NAME", "incident_solutions")
BEDROCK_REGION      = os.environ.get("BEDROCK_REGION", "us-east-1")
ENVIRONMENT         = os.environ.get("ENVIRONMENT", "prod")
BATCH_SIZE          = int(os.environ.get("BATCH_SIZE", "50"))

bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
s3      = boto3.client("s3")


def _get_embedding(text: str) -> List[float]:
    """调用 Bedrock Titan Embeddings V2 生成 1024 维向量"""
    response = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text[:8000], "dimensions": 1024, "normalize": True}),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())["embedding"]


def _get_opensearch_client():
    """构建带 AWS SigV4 认证的 OpenSearch 客户端"""
    import boto3
    from opensearchpy import OpenSearch, RequestsHttpConnection
    from requests_aws4auth import AWS4Auth

    session   = boto3.Session()
    creds     = session.get_credentials()
    awsauth   = AWS4Auth(
        creds.access_key,
        creds.secret_key,
        BEDROCK_REGION,
        "aoss",
        session_token=creds.token,
    )
    host = OPENSEARCH_ENDPOINT.replace("https://", "")
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=awsauth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30,
    )


def _read_parquet_from_s3(bucket: str, key: str):
    """从 S3 下载 Parquet 文件并解析为 DataFrame（pandas）"""
    import io
    import pandas as pd
    import pyarrow.parquet as pq

    response = s3.get_object(Bucket=bucket, Key=key)
    buffer   = io.BytesIO(response["Body"].read())
    table    = pq.read_table(buffer)
    return table.to_pandas()


def _build_os_document(row, vector: List[float]) -> Dict[str, Any]:
    """将 incident_summary 行转换为 OpenSearch 文档格式"""
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
        "_index": INDEX_NAME,
        "_id":    str(row.get("incident_id", "")),
        "_source": {
            "incident_id":      str(row.get("incident_id", "")),
            "title":            str(row.get("title", "")),
            "content":          content,
            "doc_type":         "incident_solution",
            "error_codes":      error_codes,
            "affected_service": str(row.get("service_name", "")),
            "resolution":       str(row.get("resolution", "")),
            "severity":         str(row.get("severity", "P3")),
            "resolved_at":      str(row.get("resolved_at", "")),
            "embedding":        vector,
            "source_env":       ENVIRONMENT,
        },
    }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """主入口"""
    from opensearchpy import helpers

    records = event.get("Records", [])
    if not records:
        logger.warning("No S3 records in event")
        return {"indexed": 0, "failed": 0, "source_key": None}

    record   = records[0]
    bucket   = record["s3"]["bucket"]["name"]
    key      = unquote_plus(record["s3"]["object"]["key"])

    logger.info("Processing S3 event: s3://%s/%s", bucket, key)

    try:
        df = _read_parquet_from_s3(bucket, key)
    except Exception as e:
        logger.error("Failed to read Parquet file s3://%s/%s: %s", bucket, key, e)
        raise  # Raise to trigger SQS DLQ

    os_client = _get_opensearch_client()
    indexed   = 0
    failed    = 0
    actions   = []

    for _, row in df.iterrows():
        try:
            content = " ".join(filter(None, [
                str(row.get("title", "")),
                str(row.get("symptoms", "")),
                str(row.get("root_cause", "")),
                str(row.get("resolution", "")),
            ]))
            vector  = _get_embedding(content)
            doc     = _build_os_document(row.to_dict(), vector)
            actions.append(doc)
        except Exception as e:
            logger.warning("Failed to generate embedding for incident %s: %s", row.get("incident_id"), e)
            failed += 1

        # 批量写入
        if len(actions) >= BATCH_SIZE:
            success, errors = helpers.bulk(os_client, actions, raise_on_error=False)
            indexed += success
            if errors:
                logger.warning("Bulk index had %d errors: %s", len(errors), errors[:3])
                failed += len(errors)
            actions = []

    # 写入剩余记录
    if actions:
        success, errors = helpers.bulk(os_client, actions, raise_on_error=False)
        indexed += success
        if errors:
            failed += len(errors)

    logger.info(
        "Indexing complete: %d indexed, %d failed, source=s3://%s/%s",
        indexed, failed, bucket, key,
    )

    return {
        "indexed":    indexed,
        "failed":     failed,
        "source_key": key,
        "index":      INDEX_NAME,
    }
