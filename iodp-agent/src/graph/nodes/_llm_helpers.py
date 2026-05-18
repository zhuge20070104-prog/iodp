# src/graph/nodes/_llm_helpers.py
"""
LLM 调用辅助：模型路由。

设计（DashScope Qwen via OpenAI 兼容 API）：
  - build_router_llm()    → router/rag/reply 用，模型默认 qwen-turbo（便宜）
  - build_reasoning_llm() → log_analyzer/bug_report 用，模型默认 qwen-max（聪明）
  - cached_system(text)   → 仅构造 SystemMessage 的薄包装（旧 Bedrock 时代叫这名，
                            历史背景在函数 docstring 里）

历史：项目曾用 Bedrock Claude + Anthropic Prompt Caching，AWS 中国账号过不了
allowlisting 后切到 DashScope。Prompt Caching 机制随之失效（Qwen 没有等价
特性），但函数名和调用点保留以减少改动面。
"""

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

from src.config import settings


# 用 OpenAI 兼容 API（默认 DeepSeek，可通过 settings.llm_base_url 切换通义/智谱/OpenAI）
# 不用 AWS Bedrock：中国大陆注册的 AWS 账号被拒（Anthropic/Bedrock allowlisting 都过不了）
def build_reasoning_llm(*, max_tokens: int, temperature: float = 0) -> ChatOpenAI:
    """复杂推理节点：log_analyzer / bug_report"""
    return ChatOpenAI(
        model=settings.llm_reasoning_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def build_router_llm(*, max_tokens: int, temperature: float = 0) -> ChatOpenAI:
    """简单分类/格式化节点：router / rag / reply"""
    return ChatOpenAI(
        model=settings.llm_router_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def cached_system(text: str) -> SystemMessage:
    """
    构造 SystemMessage。

    历史背景：函数名 "cached_system" 是早期用 Bedrock + Anthropic Prompt Caching
    时遗留的——当时会构造 list-of-blocks + cache_control=ephemeral 让 Claude 复用
    SystemMessage 的 KV cache，省一半 token。
    切到 DashScope (Qwen via OpenAI 兼容 API) 后这个机制没了，函数退化为普通
    SystemMessage 构造。保留旧函数名仅为减少调用点改动。
    """
    return SystemMessage(content=text)
