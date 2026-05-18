# src/tools/s3_vectors_tool.py
"""
封装 Amazon S3 Vectors 向量检索工具
- 使用 DashScope text-embedding-v3（OpenAI 兼容 endpoint）生成查询向量
- 与 iodp-bigdata/lambda/vector_indexer 写入侧用同一个 embedding 模型，
  保证 query 向量和 indexed 向量在同一语义空间
- 支持多 index 混合检索（同一个 vector bucket 下多个 index）
- 支持按 error_codes 元数据过滤（pre-filter）

S3 Vectors 模型：
    vector bucket  ─┬── index "incident_solutions"
                    └── index "product_docs"

替代旧的 OpenSearch Serverless 方案，成本降低约 90%、查询延迟 100~800ms。
"""

import logging
from typing import Any, Dict, List, Optional

import boto3
import json

from src.config import settings
from src.log_utils import Timer, log_event

logger = logging.getLogger(__name__)


def _get_embedding(text: str, region: str = None) -> List[float]:
    """通过 OpenAI 兼容 endpoint 获取查询向量（默认通义千问 text-embedding-v3, 1024 维）。

    AWS Bedrock 在中国注册账号下整体被 allowlist，所以 embedding 也不能用 Bedrock，
    改走 OpenAI 兼容 API。`region` 参数保留只为向后兼容，实际不再使用。
    """
    from openai import OpenAI  # lazy import 避免 module 初始化时报错
    client = OpenAI(
        api_key=settings.embedding_api_key or settings.llm_api_key,
        base_url=settings.embedding_base_url or settings.llm_base_url,
    )
    with Timer() as t:
        resp = client.embeddings.create(
            model=settings.embedding_model,
            input=text[:2048],
            dimensions=settings.embedding_dimensions,
        )
    vec = resp.data[0].embedding
    log_event(
        "embedding", "success",
        model=settings.embedding_model,
        input_chars=len(text),
        dimensions=len(vec),
        elapsed_ms=t.elapsed_ms,
    )
    return vec


def vector_search(
    query_text: str,
    index_names: List[str],
    top_k: int = 5,
    vector_bucket_name: str = "",
    region: str = "us-east-1",
    filter_error_codes: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    在 S3 Vectors 的指定多个 index 中执行向量相似度检索。
    返回结构与旧 OpenSearch 工具兼容：list of {_id, _score, _source}。
    """
    bucket = vector_bucket_name or settings.vector_bucket_name
    if not bucket:
        log_event("s3vectors", "skip", reason="empty_bucket_name")
        return []

    s3vectors = boto3.client("s3vectors", region_name=region)

    # 构建可选元数据过滤器（S3 Vectors 用 $in 表达数组成员关系）
    metadata_filter: Optional[Dict[str, Any]] = None
    if filter_error_codes:
        metadata_filter = {"error_codes": {"$in": filter_error_codes}}

    log_event(
        "s3vectors", "start",
        bucket=bucket, region=region,
        index_names=index_names, top_k=top_k,
        filter_error_codes=filter_error_codes,
        query_chars=len(query_text),
    )

    # 生成查询向量
    query_vector = _get_embedding(query_text, region)

    all_hits: List[Dict[str, Any]] = []
    with Timer() as total_t:
        for index_name in index_names:
            try:
                kwargs: Dict[str, Any] = {
                    "vectorBucketName": bucket,
                    "indexName":        index_name,
                    "queryVector":      {"float32": query_vector},
                    "topK":             top_k * 2 if metadata_filter else top_k,
                    "returnMetadata":   True,
                    "returnDistance":   True,
                }
                if metadata_filter is not None:
                    kwargs["filter"] = metadata_filter

                with Timer() as idx_t:
                    response = s3vectors.query_vectors(**kwargs)
                vectors = response.get("vectors", [])
                log_event(
                    "s3vectors", "index_success",
                    bucket=bucket, index=index_name,
                    returned=len(vectors),
                    top_score=(1.0 - vectors[0].get("distance", 0.0)) if vectors else None,
                    elapsed_ms=idx_t.elapsed_ms,
                )
            except Exception as e:
                log_event(
                    "s3vectors", "index_error",
                    bucket=bucket, index=index_name, error=str(e),
                )
                continue

            # S3 Vectors 返回 distance（越小越相似）；统一转为 score（越大越相似）
            for vec in vectors:
                distance = vec.get("distance", 0.0)
                score    = 1.0 - distance  # cosine distance ∈ [0,2] → score ∈ [-1,1]
                metadata = vec.get("metadata") or {}
                all_hits.append({
                    "_id":     vec.get("key", ""),
                    "_score":  score,
                    "_source": {
                        "title":       metadata.get("title", ""),
                        "content":     metadata.get("content", ""),
                        "doc_type":    metadata.get("doc_type", "product_doc"),
                        "error_codes": metadata.get("error_codes", []),
                        "created_at":  metadata.get("created_at", ""),
                    },
                })

    # 跨 index 按 score 排序，去重，返回 top_k
    all_hits.sort(key=lambda h: h["_score"], reverse=True)
    seen_ids: set = set()
    deduped_hits: List[Dict[str, Any]] = []
    for hit in all_hits:
        if hit["_id"] not in seen_ids:
            seen_ids.add(hit["_id"])
            deduped_hits.append(hit)
        if len(deduped_hits) >= top_k:
            break

    log_event(
        "s3vectors", "success",
        bucket=bucket,
        merged_hits=len(deduped_hits),
        top_score=deduped_hits[0]["_score"] if deduped_hits else None,
        elapsed_ms=total_t.elapsed_ms,
    )
    return deduped_hits
