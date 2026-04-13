# src/main.py
"""
FastAPI 应用入口 v2 — 异步 Job 模式

改进：
  原来：POST /diagnose 同步执行 graph.invoke()，API Gateway 29s 超时
  现在：
    POST /diagnose  → 立即返回 {job_id, status="queued"}（202 Accepted）
    GET  /diagnose/{job_id} → 轮询 Job 状态和结果

DynamoDB Jobs 表 Schema:
  Table: iodp-agent-jobs-{env}
  PK:  job_id      (String)
  GSI: thread_id   (String, for multi-turn lookup)
  Attributes:
    status         String   "queued" | "running" | "completed" | "failed"
    thread_id      String
    request_json   String   (serialized DiagnoseRequest)
    result_json    String   (serialized final state)
    error          String   (if failed)
    created_at     String   ISO timestamp
    completed_at   String   ISO timestamp
    TTL            Number   Unix timestamp (1 hour TTL)
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from fastapi import BackgroundTasks, FastAPI, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from src.config import settings
from src.graph.checkpointer import get_checkpointer
from src.graph.graph_builder import build_graph

logger = logging.getLogger(__name__)

app = FastAPI(
    title="IODP Agent API",
    version="2.0.0",
    description="Multi-agent intelligent diagnosis system (async job pattern)",
)

_dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)


def _jobs_table():
    return _dynamodb.Table(settings.agent_jobs_table)


# ════════════════════════════════════════════════════════════════════════
# Request / Response models
# ════════════════════════════════════════════════════════════════════════

class DiagnoseRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None   # 多轮对话必须传入相同 thread_id
    user_id: Optional[str] = None     # 可选预填充（如前端已知）


class JobResponse(BaseModel):
    job_id: str
    status: str                        # "queued" | "running" | "completed" | "failed"
    thread_id: str
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None


# ════════════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════════════

def _create_job_record(job_id: str, thread_id: str, request: DiagnoseRequest) -> None:
    """在 DynamoDB 中创建初始 Job 记录"""
    now = datetime.now(timezone.utc)
    ttl = int(now.timestamp()) + settings.async_job_ttl_seconds

    _jobs_table().put_item(Item={
        "job_id":       job_id,
        "thread_id":    thread_id,
        "status":       "queued",
        "request_json": request.model_dump_json(),
        "result_json":  "",
        "error":        "",
        "created_at":   now.isoformat(),
        "completed_at": "",
        "TTL":          ttl,
    })


def _update_job_status(
    job_id: str,
    status: str,
    result_json: str = "",
    error: str = "",
) -> None:
    """更新 DynamoDB Job 记录状态"""
    update_expr = "SET #s = :s"
    expr_names  = {"#s": "status"}
    expr_values = {":s": status}

    if result_json:
        update_expr += ", result_json = :r"
        expr_values[":r"] = result_json
    if error:
        update_expr += ", #e = :e"
        expr_names["#e"] = "error"
        expr_values[":e"] = error
    if status in ("completed", "failed"):
        update_expr += ", completed_at = :c"
        expr_values[":c"] = datetime.now(timezone.utc).isoformat()

    _jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def _get_job_record(job_id: str) -> Optional[dict]:
    """从 DynamoDB 读取 Job 记录"""
    response = _jobs_table().get_item(Key={"job_id": job_id})
    return response.get("Item")


# ════════════════════════════════════════════════════════════════════════
# 后台 LangGraph 执行任务
# ════════════════════════════════════════════════════════════════════════

async def run_graph_job(job_id: str, thread_id: str, request: DiagnoseRequest) -> None:
    """
    后台任务：执行 LangGraph 图，完成后将结果写入 DynamoDB。
    异常不向 FastAPI 抛出（BackgroundTasks 内部捕获）。
    """
    try:
        _update_job_status(job_id, "running")

        checkpointer = get_checkpointer()
        graph        = build_graph(checkpointer)

        initial_state = {
            "messages":        [HumanMessage(content=request.message)],
            "raw_user_input":  request.message,
            "iteration_count": 0,
            "router":          None,
            "log_analyzer":    None,
            "rag":             None,
            "synthesizer":     None,
            "thread_id":       thread_id,
            "environment":     settings.environment,
            "job_id":          job_id,
        }

        # 如果前端传入了 user_id，预填充到 router 子结构
        if request.user_id:
            initial_state["router"] = {
                "intent":                 None,
                "user_id":                request.user_id,
                "incident_time_hint":     None,
                "missing_info":           [],
                "clarification_question": None,
            }

        config = {"configurable": {"thread_id": thread_id}}
        final_state = await graph.ainvoke(initial_state, config=config)

        # 序列化最终状态（只取对前端有意义的字段）
        result = {
            "user_reply":  (final_state.get("synthesizer") or {}).get("user_reply"),
            "bug_report":  (final_state.get("synthesizer") or {}).get("bug_report"),
            "intent":      (final_state.get("router") or {}).get("intent"),
            "thread_id":   thread_id,
        }

        _update_job_status(
            job_id,
            status="completed",
            result_json=json.dumps(result, ensure_ascii=False),
        )
        logger.info("Job %s completed successfully", job_id)

    except Exception as e:
        logger.exception("Job %s failed: %s", job_id, e)
        _update_job_status(job_id, status="failed", error=str(e))


# ════════════════════════════════════════════════════════════════════════
# API 端点
# ════════════════════════════════════════════════════════════════════════

@app.post("/diagnose", response_model=JobResponse, status_code=202)
async def submit_diagnosis(
    request: DiagnoseRequest,
    background_tasks: BackgroundTasks,
) -> JobResponse:
    """
    提交异步诊断 Job。立即返回 job_id，客户端通过 GET /diagnose/{job_id} 轮询结果。
    """
    job_id    = str(uuid.uuid4())
    thread_id = request.thread_id or f"thread_{job_id}"
    now       = datetime.now(timezone.utc).isoformat()

    _create_job_record(job_id, thread_id, request)
    background_tasks.add_task(run_graph_job, job_id, thread_id, request)

    logger.info("Job %s queued for thread %s", job_id, thread_id)

    return JobResponse(
        job_id=job_id,
        status="queued",
        thread_id=thread_id,
        created_at=now,
    )


@app.get("/diagnose/{job_id}", response_model=JobResponse)
async def get_diagnosis_result(job_id: str) -> JobResponse:
    """
    轮询诊断 Job 状态和结果。
    建议客户端每 2-3 秒轮询一次，最多等待 120 秒。
    """
    item = _get_job_record(job_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found or expired")

    result = None
    if item.get("result_json"):
        try:
            result = json.loads(item["result_json"])
        except json.JSONDecodeError:
            result = {"raw": item["result_json"]}

    return JobResponse(
        job_id=job_id,
        status=item["status"],
        thread_id=item["thread_id"],
        result=result,
        error=item.get("error") or None,
        created_at=item["created_at"],
        completed_at=item.get("completed_at") or None,
    )


@app.get("/health")
async def health():
    """健康检查端点（API Gateway / ECS health check 使用）"""
    return {
        "status": "ok",
        "version": "2.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
