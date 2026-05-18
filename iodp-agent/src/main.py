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

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from fastapi import FastAPI, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from src.config import settings
from src.graph.checkpointer import get_checkpointer
from src.graph.graph_builder import build_graph
from src.log_utils import log_event

# Lambda runtime pre-installs a handler on the root logger, so basicConfig is a
# no-op. We must explicitly raise the root level — otherwise every logger.info()
# in this project is filtered out and CloudWatch shows only START/END/REPORT.
logging.getLogger().setLevel(logging.INFO)
# Quiet down noisy AWS SDK loggers; keep our own loggers at INFO.
for noisy in ("botocore", "urllib3", "s3transfer", "boto3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="IODP Agent API",
    version="2.0.0",
    description="Multi-agent intelligent diagnosis system (async job pattern)",
)

_dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)

# Lambda 自调用 worker：POST 时通过 lambda.invoke(InvocationType='Event') 触发自己跑
# LangGraph，立即返回 202。AWS_LAMBDA_FUNCTION_NAME 在 Lambda runtime 由系统注入；
# 本地 uvicorn 跑时这个变量不存在，会走 asyncio.create_task fallback。
_SELF_FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
_lambda_client = boto3.client("lambda", region_name=settings.aws_region)


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
        log_event(
            "request", "start",
            job_id=job_id, thread_id=thread_id,
            user_id=request.user_id, message_chars=len(request.message),
        )

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

        # 序列化最终状态（只取对前端有意义的字段）。
        # user_reply 优先级：synthesizer 最终回复 > router 追问 > 兜底
        # 当 router 判定 tech_issue 但无 user_id 时直接 END（无 synthesizer），
        # 用 router 的 clarification_question 给用户追问 user_id；否则给个友好兜底。
        router_state      = final_state.get("router") or {}
        synthesizer_state = final_state.get("synthesizer") or {}
        intent            = router_state.get("intent")

        user_reply = (
            synthesizer_state.get("user_reply")
            or router_state.get("clarification_question")
            or ("已收到您的反馈，正在处理。" if intent != "tech_issue"
                else "为了帮您查询故障，请告诉我您的用户 ID。")
        )

        result = {
            "user_reply":  user_reply,
            "bug_report":  synthesizer_state.get("bug_report"),
            "intent":      intent,
            "thread_id":   thread_id,
        }

        _update_job_status(
            job_id,
            status="completed",
            result_json=json.dumps(result, ensure_ascii=False),
        )
        log_event(
            "request", "success",
            job_id=job_id, thread_id=thread_id, intent=intent,
            has_bug_report=bool(synthesizer_state.get("bug_report")),
            reply_chars=len(user_reply),
        )

    except Exception as e:
        log_event("request", "error", job_id=job_id, thread_id=thread_id, error=str(e))
        logger.exception("Job %s failed: %s", job_id, e)
        _update_job_status(job_id, status="failed", error=str(e))


# ════════════════════════════════════════════════════════════════════════
# API 端点
# ════════════════════════════════════════════════════════════════════════

@app.post("/diagnose", response_model=JobResponse, status_code=202)
async def submit_diagnosis(request: DiagnoseRequest) -> JobResponse:
    """
    提交异步诊断 Job。立即返回 job_id，客户端通过 GET /diagnose/{job_id} 轮询结果。

    在 Lambda 环境用 boto3.lambda.invoke(InvocationType='Event') 异步触发
    worker Lambda（同一函数，靠 event.source 路由），绕开 Mangum + BackgroundTasks
    会等所有 BG task 完成才返回 response 的坑（之前导致 33s Lambda → API Gateway
    29s 超时 → 客户端见 503）。
    本地 uvicorn 跑时退化为 asyncio.create_task，BG task 不会撞 ASGI 长进程。
    """
    job_id    = str(uuid.uuid4())
    thread_id = request.thread_id or f"thread_{job_id}"
    now       = datetime.now(timezone.utc).isoformat()

    _create_job_record(job_id, thread_id, request)

    if _SELF_FUNCTION_NAME:
        _lambda_client.invoke(
            FunctionName=_SELF_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps({
                "source":    "iodp-worker",
                "job_id":    job_id,
                "thread_id": thread_id,
                "request":   request.model_dump(),
            }),
        )
        logger.info("Job %s dispatched to worker via Lambda self-invoke", job_id)
    else:
        asyncio.create_task(run_graph_job(job_id, thread_id, request))
        logger.info("Job %s queued locally (asyncio task)", job_id)

    return JobResponse(
        job_id=job_id,
        status="queued",
        thread_id=thread_id,
        created_at=now,
    )


async def worker_handler(event: dict) -> dict:
    """
    Lambda 自调用 worker 入口。由 lambda_handler.py 在 event.source=='iodp-worker'
    时调用，直接跑 run_graph_job。这里独立于 FastAPI/Mangum，没有"等响应再返回"的
    冻结问题。
    """
    job_id    = event["job_id"]
    thread_id = event["thread_id"]
    request   = DiagnoseRequest(**event["request"])
    await run_graph_job(job_id, thread_id, request)
    return {"job_id": job_id, "status": "completed"}


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
