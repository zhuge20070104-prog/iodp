# eval/report.py
"""
产出 Markdown 报告 —— 记录每次 eval 的快照
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _check_cell(checks) -> str:
    if not checks:
        return "—"
    parts = []
    for c in checks:
        mark = "✓" if c.passed else "✗"
        parts.append(f"{mark}{c.name}")
    return "<br>".join(parts)


def render_markdown(results, judges, mode: str, llm_judge_enabled: bool) -> str:
    """
    results: List[CaseResult]
    judges:  List[JudgeResult] aligned with results
    """
    total = len(results)
    passed = sum(1 for j in judges if j.overall_passed)
    rule_only_passed = sum(1 for j in judges if j.rule_passed)
    llm_scored = [j.llm_score for j in judges if j.llm_score is not None]
    avg_llm = sum(llm_scored) / len(llm_scored) if llm_scored else None

    # Latency
    latencies = [r.latency_ms for r in results if r.error is None]
    avg_lat = int(sum(latencies) / len(latencies)) if latencies else 0
    p95_lat = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"# Agent Eval Report",
        "",
        f"- Generated: `{now}`",
        f"- Mode: `{mode}`",
        f"- LLM judge: `{'enabled' if llm_judge_enabled else 'disabled'}`",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Total cases | {total} |",
        f"| Overall passed | {passed} / {total} ({passed*100//total if total else 0}%) |",
        f"| Rule-only passed | {rule_only_passed} / {total} |",
        f"| Avg LLM judge score | {f'{avg_llm:.2f}' if avg_llm is not None else 'n/a (disabled)'} |",
        f"| Avg latency (rule) | {avg_lat} ms |",
        f"| p95 latency | {p95_lat} ms |",
        "",
        "## Per-case results",
        "",
        "| ID | Category | Intent | Rule checks | LLM score | Latency | Status |",
        "|---|---|---|---|---|---|---|",
    ]

    for r, j in zip(results, judges):
        intent = r.intent or "—"
        llm_cell = (
            f"{j.llm_score:.2f}"
            if j.llm_score is not None
            else (f"skip({j.llm_skipped_reason})" if j.llm_skipped_reason else "—")
        )
        status = "✅ PASS" if j.overall_passed else "❌ FAIL"
        lines.append(
            f"| {r.case_id} | {r.category} | {intent} | {_check_cell(j.rule_checks)} | {llm_cell} | {r.latency_ms}ms | {status} |"
        )

    # Failure details section
    failures = [(r, j) for r, j in zip(results, judges) if not j.overall_passed]
    if failures:
        lines.append("")
        lines.append("## Failure details")
        lines.append("")
        for r, j in failures:
            lines.append(f"### {r.case_id}")
            if r.error:
                lines.append(f"- Runner error: `{r.error}`")
            for c in j.rule_checks:
                if not c.passed:
                    lines.append(f"- **{c.name}**: {c.detail}")
            if j.llm_score is not None and j.llm_score < 0.6:
                lines.append(f"- LLM judge: score={j.llm_score:.2f}, reasoning={j.llm_reasoning}")
            if r.bug_report:
                rc = r.bug_report.get("root_cause", "")
                lines.append(f"- `root_cause`: {rc[:200]}")
            lines.append("")

    return "\n".join(lines) + "\n"


def write_report(results, judges, mode: str, llm_judge_enabled: bool) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    md = render_markdown(results, judges, mode, llm_judge_enabled)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot = REPORTS_DIR / f"eval_{mode}_{stamp}.md"
    latest   = REPORTS_DIR / "latest.md"
    snapshot.write_text(md, encoding="utf-8")
    latest.write_text(md, encoding="utf-8")
    return latest
