# scripts/index_knowledge_base.py
"""
离线脚本：将项目一 Gold 层的 incident_summary 数据批量索引到 OpenSearch
调用时机：每天凌晨 1 点，由 EventBridge 触发
"""

import boto3
import json
import awswrangler as wr
from opensearchpy import OpenSearch, RequestsHttpConnection, helpers
from requests_aws4auth import AWS4Auth

GOLD_DB    = "iodp_gold_prod"
GOLD_TABLE = "incident_summary"
OS_INDEX   = "incident_solutions"


def index_incidents(opensearch_client, bedrock_client, athena_df):
    """将 DataFrame 批量向量化并索引到 OpenSearch"""
    def embed(text: str):
        resp = bedrock_client.invoke_model(
            modelId="amazon.titan-embed-text-v2:0",
            body=json.dumps({"inputText": text, "dimensions": 1024}),
            contentType="application/json",
            accept="application/json",
        )
        return json.loads(resp["body"].read())["embedding"]

    actions = []
    for _, row in athena_df.iterrows():
        content = f"{row['title']}\n{row['symptoms']}\n{row['root_cause']}\n{row['resolution']}"
        vector  = embed(content)
        actions.append({
            "_index": OS_INDEX,
            "_id":    row["incident_id"],
            "_source": {
                "incident_id":      row["incident_id"],
                "title":            row["title"],
                "content":          content,
                "doc_type":         "incident_solution",
                "error_codes":      json.loads(row["error_codes"]) if row["error_codes"] else [],
                "affected_service": row["service_name"],
                "resolution":       row["resolution"],
                "severity":         row["severity"],
                "resolved_at":      row["resolved_at"],
                "embedding":        vector,
            }
        })

    # 分批写入，避免超出 OpenSearch 单次请求大小限制
    batch_size = 50
    for i in range(0, len(actions), batch_size):
        helpers.bulk(opensearch_client, actions[i:i+batch_size])
        print(f"Indexed {min(i+batch_size, len(actions))}/{len(actions)} documents")


if __name__ == "__main__":
    # 读取项目一 Gold 层的最新 incident_summary 数据
    df = wr.athena.read_sql_table(
        table=GOLD_TABLE,
        database=GOLD_DB,
        ctas_approach=False,
    )
    print(f"Loaded {len(df)} incidents from Gold layer")
    # ... 初始化客户端，调用索引函数
