# src/graph/graph_builder.py
"""
LangGraph 图构建 v2

改进：
  - Synthesizer 拆分为并行的 ReplyAgent + BugReportAgent（fan-out）
  - route_after_router 使用 get_intent() / get_user_id() helper
  - max_clarification_iterations 从 settings 读取

图结构（v2）:

    START
      │
      ▼
  [router_agent]
      │
      ├── intent == "tech_issue"      → [log_analyzer_agent] → [rag_agent]
      │                                                             │
      │                                              ┌─────────────┤
      │                                              │             │
      │                                       [reply_agent]  [bug_report_agent]  ← 并行
      │                                              │             │
      │                                              └──────┬──────┘
      │                                                     │ (merge_synthesizer)
      │                                                    END
      │
      ├── intent == "inquiry"         → [rag_agent] → [reply_agent] → END
      ├── intent == "refund"          → [reply_agent] → END
      └── intent == "need_more_info"  → END（追问）
"""

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.base import BaseCheckpointSaver

from .state import AgentState, get_intent, get_user_id
from .nodes.router_agent import router_agent_node
from .nodes.log_analyzer_agent import log_analyzer_agent_node
from .nodes.rag_agent import rag_agent_node
from .nodes.reply_agent import reply_agent_node
from .nodes.bug_report_agent import bug_report_agent_node

from src.config import settings


def route_after_router(state: AgentState) -> str | list[str]:
    """
    Router Agent 之后的条件路由。
    返回下一个节点名称（或 END）。
    """
    intent          = get_intent(state) or "unknown"
    user_id         = get_user_id(state)
    iteration_count = state.get("iteration_count", 0)

    # 达到最大追问次数：强制推进（不再追问）
    if iteration_count >= settings.max_clarification_iterations:
        return "log_analyzer_agent"

    if intent == "tech_issue":
        if not user_id:
            # 信息不足，向用户追问
            return END
        return "log_analyzer_agent"

    elif intent == "need_more_info":
        return END

    elif intent == "inquiry":
        return "rag_agent"

    elif intent == "security_violation":
        # 安全违规：直接走 reply_agent 生成拒绝回复，不查任何数据
        return "reply_agent"

    else:
        # refund / unknown：直接回复，不需要日志查询
        return "reply_agent"


def route_after_rag(state: AgentState) -> list[str]:
    """
    RAG Agent 之后：fan-out 到并行的 reply_agent + bug_report_agent
    tech_issue 路径走两个节点；inquiry 只走 reply_agent
    """
    intent = get_intent(state) or "unknown"
    if intent == "tech_issue":
        return ["reply_agent", "bug_report_agent"]
    else:
        # inquiry 等路径不需要 bug report
        return ["reply_agent"]


def build_graph(checkpointer: BaseCheckpointSaver) -> StateGraph:
    """
    构建并编译 LangGraph 图
    checkpointer: DynamoDB Checkpointer 实例
    """
    builder = StateGraph(AgentState)

    # ─── 注册节点 ───
    builder.add_node("router_agent",       router_agent_node)
    builder.add_node("log_analyzer_agent", log_analyzer_agent_node)
    builder.add_node("rag_agent",          rag_agent_node)
    builder.add_node("reply_agent",        reply_agent_node)
    builder.add_node("bug_report_agent",   bug_report_agent_node)

    # ─── 入口 ───
    builder.set_entry_point("router_agent")

    # ─── 条件路由：Router → 下一节点 ───
    builder.add_conditional_edges(
        "router_agent",
        route_after_router,
        {
            "log_analyzer_agent": "log_analyzer_agent",
            "rag_agent":          "rag_agent",
            "reply_agent":        "reply_agent",
            END:                  END,
        },
    )

    # ─── 线性：Log Analyzer → RAG ───
    builder.add_edge("log_analyzer_agent", "rag_agent")

    # ─── Fan-out：RAG → [Reply + BugReport] 并行 ───
    builder.add_conditional_edges(
        "rag_agent",
        route_after_rag,
        {
            "reply_agent":      "reply_agent",
            "bug_report_agent": "bug_report_agent",
        },
    )

    # ─── 终止边 ───
    builder.add_edge("reply_agent",      END)
    builder.add_edge("bug_report_agent", END)

    return builder.compile(checkpointer=checkpointer)
