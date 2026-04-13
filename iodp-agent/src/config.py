# src/config.py
"""
全局配置管理 v2
改进：
  - max_clarification_iterations 从代码硬编码 (3) 改为可配置项
  - 新增 athena_max_rows 防止 Agent 获取过多行导致 Token 超限
  - 新增 async_job_ttl_seconds 控制异步 Job 记录过期时间
"""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # AWS 基础
    aws_region: str = "us-east-1"

    # Athena
    athena_result_bucket: str = "iodp-athena-results-prod"
    athena_workgroup: str = "primary"
    # 新增：限制 Athena 结果行数，防止超大结果集撑爆 AgentState 和 Bedrock Token
    athena_max_rows: int = 50

    # DynamoDB
    agent_state_table: str = "iodp-agent-state-prod"
    agent_jobs_table: str = "iodp-agent-jobs-prod"     # 异步 Job 跟踪表（新增）
    dq_reports_table: str = "iodp-dq-reports-prod"

    # OpenSearch
    opensearch_endpoint: str = ""

    # Bedrock
    bedrock_model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"

    # 环境
    environment: str = "prod"

    # Agent 行为配置（原来硬编码，现在可通过环境变量覆盖）
    # 最大追问轮数：超过此值强制进入 Synthesizer
    max_clarification_iterations: int = 3

    # 异步 Job TTL（秒）：Job 记录在 DynamoDB 中的保留时间
    async_job_ttl_seconds: int = 3600  # 1 小时

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # 允许通过 IODP_ 前缀的环境变量覆盖，例如 IODP_MAX_CLARIFICATION_ITERATIONS=5
        env_prefix = "IODP_"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


# 模块级单例，方便直接 import
settings = get_settings()
