# lambda_handler.py
"""
Lambda 入口。同一个函数承担两种调用方式：

1. API Gateway HTTP API → Mangum 适配 FastAPI → /diagnose、/diagnose/{id}、/health
2. Lambda 自调用（InvocationType='Event'）→ event.source=='iodp-worker' → 跑 LangGraph

第 2 条是为了绕开 BackgroundTasks 在 Mangum 下被等齐才返回 response 的坑：
POST handler 不再用 BackgroundTasks，而是异步 invoke 自己，立即返回 202。
"""

import asyncio
import logging

from mangum import Mangum

from src.main import app, worker_handler

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_api_handler = Mangum(app, lifespan="off")


def _ensure_event_loop():
    """
    Python 3.12 严格化后 asyncio.get_event_loop() 在主线程没设过 loop 时直接抛
    RuntimeError（旧版本会自动创建）。Mangum 0.17 的 protocols/http.py:46 仍在用
    这个老 API，会炸。
    每次 invocation 入口都检查一遍：没 loop 或 loop 已 closed 时建一个新的。
    用 loop.run_until_complete 取代 asyncio.run，避免关闭 loop 影响后续调用。
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("loop closed")
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def handler(event, context):
    loop = _ensure_event_loop()

    # 自调用 worker 路径：跑完 LangGraph 才返回，但调用方是 lambda.invoke(Event)
    # fire-and-forget 的，所以这里耗时多久都不影响 API Gateway。
    if isinstance(event, dict) and event.get("source") == "iodp-worker":
        logger.info("Worker invoked for job %s", event.get("job_id"))
        return loop.run_until_complete(worker_handler(event))

    # 默认走 API Gateway → Mangum → FastAPI
    return _api_handler(event, context)
