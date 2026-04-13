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

from langchain_aws import ChatBedrock
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ..state import AgentState, ErrorLogEntry, LogAnalyzerOutput, get_user_id, get_incident_time_hint
from src.config import settings
from src.tools.athena_tool import execute_athena_query
from src.tools.dynamodb_tool import query_dq_reports

LOG_ANALYZER_SYSTEM_PROMPT = """
你是一个 AWS Athena SQL 专家，负责从用户的故障描述中生成精确的 SQL 查询。

可用的视图：
- `iodp_gold_{env}.v_error_log_enriched`
  字段：stat_hour, service_name, error_code, error_rate, error_count, total_requests,
        p99_duration_ms, unique_users, sample_trace_ids, user_id, req_path,
        req_method, http_status, error_message, stack_trace, trace_id, event_timestamp

查询规则：
1. 时间范围：根据 incident_time_hint 推算，默认查前后 2 小时
2. 按 error_rate DESC 排序，LIMIT {max_rows}
3. 如果 user_id 已知，WHERE user_id = '...'
4. 只返回 log_level IN ('ERROR', 'FATAL') 的记录
5. 不允许使用 DROP/DELETE/INSERT/UPDATE/CREATE 语句

只输出纯 SQL，不需要解释。
""".strip()


def _parse_time_hint(time_hint: str) -> tuple[str, str]:
    """将自然语言时间描述转换为 ISO 时间段"""
    now = datetime.now(timezone.utc)
    base_date = now - timedelta(days=1) if "昨" in time_hint else now

    hour_match = re.search(r"(\d{1,2})\s*[点时]", time_hint)
    if hour_match:
        hour = int(hour_match.group(1))
        if ("晚" in time_hint or "夜" in time_hint) and hour < 12:
            hour += 12
        elif "上午" in time_hint and hour > 12:
            hour -= 12
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
    使用 LLM 生成 SQL → 调用 Athena（截断到 max_rows）→ 查 DQ 报告
    """
    user_id   = get_user_id(state)
    time_hint = get_incident_time_hint(state) or "最近1小时"
    env       = state.get("environment", "prod")

    time_start, time_end = _parse_time_hint(time_hint)

    llm = ChatBedrock(
        model_id=settings.bedrock_model_id,
        region_name=settings.aws_region,
        model_kwargs={"max_tokens": 2048, "temperature": 0},
    )

    prompt_content = LOG_ANALYZER_SYSTEM_PROMPT.replace("{env}", env).replace(
        "{max_rows}", str(settings.athena_max_rows)
    )
    sql_response = llm.invoke([
        SystemMessage(content=prompt_content),
        HumanMessage(content=(
            f"用户ID: {user_id}\n"
            f"故障时间段: {time_start} ~ {time_end}\n"
            f"环境: {env}\n"
            f"请生成查询该用户在该时段的 API 错误日志的 SQL。"
        )),
    ])

    generated_sql = sql_response.content.strip()

    # ─── 执行 Athena 查询 ───
    query_result = execute_athena_query(
        sql=generated_sql,
        database=f"iodp_gold_{env}",
        output_bucket=settings.athena_result_bucket,
        workgroup=settings.athena_workgroup,
        max_rows=settings.athena_max_rows,       # v2: 结果截断
    )

    rows           = query_result.get("rows", [])
    rows_truncated = query_result.get("rows_truncated", False)

    # ─── 查询该时段 DQ 报告 ───
    dq_anomaly = query_dq_reports(
        table_name="bronze_app_logs",
        time_start=time_start,
        time_end=time_end,
        dynamodb_table=settings.dq_reports_table,
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
