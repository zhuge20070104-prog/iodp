# eval/runner.py
"""
Agent eval runner —— 对 golden set 逐条跑 agent 节点，收集最终输出

两种模式：
  offline（默认）：LLM 和 AWS 工具都被 patch；验证 graph 管道、JSON 解析、schema 校验
  online          ：LLM 调真实 Bedrock；Athena/S3 Vectors/DynamoDB 仍然 stub（fixture 提供）

design note
  - 不跑完整 StateGraph，而是按需直接调用节点函数，配合 fixture 注入 log_analyzer / rag
    输出。好处：避免 checkpointer / DynamoDB 依赖，跑得快
  - reply_agent / bug_report_agent 按 fan-out 语义先后调用，再用 merge_synthesizer 合并
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

logger = logging.getLogger("eval.runner")

EVAL_DIR     = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
FIXTURES_DIR = EVAL_DIR / "fixtures"
CASES_FILE   = EVAL_DIR / "golden" / "cases.yaml"

# Ensure src/ is importable when run as `python -m eval` from project root
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ════════════════════════════════════════════════════════════════════════
# Case dataclass
# ════════════════════════════════════════════════════════════════════════

@dataclass
class Case:
    id: str
    category: str
    user_input: str
    fixture: Optional[str]
    expected: Dict[str, Any]


@dataclass
class CaseResult:
    case_id: str
    category: str
    intent: Optional[str]
    user_reply: Optional[str]
    bug_report: Optional[Dict[str, Any]]
    latency_ms: int
    error: Optional[str] = None


# ════════════════════════════════════════════════════════════════════════
# YAML loader (minimal dep — fall back to PyYAML if installed, else raise)
# ════════════════════════════════════════════════════════════════════════

def load_cases(path: Path = CASES_FILE) -> List[Case]:
    import yaml  # type: ignore
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Case(**c) for c in data["cases"]]


def load_fixture(name: Optional[str]) -> Dict[str, Any]:
    if not name:
        return {"error_logs": [], "rag_docs": []}
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ════════════════════════════════════════════════════════════════════════
# Scripted LLM stubs (offline mode)
# ════════════════════════════════════════════════════════════════════════

def _router_stub_response(user_input: str, known_user_id: Optional[str]) -> str:
    """
    基于输入文本的启发式返回 router JSON。
    不追求 LLM 真实行为，仅保证 graph 路由路径能走通。
    """
    text = user_input.lower()

    # 提示注入 / SQL 注入
    if any(k in text for k in ["ignore previous", "忽略", "drop table", "'; --", "sudo"]):
        return json.dumps({
            "intent": "security_violation",
            "user_id": None,
            "incident_time_hint": None,
            "missing_info": [],
            "clarification_question": None,
        })

    # 尝试提取 user_id（usr_XXX 模式）
    m = re.search(r"usr_[a-zA-Z0-9]+", user_input)
    extracted_uid = m.group(0) if m else known_user_id

    # 退款
    if "退款" in user_input or "refund" in text:
        return json.dumps({
            "intent": "refund",
            "user_id": extracted_uid,
            "incident_time_hint": None,
            "missing_info": [],
            "clarification_question": None,
        })

    # 咨询
    if any(k in user_input for k in ["请问", "如何", "怎么", "支持哪些"]):
        return json.dumps({
            "intent": "inquiry",
            "user_id": extracted_uid,
            "incident_time_hint": None,
            "missing_info": [],
            "clarification_question": None,
        })

    # 故障：有 user_id → tech_issue，无 → need_more_info
    fault_kw = ["失败", "错误", "卡", "超时", "慢", "登录不上", "504", "401", "页面", "问题", "系统"]
    if any(k in user_input for k in fault_kw):
        if extracted_uid:
            m_time = re.search(r"(昨晚|昨天|今天|早上|上午|下午|晚上)[^，。,.]{0,10}", user_input)
            time_hint = m_time.group(0) if m_time else None
            return json.dumps({
                "intent": "tech_issue",
                "user_id": extracted_uid,
                "incident_time_hint": time_hint,
                "missing_info": [],
                "clarification_question": None,
            })
        return json.dumps({
            "intent": "need_more_info",
            "user_id": None,
            "incident_time_hint": None,
            "missing_info": ["user_id"],
            "clarification_question": "请提供您的账户ID以便排查。",
        })

    # 兜底
    return json.dumps({
        "intent": "need_more_info",
        "user_id": extracted_uid,
        "incident_time_hint": None,
        "missing_info": ["details"],
        "clarification_question": "请详细描述您遇到的问题。",
    })


def _bug_report_stub_response(error_logs: List[Dict[str, Any]]) -> str:
    """
    基于 fixture 的 error_logs 生成 bug_report JSON（仅 LLM 需要输出的字段）
    系统字段由 bug_report_agent_node 自行注入
    """
    if not error_logs:
        return json.dumps({
            "severity": "P3",
            "root_cause": "证据不足，需进一步排查。",
            "reproduction_steps": ["无法确定复现路径"],
            "recommended_fix": "建议联系 SRE 团队排查",
            "confidence_score": 0.2,
        })

    top = error_logs[0]
    err_rate = top.get("error_rate", 0.0)
    if err_rate > 0.20:
        severity = "P0"
    elif err_rate > 0.05:
        severity = "P1"
    elif err_rate > 0.01:
        severity = "P2"
    else:
        severity = "P3"

    codes = sorted({log.get("error_code", "") for log in error_logs if log.get("error_code")})
    services = sorted({log.get("service_name", "") for log in error_logs if log.get("service_name")})
    service_map = {
        "payment-service":  "支付",
        "auth-service":     "认证",
        "api-gateway":      "网关",
        "order-service":    "业务",
    }
    service_zh = "、".join(service_map.get(s, s) for s in services)

    root_cause = (
        f"{service_zh}服务出现错误码 {','.join(codes)}，"
        f"{top.get('error_message', '')}"
    )

    return json.dumps({
        "severity": severity,
        "root_cause": root_cause,
        "reproduction_steps": [
            "1. 用户触发对应操作",
            f"2. {services[0] if services else 'service'} 返回错误码 {codes[0] if codes else 'UNKNOWN'}",
        ],
        "recommended_fix": f"检查 {services[0] if services else '相关'} 服务的下游依赖状态，排查 {codes[0] if codes else '错误'} 成因。",
        "confidence_score": 0.75 if error_logs else 0.2,
    })


def _reply_stub_response(intent: str, error_logs: List[Dict[str, Any]]) -> str:
    if intent == "security_violation":
        return "抱歉，您的请求包含不允许的内容，我无法执行。如您有合法的咨询诉求请换一种描述方式。"
    if intent == "refund":
        return "已收到您的退款申请，我们会在 1-3 个工作日内处理并通过短信通知您。"
    if intent == "inquiry":
        return "我们的支付系统目前支持信用卡、支付宝、微信支付和银行转账。更多详情可以查看帮助中心。"
    if intent == "need_more_info":
        return "为了更好地帮到您，请提供您的账户ID和问题发生的大致时间。"
    if error_logs:
        svc = error_logs[0].get("service_name", "相关")
        code = error_logs[0].get("error_code", "")
        return f"非常抱歉给您带来不便，我们检测到 {svc} 服务当前存在 {code} 错误，技术团队已在处理。"
    return "我们正在排查您遇到的问题，感谢您的反馈。"


# ════════════════════════════════════════════════════════════════════════
# Mock LLM factory
# ════════════════════════════════════════════════════════════════════════

class _StubLLM:
    """Mimics ChatBedrock.invoke(): returns an object with .content."""
    def __init__(self, response_fn):
        self._response_fn = response_fn

    def invoke(self, messages):
        content = self._response_fn(messages)
        resp = MagicMock()
        resp.content = content
        return resp


def _make_router_llm(user_input: str, known_user_id: Optional[str]):
    return _StubLLM(lambda _msgs: _router_stub_response(user_input, known_user_id))


def _make_bug_report_llm(error_logs: List[Dict[str, Any]]):
    return _StubLLM(lambda _msgs: _bug_report_stub_response(error_logs))


def _make_reply_llm(intent: str, error_logs: List[Dict[str, Any]]):
    return _StubLLM(lambda _msgs: _reply_stub_response(intent, error_logs))


# ════════════════════════════════════════════════════════════════════════
# Tool / IO stubs
# ════════════════════════════════════════════════════════════════════════

def _noop(*_args, **_kwargs):
    return None


def _patch_aws_side_effects():
    """
    bug_report_agent._save_to_tickets_table 会写 DynamoDB；整段 patch 成 no-op。
    reply_agent 不写 AWS；log_analyzer / rag 在 eval 中不会被调用。
    """
    return patch("src.graph.nodes.bug_report_agent._save_to_tickets_table", _noop)


# ════════════════════════════════════════════════════════════════════════
# Core runner
# ════════════════════════════════════════════════════════════════════════

def _build_initial_state(case: Case) -> dict:
    from langchain_core.messages import HumanMessage
    return {
        "messages":        [HumanMessage(content=case.user_input)],
        "raw_user_input":  case.user_input,
        "iteration_count": 0,
        "router":          None,
        "log_analyzer":    None,
        "rag":             None,
        "synthesizer":     None,
        "thread_id":       f"eval-{case.id}",
        "environment":     "eval",
        "job_id":          f"eval-job-{case.id}",
    }


def _seed_fixture_state(state: dict, fixture: Dict[str, Any]) -> dict:
    state["log_analyzer"] = {
        "athena_query_sql": "-- fixture --",
        "error_logs":       fixture.get("error_logs", []),
        "dq_anomaly":       None,
        "rows_truncated":   False,
    }
    state["rag"] = {
        "rag_query":      "-- fixture --",
        "retrieved_docs": fixture.get("rag_docs", []),
    }
    return state


def _merge_synth(state: dict, partial: Dict[str, Any]) -> None:
    """Apply the merge_synthesizer reducer manually."""
    from src.graph.state import merge_synthesizer
    state["synthesizer"] = merge_synthesizer(state.get("synthesizer"), partial.get("synthesizer"))


def run_case(case: Case, mode: str = "offline") -> CaseResult:
    """
    Execute the agent pipeline for a single case.
    mode="offline": all LLMs stubbed; "online": LLMs use real Bedrock.
    """
    start = datetime.now(timezone.utc)
    try:
        from src.graph.nodes import router_agent as router_mod
        from src.graph.nodes import bug_report_agent as bug_mod
        from src.graph.nodes import reply_agent as reply_mod

        state   = _build_initial_state(case)
        fixture = load_fixture(case.fixture)

        # Router
        if mode == "offline":
            with patch.object(
                router_mod, "ChatBedrock",
                return_value=_make_router_llm(case.user_input, None),
            ):
                router_update = router_mod.router_agent_node(state)
        else:
            router_update = router_mod.router_agent_node(state)

        state.update({k: v for k, v in router_update.items() if k != "messages"})

        intent = (state.get("router") or {}).get("intent")

        # Branch routing mirror of graph_builder.route_after_router / route_after_rag
        needs_logs       = intent == "tech_issue"
        needs_reply      = intent in ("tech_issue", "inquiry", "refund", "security_violation")
        needs_bug_report = intent == "tech_issue"

        if needs_logs:
            _seed_fixture_state(state, fixture)

        error_logs = (state.get("log_analyzer") or {}).get("error_logs", [])

        aws_patch = _patch_aws_side_effects() if mode == "offline" else patch("builtins.dict", dict)
        # (online mode still patches AWS-side persistence to avoid writing during eval)
        if mode == "online":
            aws_patch = _patch_aws_side_effects()

        with aws_patch:
            if needs_bug_report:
                if mode == "offline":
                    with patch.object(
                        bug_mod, "ChatBedrock",
                        return_value=_make_bug_report_llm(error_logs),
                    ):
                        br_update = bug_mod.bug_report_agent_node(state)
                else:
                    br_update = bug_mod.bug_report_agent_node(state)
                _merge_synth(state, br_update)

            if needs_reply:
                if mode == "offline":
                    with patch.object(
                        reply_mod, "ChatBedrock",
                        return_value=_make_reply_llm(intent or "unknown", error_logs),
                    ):
                        rp_update = reply_mod.reply_agent_node(state)
                else:
                    rp_update = reply_mod.reply_agent_node(state)
                _merge_synth(state, rp_update)

        synth      = state.get("synthesizer") or {}
        user_reply = synth.get("user_reply")
        bug_report = synth.get("bug_report")

        latency_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

        return CaseResult(
            case_id=case.id,
            category=case.category,
            intent=intent,
            user_reply=user_reply,
            bug_report=dict(bug_report) if bug_report else None,
            latency_ms=latency_ms,
        )

    except Exception as e:
        logger.exception("Case %s failed: %s", case.id, e)
        return CaseResult(
            case_id=case.id,
            category=case.category,
            intent=None,
            user_reply=None,
            bug_report=None,
            latency_ms=int((datetime.now(timezone.utc) - start).total_seconds() * 1000),
            error=f"{type(e).__name__}: {e}",
        )


def run_all(mode: str = "offline") -> List[CaseResult]:
    cases   = load_cases()
    results = []
    for c in cases:
        logger.info("Running case %s (%s)", c.id, c.category)
        results.append(run_case(c, mode=mode))
    return results
