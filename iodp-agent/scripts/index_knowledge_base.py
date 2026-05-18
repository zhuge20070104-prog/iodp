# scripts/index_knowledge_base.py
"""
离线脚本：从 Athena 把 Gold 层 incident_summary 全量灌进 S3 Vectors。

定位（"冷启动"）：
  - 首次部署完，S3 Vectors index 是空的，agent RAG 检索不到任何东西。
  - 这个脚本读 3 年累积的历史 incident_summary，一次性向量化后写入。
  - 之后日常增量由 bigdata 的 vector_indexer Lambda 处理（S3 Event 驱动）。

兼任"全量重建"应急工具：embedding 模型升级 / 向量维度变更时也用得上。
"""

import argparse
import json
from typing import Any, Dict, List

import awswrangler as wr
import boto3

import os
from openai import OpenAI

GOLD_TABLE       = "incident_summary"
DEFAULT_INDEX    = "incident-solutions"
# embedding 用 OpenAI 兼容 endpoint（默认 DashScope text-embedding-v3, 1024 维）
# 与 iodp-agent 查询侧 + iodp-bigdata vector_indexer 用同一模型，保证向量空间一致。
EMBED_BASE_URL   = os.environ.get("IODP_EMBEDDING_BASE_URL") or os.environ.get("IODP_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
EMBED_API_KEY    = os.environ.get("IODP_EMBEDDING_API_KEY") or os.environ.get("IODP_LLM_API_KEY", "")
EMBED_MODEL_ID   = os.environ.get("IODP_EMBEDDING_MODEL", "text-embedding-v3")
EMBED_DIMENSION  = int(os.environ.get("IODP_EMBEDDING_DIMENSIONS", "1024"))
PUT_BATCH_SIZE   = 50  # S3 Vectors put_vectors 单次最多 500 条


def embed(text: str) -> List[float]:
    """通过 OpenAI 兼容 endpoint 生成文档向量。"""
    client = OpenAI(api_key=EMBED_API_KEY, base_url=EMBED_BASE_URL)
    resp = client.embeddings.create(
        model=EMBED_MODEL_ID,
        input=text[:2048],
        dimensions=EMBED_DIMENSION,
    )
    return resp.data[0].embedding


def build_record(row, vector: List[float]) -> Dict[str, Any]:
    """将 incident_summary 行转换为 put_vectors 记录格式"""
    error_codes = row["error_codes"]
    if isinstance(error_codes, str):
        try:
            error_codes = json.loads(error_codes)
        except (json.JSONDecodeError, TypeError):
            error_codes = []
    elif error_codes is None:
        error_codes = []

    content = "\n".join(filter(None, [
        str(row.get("title") or ""),
        str(row.get("symptoms") or ""),
        str(row.get("root_cause") or ""),
        str(row.get("resolution") or ""),
    ]))

    return {
        "key": str(row["incident_id"]),
        "data": {"float32": vector},
        "metadata": {
            # 可过滤
            "doc_type":         "incident_solution",
            "error_codes":      error_codes,
            # 不可过滤（仅 returnMetadata 时回传，便宜）
            "incident_id":      str(row["incident_id"]),
            "title":            str(row.get("title") or ""),
            "content":          content,
            "affected_service": str(row.get("service_name") or ""),
            "resolution":       str(row.get("resolution") or ""),
            "severity":         str(row.get("severity") or "P3"),
            "resolved_at":      str(row.get("resolved_at") or ""),
        },
    }


def index_incidents(s3vectors_client, df, vector_bucket: str, index_name: str) -> int:
    """全量向量化并分批写入 S3 Vectors。返回写入条数。"""
    batch: List[Dict[str, Any]] = []
    written = 0
    total   = len(df)

    for _, row in df.iterrows():
        try:
            content = "\n".join(filter(None, [
                str(row.get("title") or ""),
                str(row.get("symptoms") or ""),
                str(row.get("root_cause") or ""),
                str(row.get("resolution") or ""),
            ]))
            vector = embed(content)
            batch.append(build_record(row, vector))
        except Exception as e:
            print(f"[WARN] embedding failed for {row.get('incident_id')}: {e}")
            continue

        if len(batch) >= PUT_BATCH_SIZE:
            s3vectors_client.put_vectors(
                vectorBucketName=vector_bucket,
                indexName=index_name,
                vectors=batch,
            )
            written += len(batch)
            print(f"Indexed {written}/{total} documents")
            batch = []

    if batch:
        s3vectors_client.put_vectors(
            vectorBucketName=vector_bucket,
            indexName=index_name,
            vectors=batch,
        )
        written += len(batch)
        print(f"Indexed {written}/{total} documents")

    return written


def main():
    parser = argparse.ArgumentParser(description="全量索引 incident_summary 到 S3 Vectors")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--env", default="dev")
    parser.add_argument("--vector-bucket", required=True,
                        help="S3 Vectors bucket name (来自 iodp-agent terraform output vector_bucket_name)")
    parser.add_argument("--index-name", default=DEFAULT_INDEX)
    args = parser.parse_args()

    gold_db = f"iodp_gold_{args.env}"

    # ─── 1. 从 Gold 层读取全量 incident_summary ───
    print(f"Reading from Athena: {gold_db}.{GOLD_TABLE}")
    df = wr.athena.read_sql_table(
        table=GOLD_TABLE,
        database=gold_db,
        ctas_approach=False,
        boto3_session=boto3.Session(region_name=args.region),
    )
    print(f"Loaded {len(df)} incidents from Gold layer")

    if df.empty:
        print("No incidents to index. Exiting.")
        return

    # ─── 2. 初始化 S3 Vectors 客户端 ───
    # （embedding 走 OpenAI 兼容 client，在 embed() 内部 lazy 初始化）
    s3vectors = boto3.client("s3vectors", region_name=args.region)

    # ─── 3. 全量索引 ───
    written = index_incidents(s3vectors, df, args.vector_bucket, args.index_name)
    print(f"Done. Indexed {written}/{len(df)} incidents to s3vectors://{args.vector_bucket}/{args.index_name}")


if __name__ == "__main__":
    main()
