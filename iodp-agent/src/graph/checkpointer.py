# src/graph/checkpointer.py
"""
基于 Amazon DynamoDB 的 LangGraph Checkpointer 实现
使用 langgraph-checkpoint-dynamodb（社区库）或自实现

关键设计：
- 每次 graph.invoke() 自动在 DynamoDB 中保存/恢复状态
- 支持 Human-in-the-loop：多轮对话中断后可从 thread_id 恢复
- TTL = 7天（对话过期后自动清理，FinOps）
"""

from langgraph.checkpoint.memory import MemorySaver


# 模块级单例：同一 Lambda 容器内所有 invocation 共享同一份 checkpointer，
# 使多轮对话（thread_id 复用）能跨 invocation 恢复历史状态。
# 之前每次 get_checkpointer() 都 new 一个新 MemorySaver，导致多轮对话丢失上下文。
_CHECKPOINTER = MemorySaver()


def get_checkpointer() -> MemorySaver:
    """
    多轮对话状态 = Lambda 容器内存（单例）。
    - 同一容器复用期间（5-15 min 空闲窗口）多轮对话连续。
    - Lambda 冷启动后状态丢失。Demo 单次对话足够。

    升级到 DynamoDB 持久化的路径（待办）：
    - 当前社区包 langgraph-checkpoint-dynamodb (Justin Ramsey) 要求两张表：
      checkpoints_table + writes_table，schema 跟现有 iodp-agent-state-{env} 不兼容。
    - 需在 main.tf 增加第二张 dynamodb 表，再回切到 DynamoDBSaver(checkpoints_table_name=..., writes_table_name=..., client_config={"region_name": ...})。
    """
    return _CHECKPOINTER
