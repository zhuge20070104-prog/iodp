# eval/judge.py
"""
判官模块 —— 混合评分

rule_judge(case, result):
  - intent 匹配（硬性）
  - root_cause 关键字命中（硬性）
  - required_error_codes 子集关系（硬性）
  - severity 匹配（可选）
  - bug_report / user_reply 存在性（可选）
  - reproduction_steps 最小条数（可选）

llm_judge(case, result):
  - 仅当 case.expected.evidence_judge_prompt 存在时触发
  - 调用 Bedrock（或在 --no-llm-judge 模式下 skip）
  - 输出 {score: 0~1, reasoning: str}
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("eval.judge")


@dataclass
class RuleCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class JudgeResult:
    case_id: str
    rule_checks: List[RuleCheck]
    rule_passed: bool
    llm_score: Optional[float] = None
    llm_reasoning: Optional[str] = None
    llm_skipped_reason: Optional[str] = None

    @property
    def overall_passed(self) -> bool:
        # Rules are mandatory. LLM score (when present) must be >= 0.6.
        if not self.rule_passed:
            return False
        if self.llm_score is not None and self.llm_score < 0.6:
            return False
        return True


# ════════════════════════════════════════════════════════════════════════
# Rule judge
# ════════════════════════════════════════════════════════════════════════

def rule_judge(case, result) -> tuple[List[RuleCheck], bool]:
    """
    Return (checks, all_passed).
    case: eval.runner.Case
    result: eval.runner.CaseResult
    """
    checks: List[RuleCheck] = []
    exp = case.expected

    # Short-circuit: runner errored
    if result.error:
        checks.append(RuleCheck("runner_no_error", False, f"runner error: {result.error}"))
        return checks, False

    # 1. intent
    want_intent = exp.get("intent")
    if want_intent:
        ok = result.intent == want_intent
        checks.append(RuleCheck(
            "intent_match", ok,
            f"want={want_intent} got={result.intent}",
        ))

    # 2. must_have_bug_report
    must_br = exp.get("must_have_bug_report", False)
    if must_br:
        checks.append(RuleCheck(
            "has_bug_report",
            result.bug_report is not None,
            "bug_report present" if result.bug_report else "bug_report missing",
        ))

    # 3. must_have_user_reply (default True unless explicitly False)
    must_reply = exp.get("must_have_user_reply", True)
    # for need_more_info there is no user_reply generated, so default to case value
    if must_reply:
        ok = bool(result.user_reply and result.user_reply.strip())
        checks.append(RuleCheck(
            "has_user_reply", ok,
            f"len={len(result.user_reply or '')}",
        ))
    elif "must_have_user_reply" in exp and exp["must_have_user_reply"] is False:
        # only verify absence if explicitly set to false
        ok = not (result.user_reply and result.user_reply.strip())
        checks.append(RuleCheck(
            "no_user_reply_expected", ok,
            f"reply={(result.user_reply or '')[:40]}",
        ))

    # 4. Bug report fields (only if bug_report present and expected)
    br = result.bug_report or {}
    if must_br and br:
        # severity
        want_sev = exp.get("severity")
        if want_sev:
            ok = br.get("severity") == want_sev
            checks.append(RuleCheck(
                "severity_match", ok,
                f"want={want_sev} got={br.get('severity')}",
            ))

        # root_cause keywords (OR semantics)
        kws = exp.get("root_cause_keywords", [])
        if kws:
            rc = (br.get("root_cause") or "").lower()
            hit = any(k.lower() in rc for k in kws)
            checks.append(RuleCheck(
                "root_cause_keyword", hit,
                f"kws={kws} root_cause={br.get('root_cause', '')[:80]}",
            ))

        # required_error_codes subset
        needed_codes = exp.get("required_error_codes", [])
        if needed_codes is not None:
            got_codes = set(br.get("error_codes") or [])
            missing = [c for c in needed_codes if c not in got_codes]
            checks.append(RuleCheck(
                "error_codes_subset", not missing,
                f"missing={missing} got={sorted(got_codes)}",
            ))

        # reproduction_steps min count
        min_steps = exp.get("reproduction_min_steps", 0)
        if min_steps > 0:
            steps = br.get("reproduction_steps") or []
            checks.append(RuleCheck(
                "reproduction_min_steps", len(steps) >= min_steps,
                f"got={len(steps)} want>={min_steps}",
            ))

    all_passed = all(c.passed for c in checks)
    return checks, all_passed


# ════════════════════════════════════════════════════════════════════════
# LLM judge (evidence relevance)
# ════════════════════════════════════════════════════════════════════════

JUDGE_SYSTEM_PROMPT = """
你是一个严格的 AI 评估员。针对一个故障诊断 agent 的 bug 报告，评估其证据与根因解释
是否支撑问题描述所问的判断。

评分规则：
- 0.0~0.3：完全偏离 / 证据不相关 / 把误导表象当根因
- 0.4~0.6：部分相关但遗漏关键证据
- 0.7~0.9：识别正确，证据充分
- 1.0   ：完美，多因素均命中

严格输出 JSON：{"score": <float 0-1>, "reasoning": "<不超过 60 字>"}
""".strip()


def _invoke_bedrock_judge(prompt: str) -> str:
    """Call Bedrock ChatBedrock with the judge system prompt."""
    from langchain_aws import ChatBedrock
    from langchain_core.messages import HumanMessage, SystemMessage

    model_id = os.getenv("BEDROCK_JUDGE_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0")
    region   = os.getenv("AWS_REGION", "us-east-1")

    llm = ChatBedrock(
        model_id=model_id,
        region_name=region,
        model_kwargs={"max_tokens": 256, "temperature": 0},
    )
    resp = llm.invoke([
        SystemMessage(content=JUDGE_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ])
    return resp.content


def llm_judge(case, result, enabled: bool = False) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """
    Return (score, reasoning, skipped_reason).
    If judge_prompt not set on case.expected → returns (None, None, 'no-judge-prompt').
    If enabled=False → returns (None, None, 'disabled').
    """
    judge_prompt = case.expected.get("evidence_judge_prompt")
    if not judge_prompt:
        return None, None, "no-judge-prompt"
    if not enabled:
        return None, None, "disabled"
    if result.error or not result.bug_report:
        return None, None, "no-bug-report"

    br = result.bug_report
    payload = (
        f"问题：{judge_prompt}\n\n"
        f"用户输入：{case.user_input}\n\n"
        f"agent 生成的 bug 报告要点：\n"
        f"- root_cause: {br.get('root_cause')}\n"
        f"- severity: {br.get('severity')}\n"
        f"- error_codes: {br.get('error_codes')}\n"
        f"- confidence_score: {br.get('confidence_score')}\n"
        f"- recommended_fix: {br.get('recommended_fix')}\n"
    )

    try:
        raw = _invoke_bedrock_judge(payload)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None, None, f"judge returned non-JSON: {raw[:80]}"
        obj = json.loads(m.group(0))
        score = float(obj.get("score", 0))
        reasoning = obj.get("reasoning", "")
        return score, reasoning, None
    except Exception as e:
        logger.warning("LLM judge call failed for %s: %s", case.id, e)
        return None, None, f"error: {type(e).__name__}"


# ════════════════════════════════════════════════════════════════════════
# Combined
# ════════════════════════════════════════════════════════════════════════

def judge_case(case, result, llm_judge_enabled: bool = False) -> JudgeResult:
    checks, rule_passed = rule_judge(case, result)
    score, reasoning, skipped = llm_judge(case, result, enabled=llm_judge_enabled)
    return JudgeResult(
        case_id=case.id,
        rule_checks=checks,
        rule_passed=rule_passed,
        llm_score=score,
        llm_reasoning=reasoning,
        llm_skipped_reason=skipped,
    )
