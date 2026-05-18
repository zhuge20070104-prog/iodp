"""
Structured single-line JSON logging for CloudWatch Logs Insights.

All trace logs go through `log_event(phase, event, **fields)` and produce one
JSON object per line, e.g.:

    {"phase":"athena","event":"success","query_execution_id":"abc","rows":8,"elapsed_ms":1834}

Logs Insights consumption:
    fields @timestamp, @message
    | filter phase = "athena"
    | sort @timestamp desc
"""

import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger("iodp.trace")


def log_event(phase: str, event: str, **fields: Any) -> None:
    payload = {"phase": phase, "event": event, **fields}
    try:
        logger.info(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        logger.info("phase=%s event=%s fields=%r", phase, event, fields)


def compress_sql(sql: str) -> str:
    """Collapse whitespace so a multi-line SQL becomes one CloudWatch line."""
    return re.sub(r"\s+", " ", sql).strip()


class Timer:
    """`with Timer() as t: ...; t.elapsed_ms` — millisecond wall clock."""

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        self.elapsed_ms = 0
        return self

    def __exit__(self, *_: Any) -> None:
        self.elapsed_ms = int((time.perf_counter() - self._start) * 1000)
