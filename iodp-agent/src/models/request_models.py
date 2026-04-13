# src/models/request_models.py
from typing import Optional
from pydantic import BaseModel, Field


class DiagnoseRequest(BaseModel):
    """POST /diagnose 请求体"""
    message:   str          = Field(..., min_length=1, max_length=2000,
                                    description="用户输入的投诉/问题描述")
    thread_id: Optional[str] = Field(None, description="多轮对话 ID，首轮为 None")
    user_token: str          = Field(..., description="前端 JWT Token（由 API GW Cognito Authorizer 验证）")


class DiagnoseResponse(BaseModel):
    """POST /diagnose 响应体"""
    thread_id:     str
    user_reply:    str                         # 给用户看的自然语言回复
    bug_report:    Optional[dict] = None       # 给研发的 JSON 报告（仅技术故障时有）
    needs_more_info: bool = False              # true 时前端显示追问 UI
    clarification_question: Optional[str] = None
    intent:        str = "unknown"
