# lambda_handler.py
"""
Lambda 入口：用 Mangum 将 API Gateway HTTP API 事件适配到 FastAPI ASGI 应用。
Mangum 自动处理 API Gateway v2 (HTTP API) 的 event/response 格式转换。
"""

from mangum import Mangum
from src.main import app

handler = Mangum(app, lifespan="off")
