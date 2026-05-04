# src/tools/s3_vectors_tool.py
"""
封装 Amazon S3 Vectors 向量检索工具
- 使用 Amazon Bedrock Embeddings (Titan Embeddings V2) 生成查询向量
- 支持多 index 混合检索（同一个 vector bucket 下多个 index）
- 支持按 error_codes 元数据过滤（pre-filter）

S3 Vectors 模型：
    vector bucket  ─┬── index "incident_solutions"
                    └── index "product_docs"

替代旧的 OpenSearch Serverless 方案，成本降低约 90%、查询延迟 100~800ms。
"""

from typing import Any, Dict, List, Optional

import boto3
import json

from src.config import settings


def _get_embedding(text: str, region: str) -> List[float]:
    """使用 Bedrock Titan Embeddings V2 生成文本向量"""
    bedrock = boto3.client("bedrock-runtime", region_name=region)
    response = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text, "dimensions": 1024, "normalize": True}),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())["embedding"]


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
        return []

    s3vectors = boto3.client("s3vectors", region_name=region)

    # 生成查询向量
    query_vector = _get_embedding(query_text, region)

    # 构建可选元数据过滤器（S3 Vectors 用 $in 表达数组成员关系）
    metadata_filter: Optional[Dict[str, Any]] = None
    if filter_error_codes:
        metadata_filter = {"error_codes": {"$in": filter_error_codes}}

    all_hits: List[Dict[str, Any]] = []
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

            response = s3vectors.query_vectors(**kwargs)
        except Exception as e:
            # 单个 index 失败不应中断流程
            print(f"S3 Vectors query on {index_name} failed: {e}")
            continue

        # S3 Vectors 返回 distance（越小越相似）；统一转为 score（越大越相似）
        for vec in response.get("vectors", []):
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

    return deduped_hits
