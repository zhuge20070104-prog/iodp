# src/config.py
"""
全局配置管理 v2
改进：
  - max_clarification_iterations 从代码硬编码 (3) 改为可配置项
  - 新增 athena_max_rows 防止 Agent 获取过多行导致 Token 超限
  - 新增 async_job_ttl_seconds 控制异步 Job 记录过期时间
"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # AWS 基础
    aws_region: str = "us-east-1"

    # Athena
    athena_result_bucket: str = "iodp-athena-results-prod"
    athena_workgroup: str = "primary"
    # 新增：限制 Athena 结果行数，防止超大结果集撑爆 AgentState 和 LLM context window
    athena_max_rows: int = 50

    # DynamoDB
    agent_state_table: str = "iodp-agent-state-prod"
    agent_jobs_table: str = "iodp-agent-jobs-prod"     # 异步 Job 跟踪表（新增）
    bug_tickets_table: str = "iodp-bug-tickets-prod"  # Bug 报告工单归档表
    dq_reports_table: str = "iodp-dq-reports-prod"

    # S3 Vectors (replaces OpenSearch Serverless; GA 2025-12)
    # 单个 vector bucket 下挂多个 index：incident_solutions / product_docs
    vector_bucket_name: str = ""

    # ─── LLM (OpenAI 兼容 endpoint, 默认通义千问 qwen) ───
    # 中国大陆注册的 AWS 账号过不了 Bedrock allowlisting，改用第三方 OpenAI 兼容 API。
    # 用通义千问的原因：一个 dashscope key 同时支持 chat + embedding，简化运维。
    # 切换 provider 只改这几行 + 重 deploy 即可：
    #   DeepSeek:  base_url="https://api.deepseek.com"  chat=deepseek-chat（注：DeepSeek 无 embedding，要单独配 embedding provider）
    #   通义千问:  base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"  chat=qwen-max/qwen-turbo  embed=text-embedding-v3
    #   智谱 GLM:   base_url="https://open.bigmodel.cn/api/paas/v4"  chat=glm-4/glm-4-flash  embed=embedding-3
    #   OpenAI:    base_url 留空（默认）  chat=gpt-4o/gpt-4o-mini  embed=text-embedding-3-small
    llm_base_url:        str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key:         str = ""             # 通过环境变量 IODP_LLM_API_KEY 注入
    llm_reasoning_model: str = "qwen-max"     # 复杂推理
    llm_router_model:    str = "qwen-turbo"   # 简单分类（便宜 ~5x）

    # ─── Embedding (OpenAI 兼容 endpoint，默认复用 LLM provider) ───
    # base_url / api_key 留空时复用 llm_base_url / llm_api_key
    embedding_base_url:   str = ""                      # 留空 = 复用 llm_base_url
    embedding_api_key:    str = ""                      # 留空 = 复用 llm_api_key
    embedding_model:      str = "text-embedding-v3"     # qwen v3，输出 1024 维
    embedding_dimensions: int = 1024                    # 跟 S3 Vectors index dimension 必须一致

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
