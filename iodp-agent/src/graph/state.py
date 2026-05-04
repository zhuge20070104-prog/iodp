# src/graph/state.py
"""
AgentState v2 — 重构为嵌套子 TypedDict
改进：
  - 原来所有节点输出字段都平铺在 AgentState 上（20+ 个字段），节点间隐式耦合
  - 现在按节点职责拆分为子 TypedDict，AgentState 持有对子结构的引用
  - 新增 merge_synthesizer reducer，支持 ReplyAgent + BugReportAgent 并行执行后合并
  - 新增兼容性 helper 函数，方便节点代码使用
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


# ════════════════════════════════════════════════════════════════════════
# 原子数据类型（保持不变）
# ════════════════════════════════════════════════════════════════════════

class ErrorLogEntry(TypedDict):
    """从项目一 Athena 查询返回的单条错误日志记录"""
    stat_hour:        str
    service_name:     str
    error_code:       str
    error_rate:       float
    error_count:      int
    total_requests:   int
    p99_duration_ms:  float
    unique_users:     int
    sample_trace_ids: List[str]
    error_message:    Optional[str]
    stack_trace:      Optional[str]
    trace_id:         Optional[str]
    event_timestamp:  str


class RAGDocument(TypedDict):
    """从 S3 Vectors 检索到的单条知识库文档"""
    doc_id:          str
    title:           str
    content:         str
    doc_type:        str       # "product_doc" | "incident_solution"
    relevance_score: float
    error_codes:     List[str]


class BugReport(TypedDict):
    """Synthesizer 输出的结构化 Bug 报告（严格 JSON 格式）"""
    report_id:               str
    generated_at:            str
    severity:                str         # "P0" | "P1" | "P2" | "P3"
    affected_service:        str
    affected_user_id:        str
    incident_time_range:     Dict[str, str]
    root_cause:              str
    error_codes:             List[str]
    evidence_trace_ids:      List[str]
    error_rate_at_incident:  float
    reproduction_steps:      List[str]
    recommended_fix:         str
    kb_references:           List[str]
    confidence_score:        float


# ════════════════════════════════════════════════════════════════════════
# 节点输出子结构
# ════════════════════════════════════════════════════════════════════════

class RouterOutput(TypedDict):
    """Router Agent 节点输出"""
    intent:                  Optional[str]   # "tech_issue" | "refund" | "inquiry" | "need_more_info" | "security_violation"
    user_id:                 Optional[str]
    incident_time_hint:      Optional[str]
    missing_info:            List[str]
    clarification_question:  Optional[str]


class LogAnalyzerOutput(TypedDict):
    """Log Analyzer Agent 节点输出"""
    athena_query_sql:  Optional[str]       # 实际执行的 SQL（用于审计）
    error_logs:        List[ErrorLogEntry]
    dq_anomaly:        Optional[Dict[str, Any]]
    rows_truncated:    bool                # 是否因 athena_max_rows 限制被截断


class RAGOutput(TypedDict):
    """RAG Agent 节点输出"""
    rag_query:      Optional[str]
    retrieved_docs: List[RAGDocument]


class SynthesizerOutput(TypedDict):
    """ReplyAgent + BugReportAgent 并行输出（通过 merge_synthesizer reducer 合并）"""
    user_reply:  Optional[str]
    bug_report:  Optional[BugReport]


def merge_synthesizer(
    a: Optional[SynthesizerOutput],
    b: Optional[SynthesizerOutput],
) -> Optional[SynthesizerOutput]:
    """
    LangGraph reducer：合并并行运行的 ReplyAgent 和 BugReportAgent 的输出。
    两者分别设置 user_reply 和 bug_report，reducer 将两者合并到同一个结构中。
    """
    if a is None:
        return b
    if b is None:
        return a
    return SynthesizerOutput(
        user_reply=a.get("user_reply") or b.get("user_reply"),
        bug_report=a.get("bug_report") or b.get("bug_report"),
    )


# ════════════════════════════════════════════════════════════════════════
# 主状态（v2）
# ════════════════════════════════════════════════════════════════════════

class AgentState(TypedDict):
    """
    LangGraph 全局状态 v2
    - messages: 对话历史，add_messages reducer 保证追加语义
    - router / log_analyzer / rag / synthesizer: 各节点输出子结构，减少平铺耦合
    - synthesizer 使用自定义 merge_synthesizer reducer 支持并行节点合并
    """
    # 对话历史
    messages: Annotated[List[BaseMessage], add_messages]

    # 用户原始输入（所有节点均可读）
    raw_user_input: str

    # 追问迭代计数（由 Router Agent 维护）
    iteration_count: int

    # ── 各节点输出（子结构）──
    router:       Optional[RouterOutput]
    log_analyzer: Optional[LogAnalyzerOutput]
    rag:          Optional[RAGOutput]
    synthesizer:  Annotated[Optional[SynthesizerOutput], merge_synthesizer]

    # 系统元数据
    thread_id:   str
    environment: str
    job_id:      Optional[str]   # 异步 API 模式下由 main.py 注入


# ════════════════════════════════════════════════════════════════════════
# 兼容性 helper 函数
# 节点代码通过这些函数访问子结构字段，而不直接操作嵌套 dict
# ════════════════════════════════════════════════════════════════════════

def get_user_id(state: AgentState) -> Optional[str]:
    r = state.get("router")
    return r.get("user_id") if r else None


def get_intent(state: AgentState) -> Optional[str]:
    r = state.get("router")
    return r.get("intent") if r else None


def get_incident_time_hint(state: AgentState) -> Optional[str]:
    r = state.get("router")
    return r.get("incident_time_hint") if r else None


def get_clarification_question(state: AgentState) -> Optional[str]:
    r = state.get("router")
    return r.get("clarification_question") if r else None


def get_error_logs(state: AgentState) -> List[ErrorLogEntry]:
    la = state.get("log_analyzer")
    return la.get("error_logs", []) if la else []


def get_dq_anomaly(state: AgentState) -> Optional[Dict[str, Any]]:
    la = state.get("log_analyzer")
    return la.get("dq_anomaly") if la else None


def get_retrieved_docs(state: AgentState) -> List[RAGDocument]:
    r = state.get("rag")
    return r.get("retrieved_docs", []) if r else []


def get_user_reply(state: AgentState) -> Optional[str]:
    s = state.get("synthesizer")
    return s.get("user_reply") if s else None


def get_bug_report(state: AgentState) -> Optional[BugReport]:
    s = state.get("synthesizer")
    return s.get("bug_report") if s else None
