# src/graph/nodes/reply_agent.py
"""
Reply Agent 节点（从原 Synthesizer 拆分）
职责：ONLY 生成用户友好的自然语言回复
  - 温和、专业、有同理心
  - 绝对不暴露内部错误码、堆栈、技术细节
  - 与 BugReportAgent 并行执行（LangGraph fan-out）
"""

from langchain_aws import ChatBedrock
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ..state import (
    AgentState, SynthesizerOutput,
    get_error_logs, get_retrieved_docs, get_user_id, get_intent, get_incident_time_hint,
)
from src.config import settings

REPLY_SYSTEM_PROMPT = """
你是一个企业级智能客服系统的用户回复专家。

【回复规则】
- 语气：温和、专业、有同理心
- 结构：致歉 → 故障确认（不暴露技术细节）→ 预计处理时间/指引
- 长度：3-5句话，不超过150字
- 禁止暴露：错误码（E2001等）、堆栈（Traceback、stack trace）、服务器地址、数据库名

【预计修复时间规则】
- 如果有明确证据（错误率 > 20%）：说"我们已定位到问题，正在紧急修复，预计1小时内恢复"
- 如果证据有限：说"我们已记录您的问题并正在排查，请稍候"
- 退款场景：说"退款申请已收到，将在3-5个工作日内处理"

【安全规则】
- 不要重复用户的技术投诉内容
- 如果用户意图是 security_violation，回复"非常抱歉，您的请求无法处理，如有疑问请联系客服。"
""".strip()


def reply_agent_node(state: AgentState) -> dict:
    """
    Reply Agent 节点：生成用户回复
    与 BugReportAgent 并行运行，各自只填充 synthesizer 的一部分字段
    """
    error_logs     = get_error_logs(state)
    retrieved_docs = get_retrieved_docs(state)
    raw_input      = state.get("raw_user_input", "")
    intent         = get_intent(state) or "unknown"
    time_hint      = get_incident_time_hint(state) or "近期"

    # 构建上下文（只传必要信息，不传原始 SQL/堆栈）
    top_error_rate = max((log["error_rate"] for log in error_logs), default=0.0)
    has_evidence   = bool(error_logs or retrieved_docs)

    context = (
        f"用户原始投诉：{raw_input}\n"
        f"意图分类：{intent}\n"
        f"故障时间：{time_hint}\n"
        f"是否有日志证据：{'是' if has_evidence else '否'}\n"
        f"最高错误率：{top_error_rate:.1%}"
    )

    llm = ChatBedrock(
        model_id=settings.bedrock_model_id,
        region_name=settings.aws_region,
        model_kwargs={"max_tokens": 512, "temperature": 0.1},
    )

    response = llm.invoke([
        SystemMessage(content=REPLY_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])

    user_reply = response.content.strip()
    if not user_reply:
        user_reply = "非常抱歉给您带来了不便。我们已记录您的问题并正在紧急排查，请稍候。"

    return {
        # merge_synthesizer reducer 会将此与 BugReportAgent 的输出合并
        "synthesizer": SynthesizerOutput(
            user_reply=user_reply,
            bug_report=None,
        ),
        "messages": [AIMessage(content=user_reply)],
    }
