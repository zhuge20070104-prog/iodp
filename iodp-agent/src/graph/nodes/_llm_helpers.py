# src/graph/nodes/_llm_helpers.py
"""
LLM 调用辅助：模型路由 + Prompt Caching。

设计：
  - build_router_llm()    → router/rag/reply 使用，模型默认 Claude 3.5 Haiku
  - build_reasoning_llm() → log_analyzer/bug_report 使用，模型默认 Claude 3.5 Sonnet
  - cached_system(text)   → 把长 system prompt 包成 list-of-blocks +
                            cache_control={"type":"ephemeral"}，让 Bedrock
                            走 prompt cache（5 分钟内重复请求 input 走 cache）

Prompt Caching 限制：Anthropic 要求被缓存块达到模型最小 token 数
（Sonnet 3.5 ≥ 1024 tokens，Haiku 3.5 ≥ 2048 tokens）。不达标时 cache_control
被静默忽略，不影响功能，因此对所有节点统一开启即可。
"""

from typing import Any, List

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
    构造一个支持 Anthropic Prompt Caching 的 SystemMessage。

    - 开关关闭时退化为普通字符串 SystemMessage（保持向后兼容）
    - 开关打开时使用 list-of-blocks 形式 + cache_control={"type":"ephemeral"}
    """
    if not settings.bedrock_prompt_cache_enabled:
        return SystemMessage(content=text)

    content: List[Any] = [{
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }]
    return SystemMessage(content=content)
