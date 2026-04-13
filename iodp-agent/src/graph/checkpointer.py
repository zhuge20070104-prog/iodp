# src/graph/checkpointer.py
"""
基于 Amazon DynamoDB 的 LangGraph Checkpointer 实现
使用 langgraph-checkpoint-dynamodb（社区库）或自实现

关键设计：
- 每次 graph.invoke() 自动在 DynamoDB 中保存/恢复状态
- 支持 Human-in-the-loop：多轮对话中断后可从 thread_id 恢复
- TTL = 7天（对话过期后自动清理，FinOps）
"""

import boto3
from langgraph.checkpoint.dynamodb import DynamoDBSaver   # pip install langgraph-checkpoint-dynamodb
from src.config import settings


def get_checkpointer() -> DynamoDBSaver:
    """
    创建并返回 DynamoDB Checkpointer 实例
    """
    dynamodb_client = boto3.client("dynamodb", region_name=settings.aws_region)
    return DynamoDBSaver(
        client=dynamodb_client,
        table_name=settings.agent_state_table,
        ttl_attribute="TTL",
        ttl_seconds=7 * 24 * 3600,   # 7 天 TTL（FinOps）
    )
