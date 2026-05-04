# examples/langgraph_claude_sdk_example.py
"""
LangGraph + Claude SDK 直接调用的完整示例

对比当前项目的 ChatBedrock 方式：
  当前：  llm = ChatBedrock(...); llm.invoke([SystemMessage(...), HumanMessage(...)])
  本例：  client = anthropic.Anthropic(); client.messages.create(model=..., messages=[...])

LangGraph 负责流程编排（图、路由、state、checkpointer），
Claude SDK 负责每个节点内的 LLM 推理。

运行方式（需要设置 ANTHROPIC_API_KEY 环境变量）：
  pip install anthropic langgraph
  python examples/langgraph_claude_sdk_example.py
"""

import json
import os
from typing import Annotated, Any, Dict, List, Optional
from typing_extensions import TypedDict

import anthropic
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages


# ═══════════════════════════════════════════════════════════════
# 1. State 定义（和当前项目 state.py 结构一致）
# ═══════════════════════════════════════════════════════════════

class RouterOutput(TypedDict):
    intent: Optional[str]
    user_id: Optional[str]
    incident_time_hint: Optional[str]
    clarification_question: Optional[str]


class LogAnalyzerOutput(TypedDict):
    error_logs: List[Dict[str, Any]]
    sql: Optional[str]


class RAGOutput(TypedDict):
    retrieved_docs: List[Dict[str, Any]]
    query: Optional[str]


class SynthesizerOutput(TypedDict):
    user_reply: Optional[str]
    bug_report: Optional[Dict[str, Any]]


def merge_synthesizer(a: Optional[SynthesizerOutput], b: Optional[SynthesizerOutput]):
    if a is None: return b
    if b is None: return a
    return SynthesizerOutput(
        user_reply=a.get("user_reply") or b.get("user_reply"),
        bug_report=a.get("bug_report") or b.get("bug_report"),
    )


class AgentState(TypedDict):
    messages: Annotated[List[Dict], add_messages]
    raw_user_input: str
    iteration_count: int
    router: Optional[RouterOutput]
    log_analyzer: Optional[LogAnalyzerOutput]
    rag: Optional[RAGOutput]
    synthesizer: Annotated[Optional[SynthesizerOutput], merge_synthesizer]


# ═══════════════════════════════════════════════════════════════
# 2. Claude SDK 客户端（全局单例）
# ═══════════════════════════════════════════════════════════════

client = anthropic.Anthropic()
MODEL = "claude-sonnet-4-20250514"


def call_claude(system: str, user_content: str, max_tokens: int = 2048) -> str:
    """封装 Claude SDK 调用，所有节点共用"""
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return response.content[0].text


def call_claude_with_tools(
    system: str,
    user_content: str,
    tools: List[Dict],
    max_tokens: int = 2048,
) -> Dict:
    """
    封装 Claude SDK tool_use 调用。
    Claude 返回 tool_use block 时，提取工具名和参数。
    用于 log_analyzer 节点（Claude 自主决定 SQL 参数）。
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        tools=tools,
        messages=[{"role": "user", "content": user_content}],
    )
    # 提取 tool_use 和 text
    result = {"text": None, "tool_calls": []}
    for block in response.content:
        if block.type == "text":
            result["text"] = block.text
        elif block.type == "tool_use":
            result["tool_calls"].append({
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return result


# ═══════════════════════════════════════════════════════════════
# 3. Node 函数（每个节点用 Claude SDK 做推理）
# ═══════════════════════════════════════════════════════════════

def router_node(state: AgentState) -> dict:
    """Router Agent：意图分类 + 信息提取"""
    system = """你是一个客服路由器。根据用户消息，输出 JSON：
{
  "intent": "tech_issue" | "inquiry" | "refund" | "need_more_info",
  "user_id": "提取到的用户ID或null",
  "incident_time_hint": "提取到的时间描述或null",
  "clarification_question": "如果信息不足，写追问问题；否则null"
}
只输出 JSON，不要解释。"""

    raw_input = state.get("raw_user_input", "")
    response_text = call_claude(system, raw_input, max_tokens=512)

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError:
        parsed = {"intent": "need_more_info", "clarification_question": "请描述您遇到的问题。"}

    return {
        "router": RouterOutput(
            intent=parsed.get("intent"),
            user_id=parsed.get("user_id"),
            incident_time_hint=parsed.get("incident_time_hint"),
            clarification_question=parsed.get("clarification_question"),
        ),
        "iteration_count": state.get("iteration_count", 0) + 1,
    }


def log_analyzer_node(state: AgentState) -> dict:
    """
    Log Analyzer：用 Claude SDK tool_use 生成 SQL。
    这里展示了 LangGraph 节点内使用 tool_use 的模式 —
    LangGraph 控制整体流程，Claude 在节点内自主决定工具参数。
    """
    router = state.get("router") or {}
    user_id = router.get("user_id", "unknown")
    time_hint = router.get("incident_time_hint", "最近1小时")

    # 定义一个 tool 让 Claude 生成查询参数
    tools = [{
        "name": "query_error_logs",
        "description": "查询 Athena 错误日志视图 v_error_log_enriched",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "完整的 SELECT SQL 语句",
                },
            },
            "required": ["sql"],
        },
    }]

    system = """你是 Athena SQL 专家。根据用户信息生成查询 v_error_log_enriched 视图的 SQL。
规则：只用 SELECT，按 error_rate DESC 排序，LIMIT 50。"""

    result = call_claude_with_tools(
        system=system,
        user_content=f"用户ID: {user_id}\n故障时间: {time_hint}",
        tools=tools,
    )

    # 提取 Claude 生成的 SQL
    sql = None
    if result["tool_calls"]:
        sql = result["tool_calls"][0]["input"].get("sql")

    # 模拟 Athena 查询结果（实际项目中调用 athena_tool.execute_athena_query）
    mock_logs = [
        {
            "stat_hour": "2026-04-15 23:00:00",
            "service_name": "payment-service",
            "error_code": "E2001",
            "error_rate": 0.15,
            "error_count": 1500,
            "total_requests": 10000,
            "p99_duration_ms": 4500.0,
            "unique_users": 320,
            "error_message": "Payment gateway timeout",
        },
    ]

    return {
        "log_analyzer": LogAnalyzerOutput(
            error_logs=mock_logs,
            sql=sql,
        ),
    }


def rag_node(state: AgentState) -> dict:
    """RAG Agent：生成检索 query → 搜索知识库"""
    log_analyzer = state.get("log_analyzer") or {}
    error_logs = log_analyzer.get("error_logs", [])

    error_codes = [log["error_code"] for log in error_logs if log.get("error_code")]
    symptoms = error_logs[0].get("error_message", "") if error_logs else ""

    system = "根据错误信息生成 2-3 句检索 query，用于搜索技术文档库。只输出查询文本。"
    user_content = f"错误码: {error_codes}\n症状: {symptoms}"

    rag_query = call_claude(system, user_content, max_tokens=256)

    # 模拟 S3 Vectors 检索结果（实际项目中调用 s3_vectors_tool.vector_search）
    mock_docs = [
        {
            "doc_id": "INC-2026-04-01-payment-E2001",
            "title": "payment-service E2001 历史故障",
            "content": "2026-04-01 payment-service 出现 E2001 错误，根因是数据库连接池耗尽。",
            "doc_type": "incident_solution",
            "relevance_score": 0.92,
        },
    ]

    return {
        "rag": RAGOutput(
            retrieved_docs=mock_docs,
            query=rag_query,
        ),
    }


def reply_node(state: AgentState) -> dict:
    """Reply Agent：生成用户友好回复（不含技术细节）"""
    log_analyzer = state.get("log_analyzer") or {}
    error_logs = log_analyzer.get("error_logs", [])
    raw_input = state.get("raw_user_input", "")

    system = """你是客服助手。生成 3-5 句友好回复。
规则：包含致歉、当前状态、预计解决时间。不要暴露错误码、SQL、堆栈信息。"""

    user_content = f"用户投诉: {raw_input}\n错误记录数: {len(error_logs)}"
    reply = call_claude(system, user_content, max_tokens=512)

    return {
        "synthesizer": SynthesizerOutput(
            user_reply=reply,
            bug_report=None,    # reply_node 只写 user_reply
        ),
    }


def bug_report_node(state: AgentState) -> dict:
    """Bug Report Agent：生成结构化 JSON 报告"""
    log_analyzer = state.get("log_analyzer") or {}
    rag = state.get("rag") or {}
    router = state.get("router") or {}
    error_logs = log_analyzer.get("error_logs", [])
    retrieved_docs = rag.get("retrieved_docs", [])

    top_error_rate = max((log["error_rate"] for log in error_logs), default=0.0)
    error_codes = [log["error_code"] for log in error_logs if log.get("error_code")]

    system = """生成 JSON Bug 报告。severity 规则：>20%=P0, >5%=P1, >1%=P2, 其余=P3。
必须包含: severity, root_cause, recommended_fix, confidence_score(0-1)。只输出 JSON。"""

    user_content = f"""
错误率: {top_error_rate:.1%}
错误码: {error_codes}
错误日志: {json.dumps(error_logs[:3], ensure_ascii=False)}
知识库: {json.dumps([d['title'] for d in retrieved_docs], ensure_ascii=False)}
"""
    response_text = call_claude(system, user_content)

    # 解析 + 系统字段注入（和当前项目逻辑一致）
    try:
        import re, uuid
        from datetime import datetime, timezone
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        bug_report = json.loads(json_match.group()) if json_match else {}
        # 系统注入 — 不信任 LLM 生成的这些字段
        bug_report["report_id"] = str(uuid.uuid4())
        bug_report["generated_at"] = datetime.now(timezone.utc).isoformat()
        bug_report["affected_user_id"] = router.get("user_id", "unknown")
        bug_report["error_codes"] = error_codes
        bug_report["error_rate_at_incident"] = top_error_rate
        bug_report["kb_references"] = [d["doc_id"] for d in retrieved_docs[:3]]
    except Exception as e:
        bug_report = {"severity": "P3", "root_cause": f"报告生成异常: {e}", "confidence_score": 0.1}

    return {
        "synthesizer": SynthesizerOutput(
            user_reply=None,    # bug_report_node 只写 bug_report
            bug_report=bug_report,
        ),
    }


# ═══════════════════════════════════════════════════════════════
# 4. 路由函数（和当前项目 graph_builder.py 一致）
# ═══════════════════════════════════════════════════════════════

MAX_CLARIFICATION = 3


def route_after_router(state: AgentState) -> str:
    router = state.get("router") or {}
    intent = router.get("intent", "unknown")
    user_id = router.get("user_id")
    iteration = state.get("iteration_count", 0)

    if iteration >= MAX_CLARIFICATION:
        return "log_analyzer"

    if intent == "tech_issue":
        return "log_analyzer" if user_id else END

    if intent == "inquiry":
        return "rag"

    if intent == "need_more_info":
        return END

    return "reply"   # refund / unknown


def route_after_rag(state: AgentState) -> list:
    router = state.get("router") or {}
    if router.get("intent") == "tech_issue":
        return ["reply", "bug_report"]   # 并行
    return ["reply"]


# ═══════════════════════════════════════════════════════════════
# 5. 构建图（和当前项目 graph_builder.py 结构完全一致）
# ═══════════════════════════════════════════════════════════════

def build_graph():
    builder = StateGraph(AgentState)

    # 注册节点
    builder.add_node("router", router_node)
    builder.add_node("log_analyzer", log_analyzer_node)
    builder.add_node("rag", rag_node)
    builder.add_node("reply", reply_node)
    builder.add_node("bug_report", bug_report_node)

    # 入口
    builder.set_entry_point("router")

    # 条件路由
    builder.add_conditional_edges("router", route_after_router, {
        "log_analyzer": "log_analyzer",
        "rag": "rag",
        "reply": "reply",
        END: END,
    })

    # 线性边
    builder.add_edge("log_analyzer", "rag")

    # Fan-out
    builder.add_conditional_edges("rag", route_after_rag, {
        "reply": "reply",
        "bug_report": "bug_report",
    })

    # 终止边
    builder.add_edge("reply", END)
    builder.add_edge("bug_report", END)

    return builder.compile()


# ═══════════════════════════════════════════════════════════════
# 6. 运行示例
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    graph = build_graph()

    initial_state = {
        "messages": [],
        "raw_user_input": "我昨晚11点支付一直失败，用户ID是u_12345",
        "iteration_count": 0,
        "router": None,
        "log_analyzer": None,
        "rag": None,
        "synthesizer": None,
    }

    print("=" * 60)
    print("LangGraph + Claude SDK 诊断示例")
    print("=" * 60)
    print(f"\n用户输入: {initial_state['raw_user_input']}\n")

    final_state = graph.invoke(initial_state)

    print("-" * 60)
    print("Router 输出:")
    print(f"  intent: {final_state['router']['intent']}")
    print(f"  user_id: {final_state['router']['user_id']}")

    print("\nLog Analyzer 输出:")
    print(f"  SQL: {final_state['log_analyzer']['sql']}")
    print(f"  错误记录数: {len(final_state['log_analyzer']['error_logs'])}")

    print("\nRAG 输出:")
    print(f"  检索 query: {final_state['rag']['query']}")
    print(f"  文档数: {len(final_state['rag']['retrieved_docs'])}")

    print("\n用户回复:")
    print(f"  {final_state['synthesizer']['user_reply']}")

    print("\nBug Report:")
    report = final_state['synthesizer']['bug_report']
    if report:
        print(f"  severity: {report.get('severity')}")
        print(f"  root_cause: {report.get('root_cause')}")
        print(f"  confidence: {report.get('confidence_score')}")
        print(f"  report_id: {report.get('report_id')}")
    print("=" * 60)
