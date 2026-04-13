# src/tools/opensearch_tool.py
"""
封装 Amazon OpenSearch Serverless 向量检索工具
- 使用 Amazon Bedrock Embeddings (Titan Embeddings V2) 生成向量
- 支持多 index 混合检索
- 支持按 error_code 字段过滤
"""

from typing import Any, Dict, List, Optional

import boto3
import json
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

from src.config import settings


def _get_opensearch_client(endpoint: str, region: str) -> OpenSearch:
    """构建带 IAM 认证的 OpenSearch 客户端"""
    session = boto3.Session()
    credentials = session.get_credentials()
    awsauth = AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        region,
        "aoss",   # Amazon OpenSearch Serverless 服务名
        session_token=credentials.token,
    )
    return OpenSearch(
        hosts=[{"host": endpoint, "port": 443}],
        http_auth=awsauth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
    )


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
    opensearch_endpoint: str = "",
    region: str = "us-east-1",
    filter_error_codes: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    在指定的 OpenSearch 索引中执行向量相似度检索
    支持 pre-filter 按 error_codes 过滤（提升精准度）
    """
    endpoint = opensearch_endpoint or settings.opensearch_endpoint
    client   = _get_opensearch_client(endpoint, region)

    # 生成查询向量
    query_vector = _get_embedding(query_text, region)

    all_hits = []
    for index_name in index_names:
        # 构建 kNN 查询（带可选 filter）
        query_body: Dict[str, Any] = {
            "size": top_k,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": query_vector,
                        "k": top_k,
                    }
                }
            },
            "_source": ["title", "content", "doc_type", "error_codes", "created_at"],
        }

        # 如果有 error_code 过滤条件，使用 bool + filter
        if filter_error_codes:
            query_body["query"] = {
                "bool": {
                    "must": [{"knn": {"embedding": {"vector": query_vector, "k": top_k * 2}}}],
                    "filter": [{"terms": {"error_codes": filter_error_codes}}],
                }
            }

        try:
            response = client.search(index=index_name, body=query_body)
            all_hits.extend(response["hits"]["hits"])
        except Exception as e:
            # 单个 index 失败不应中断流程
            print(f"OpenSearch query on {index_name} failed: {e}")

    # 按相关度排序，去重，返回 top_k
    all_hits.sort(key=lambda h: h["_score"], reverse=True)
    seen_ids = set()
    deduped_hits = []
    for hit in all_hits:
        if hit["_id"] not in seen_ids:
            seen_ids.add(hit["_id"])
            deduped_hits.append(hit)
        if len(deduped_hits) >= top_k:
            break

    return deduped_hits
