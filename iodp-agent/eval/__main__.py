# eval/__main__.py
"""
CLI entry point. Usage:
  python -m eval                       # offline mode, no LLM judge
  python -m eval --mode online         # real Bedrock for agent nodes
  python -m eval --llm-judge           # enable Bedrock as evidence judge
"""

from __future__ import annotations

import argparse
import logging
import sys

from eval.runner import run_all
from eval.judge import judge_case
from eval.report import write_report

logger = logging.getLogger("eval")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="eval")
    p.add_argument("--mode", choices=["offline", "online"], default="offline",
                   help="offline: stub LLMs; online: real Bedrock (requires AWS creds)")
    p.add_argument("--llm-judge", action="store_true",
                   help="Enable Bedrock LLM-as-judge for evidence relevance")
    p.add_argument("--fail-below", type=float, default=0.0,
                   help="Exit with non-zero if overall pass-rate below this (0-1). 0 = never fail")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    logger.info("Running in mode=%s, llm_judge=%s", args.mode, args.llm_judge)
    results = run_all(mode=args.mode)
    # load cases again for judge (runner already loaded; avoid double-load via a shared list)
    from eval.runner import load_cases
    cases   = load_cases()
    by_id   = {c.id: c for c in cases}

    judges = []
    for r in results:
        case = by_id[r.case_id]
        j = judge_case(case, r, llm_judge_enabled=args.llm_judge)
        judges.append(j)

    report_path = write_report(results, judges, mode=args.mode, llm_judge_enabled=args.llm_judge)
    passed = sum(1 for j in judges if j.overall_passed)
    total = len(judges)
    pass_rate = passed / total if total else 0.0

    logger.info("Report written: %s", report_path)
    logger.info("Result: %d/%d passed (%.0f%%)", passed, total, pass_rate * 100)

    if args.fail_below > 0 and pass_rate < args.fail_below:
        logger.error("Pass rate %.2f below threshold %.2f", pass_rate, args.fail_below)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
