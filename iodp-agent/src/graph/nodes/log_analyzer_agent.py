# src/graph/nodes/log_analyzer_agent.py
"""
Log Analyzer Agent 节点 v2
改进：
  - 使用 get_user_id() / get_incident_time_hint() helper 读取 router 子结构
  - Athena 查询结果截断为 settings.athena_max_rows 行，防止 Token 超限
  - 输出写入 state["log_analyzer"] (LogAnalyzerOutput)
"""

import re
from datetime import datetime, timedelta, timezone

from langchain_core.messages import AIMessage

from ..state import AgentState, ErrorLogEntry, LogAnalyzerOutput, get_user_id, get_incident_time_hint
from src.config import settings
from src.log_utils import log_event
from src.tools.athena_tool import execute_athena_query
from src.tools.dynamodb_tool import query_dq_reports

# LLM SQL 生成已废弃（不稳定），改为 hardcode SQL 直接查 v_error_log_enriched view。
# 历史 prompt 见 git history if needed.


def _parse_time_hint(time_hint: str) -> tuple[str, str]:
    """
    将自然语言时间描述转换为 ISO 时间段

    支持：
      - 单时间点："昨晚10点" → start=昨天 09:00, end=昨天 12:59 (±1.5h 缓冲)
      - 时间区间："昨天 晚上 10点到11点" → start=昨天 22:00, end=昨天 23:59
    """
    now = datetime.now(timezone.utc)
    base_date = now - timedelta(days=1) if "昨" in time_hint else now

    is_pm = ("晚" in time_hint) or ("夜" in time_hint) or ("下午" in time_hint) or ("傍晚" in time_hint)
    is_am = ("早上" in time_hint) or ("上午" in time_hint) or ("凌晨" in time_hint)

    def _to_24h(h: int) -> int:
        if is_pm and h < 12:
            return h + 12
        if is_am and h > 12:
            return h - 12
        return h

    # 优先识别区间："10点到11点" / "10:00 到 11:00"
    range_match = re.search(
        r"(\d{1,2})\s*[点:時]\d{0,2}\s*(?:到|至|~|-)\s*(\d{1,2})\s*[点:時]\d{0,2}",
        time_hint,
    )
    if range_match:
        start_hour = _to_24h(int(range_match.group(1)))
        end_hour   = _to_24h(int(range_match.group(2)))
        time_start = base_date.replace(hour=max(0, start_hour), minute=0, second=0, microsecond=0)
        time_end   = base_date.replace(hour=min(23, end_hour), minute=59, second=59, microsecond=0)
    else:
        hour_match = re.search(r"(\d{1,2})\s*[点时]", time_hint)
        if hour_match:
            hour = _to_24h(int(hour_match.group(1)))
        else:
            hour = base_date.hour
        time_start = base_date.replace(hour=max(0, hour - 1), minute=0, second=0, microsecond=0)
        time_end   = base_date.replace(hour=min(23, hour + 2), minute=59, second=59, microsecond=0)

    return (
        time_start.strftime("%Y-%m-%d %H:%M:%S"),
        time_end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def log_analyzer_agent_node(state: AgentState) -> dict:
    """
    Log Analyzer Agent 节点函数
    构造 SQL → 调用 Athena Silver 层 parsed_logs（截断到 max_rows）→ 查 DQ 报告

    注：之前让 LLM 生成 SQL 查 v_error_log_enriched 视图，但该视图未建（CLAUDE.md
    提及但未在 athena/ddl 实现），导致 Athena 报错被 try/except 吞掉，bug_report
    永远 evidence_trace_ids=[]。改为 hardcode SQL 直接查 silver.parsed_logs（seed
    数据真正所在的表），稳定可靠。
    """
    user_id   = get_user_id(state)
    time_hint = get_incident_time_hint(state) or "最近1小时"
    env       = state.get("environment", "prod")

    time_start, time_end = _parse_time_hint(time_hint)
    log_event(
        "node", "enter", node="log_analyzer",
        user_id=user_id, time_hint=time_hint,
        time_start=time_start, time_end=time_end, env=env,
        thread_id=state.get("thread_id"), job_id=state.get("job_id"),
    )

    # 走架构契约里的 v_error_log_enriched view（Gold api_error_stats JOIN Silver parsed_logs）。
    # 之前让 LLM 生成 SQL 不稳定，改为 hardcode 直接 SELECT。view 定义在
    # iodp-bigdata/athena/views/v_error_log_enriched.sql。
    #
    # Anonymous 模式：user_id == "anonymous" 表示用户明说没有账户ID，跳过 user_id 过滤，
    # 改查该时段全平台错误聚合（仍按 time 窗口过滤）。bug_report 会在 root_cause
    # 标注"建议提供账户ID精确定位"。
    is_anonymous = (not user_id) or user_id == "anonymous"
    user_filter  = "" if is_anonymous else f"user_id = '{user_id}' AND"

    generated_sql = f"""
        SELECT
            CAST(stat_hour AS VARCHAR)        AS stat_hour,
            service_name,
            error_code,
            error_rate,
            error_count,
            total_requests,
            p99_duration_ms,
            unique_users,
            sample_trace_ids,
            user_id,
            req_path,
            req_method,
            http_status,
            error_message,
            stack_trace,
            trace_id,
            CAST(event_timestamp AS VARCHAR)  AS event_timestamp
        FROM iodp_gold_{env}.v_error_log_enriched
        WHERE {user_filter}
              event_timestamp BETWEEN TIMESTAMP '{time_start}' AND TIMESTAMP '{time_end}'
        ORDER BY error_rate DESC, event_timestamp DESC
        LIMIT {settings.athena_max_rows}
    """.strip()

    # ─── 执行 Athena 查询 ───
    try:
        query_result = execute_athena_query(
            sql=generated_sql,
            database=f"iodp_gold_{env}",
            output_bucket=settings.athena_result_bucket,
            workgroup=settings.athena_workgroup,
            max_rows=settings.athena_max_rows,
        )
    except Exception as e:
        log_event(
            "node", "athena_failed", node="log_analyzer",
            error=str(e),
        )
        query_result = {"rows": [], "rows_truncated": False, "error": str(e)}

    rows           = query_result.get("rows", [])
    rows_truncated = query_result.get("rows_truncated", False)

    # ─── 查询该时段 DQ 报告 ───
    log_event(
        "dynamodb", "start", purpose="dq_report",
        table=settings.dq_reports_table,
        bronze_table="bronze_app_logs",
        time_start=time_start, time_end=time_end,
    )
    dq_anomaly = query_dq_reports(
        table_name="bronze_app_logs",
        time_start=time_start,
        time_end=time_end,
        dynamodb_table=settings.dq_reports_table,
    )
    log_event(
        "dynamodb", "success", purpose="dq_report",
        table=settings.dq_reports_table,
        hit=dq_anomaly is not None,
        error_type=dq_anomaly.get("error_type") if dq_anomaly else None,
        failure_rate=dq_anomaly.get("failure_rate") if dq_anomaly else None,
    )

    # ─── 格式化 ErrorLogEntry 列表 ───
    error_logs: list[ErrorLogEntry] = []
    for row in rows:
        error_logs.append(ErrorLogEntry(
            stat_hour=row.get("stat_hour", ""),
            service_name=row.get("service_name", ""),
            error_code=row.get("error_code", ""),
            error_rate=float(row.get("error_rate") or 0),
            error_count=int(row.get("error_count") or 0),
            total_requests=int(row.get("total_requests") or 0),
            p99_duration_ms=float(row.get("p99_duration_ms") or 0),
            unique_users=int(row.get("unique_users") or 0),
            sample_trace_ids=row.get("sample_trace_ids") or [],
            error_message=row.get("error_message"),
            stack_trace=row.get("stack_trace"),
            trace_id=row.get("trace_id"),
            event_timestamp=row.get("event_timestamp", ""),
        ))

    truncation_note = f"（结果已截断至 {settings.athena_max_rows} 行）" if rows_truncated else ""
    dq_note = f" ⚠️ 该时段存在数据质量异常: {dq_anomaly['error_type']}" if dq_anomaly else ""

    log_event(
        "node", "exit", node="log_analyzer",
        error_logs=len(error_logs),
        rows_truncated=rows_truncated,
        dq_anomaly=dq_anomaly.get("error_type") if dq_anomaly else None,
    )

    return {
        "log_analyzer": LogAnalyzerOutput(
            athena_query_sql=generated_sql,
            error_logs=error_logs,
            dq_anomaly=dq_anomaly,
            rows_truncated=rows_truncated,
        ),
        "messages": [
            AIMessage(content=(
                f"[Log Analyzer] 在 {time_start}~{time_end} 时段查询到 "
                f"{len(error_logs)} 条错误记录{truncation_note}。{dq_note}"
            ))
        ],
    }
