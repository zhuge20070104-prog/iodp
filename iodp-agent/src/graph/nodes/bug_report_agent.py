# src/graph/nodes/bug_report_agent.py
"""
Bug Report Agent 节点（从原 Synthesizer 拆分）
职责：ONLY 生成结构化 JSON Bug 报告（给研发团队）
  - 严格遵循 BugReport Schema
  - 根据证据推断根因，标注置信度
  - 与 ReplyAgent 并行执行（LangGraph fan-out）
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone

import boto3
from langchain_aws import ChatBedrock
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ..state import (
    AgentState, BugReport, SynthesizerOutput,
    get_error_logs, get_retrieved_docs, get_user_id, get_incident_time_hint,
)
from src.config import settings

logger = logging.getLogger(__name__)

BUG_REPORT_SYSTEM_PROMPT = """
你是一个企业级 SRE 智能助手，专门负责生成技术 Bug 报告。

【Bug 报告规则】
- 严格输出合法 JSON，遵循指定 Schema
- severity 根据 error_rate 判断：>20% = P0, >5% = P1, >1% = P2, 其余 = P3
- root_cause 必须基于日志证据推断，不能凭空捏造
- confidence_score 在 0.0~1.0 之间，根据证据充分性评估
- 证据不足时：root_cause 写 "证据不足，需进一步排查"，confidence_score <= 0.3
- reproduction_steps 列举复现步骤（如无法确定，写 "需进一步复现"）
- recommended_fix 基于知识库文档和日志证据给出修复建议；无把握时写 "建议联系 SRE 团队排查"

【安全规则】
- 不要在 root_cause 中包含用户个人信息（姓名、手机号、身份证等）

输出格式：纯 JSON，不要任何前缀或解释。
""".strip()


_BUG_TICKETS_TTL_DAYS = 180


def _save_to_tickets_table(report: BugReport) -> None:
    """将 Bug 报告持久化到 iodp-bug-tickets 表，供运维 Dashboard 和工单系统消费"""
    try:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(settings.bug_tickets_table)
        ttl = int(datetime.now(timezone.utc).timestamp()) + _BUG_TICKETS_TTL_DAYS * 86400

        item = dict(report)
        # DynamoDB 不支持嵌套 dict 直接存，序列化复杂字段
        item["incident_time_range"] = json.dumps(item.get("incident_time_range", {}))
        item["error_codes"] = item.get("error_codes", [])
        item["evidence_trace_ids"] = item.get("evidence_trace_ids", [])
        item["reproduction_steps"] = item.get("reproduction_steps", [])
        item["kb_references"] = item.get("kb_references", [])
        # float → Decimal 兼容
        item["error_rate_at_incident"] = str(item.get("error_rate_at_incident", 0))
        item["confidence_score"] = str(item.get("confidence_score", 0))
        item["TTL"] = ttl

        table.put_item(Item=item)
        logger.info("Bug report %s saved to tickets table", report["report_id"])
    except Exception as e:
        logger.error("Failed to save bug report to tickets table: %s", e)


def _validate_bug_report_schema(data: dict) -> BugReport:
    """对 LLM 生成的 Bug 报告进行 Schema 校验"""
    required_fields = [
        "report_id", "generated_at", "severity", "affected_service",
        "affected_user_id", "root_cause", "error_codes",
        "recommended_fix", "confidence_score",
    ]
    for f in required_fields:
        if f not in data:
            raise ValueError(f"Bug report missing required field: {f}")

    if data["severity"] not in ("P0", "P1", "P2", "P3"):
        raise ValueError(f"Invalid severity: {data['severity']}")

    confidence = float(data["confidence_score"])
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"confidence_score out of range: {confidence}")

    return BugReport(**{k: data.get(k) for k in BugReport.__annotations__})


def bug_report_agent_node(state: AgentState) -> dict:
    """
    Bug Report Agent 节点：生成结构化 Bug 报告
    """
    error_logs     = get_error_logs(state)
    retrieved_docs = get_retrieved_docs(state)
    user_id        = get_user_id(state) or "unknown"
    time_hint      = get_incident_time_hint(state) or "近期"
    raw_input      = state.get("raw_user_input", "")

    top_error_rate  = max((log["error_rate"] for log in error_logs), default=0.0)
    top_error_codes = list({log["error_code"] for log in error_logs if log.get("error_code")})
    top_services    = list({log["service_name"] for log in error_logs if log.get("service_name")})
    trace_samples   = error_logs[0].get("sample_trace_ids", []) if error_logs else []

    error_summary = json.dumps(error_logs[:5], ensure_ascii=False, indent=2) if error_logs else "无错误日志"
    doc_summary   = "\n\n".join([
        f"[{d['doc_type']}] {d['title']}\n{d['content'][:500]}"
        for d in retrieved_docs[:3]
    ]) if retrieved_docs else "无相关文档"

    context = f"""
用户原始投诉：{raw_input}
用户ID：{user_id}
故障时间：{time_hint}

错误日志摘要（最多5条）：
{error_summary}

知识库文档摘要：
{doc_summary}

关键指标：
- 最高错误率：{top_error_rate:.1%}
- 涉及错误码：{top_error_codes}
- 受影响服务：{top_services}
- TraceID 样本：{trace_samples}
""".strip()

    llm = ChatBedrock(
        model_id=settings.bedrock_model_id,
        region_name=settings.aws_region,
        model_kwargs={"max_tokens": 2048, "temperature": 0},
    )

    response = llm.invoke([
        SystemMessage(content=BUG_REPORT_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])

    validated_bug_report = None
    try:
        json_match = re.search(r"\{.*\}", response.content, re.DOTALL)
        if json_match:
            bug_report_dict = json.loads(json_match.group())
            # 注入系统字段（防止 LLM 捏造）
            bug_report_dict["report_id"]              = str(uuid.uuid4())
            bug_report_dict["generated_at"]           = datetime.now(timezone.utc).isoformat()
            bug_report_dict["affected_service"]       = top_services[0] if top_services else "unknown"
            bug_report_dict["affected_user_id"]       = user_id
            bug_report_dict["error_codes"]            = top_error_codes
            bug_report_dict["evidence_trace_ids"]     = trace_samples
            bug_report_dict["error_rate_at_incident"] = top_error_rate
            bug_report_dict["incident_time_range"]    = {"start": time_hint, "end": time_hint}
            bug_report_dict["kb_references"]          = [d["doc_id"] for d in retrieved_docs[:3]]
            validated_bug_report = _validate_bug_report_schema(bug_report_dict)
        else:
            raise ValueError("LLM response contained no JSON object")
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        # Schema 校验失败或无 JSON：生成低置信度兜底报告，不中断流程
        validated_bug_report = BugReport(
            report_id=str(uuid.uuid4()),
            generated_at=datetime.now(timezone.utc).isoformat(),
            severity="P3",
            affected_service=top_services[0] if top_services else "unknown",
            affected_user_id=user_id,
            incident_time_range={"start": time_hint, "end": time_hint},
            root_cause=f"报告生成异常({e})，需人工介入",
            error_codes=top_error_codes,
            evidence_trace_ids=trace_samples,
            error_rate_at_incident=top_error_rate,
            reproduction_steps=[],
            recommended_fix="请联系 SRE 团队手动排查",
            kb_references=[],
            confidence_score=0.1,
        )

    report_id = validated_bug_report["report_id"] if validated_bug_report else "N/A"

    # 持久化到 tickets 表（180 天 TTL），失败不中断流程
    if validated_bug_report:
        _save_to_tickets_table(validated_bug_report)

    return {
        # merge_synthesizer reducer 会将此与 ReplyAgent 的输出合并
        "synthesizer": SynthesizerOutput(
            user_reply=None,
            bug_report=validated_bug_report,
        ),
        "messages": [
            AIMessage(content=f"[BugReport] {report_id} generated, severity={validated_bug_report['severity'] if validated_bug_report else 'N/A'}")
        ],
    }
