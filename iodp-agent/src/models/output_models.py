# src/models/output_models.py
"""
Bug 报告和诊断结果的 Pydantic 输出模型。
在 Synthesizer Agent 中用于 Schema 校验，防止 LLM 输出缺字段或类型错误。
"""

from typing import Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


class BugReportOutput(BaseModel):
    """研发侧结构化故障报告，写入工单系统"""
    report_id:              str
    generated_at:           str
    severity:               str   = Field(..., pattern=r"^(P0|P1|P2|P3)$")
    affected_service:       str
    affected_user_id:       str
    incident_time_range:    Dict[str, str]      # {"start": "...", "end": "..."}
    root_cause:             str
    error_codes:            List[str]
    evidence_trace_ids:     List[str]           = Field(default_factory=list)
    error_rate_at_incident: float               = Field(ge=0.0, le=1.0)
    reproduction_steps:     List[str]           = Field(default_factory=list)
    recommended_fix:        str
    kb_references:          List[str]           = Field(default_factory=list)
    confidence_score:       float               = Field(ge=0.0, le=1.0)

    @field_validator("severity")
    @classmethod
    def severity_must_be_valid(cls, v: str) -> str:
        if v not in ("P0", "P1", "P2", "P3"):
            raise ValueError(f"Invalid severity: {v}")
        return v

    @field_validator("confidence_score")
    @classmethod
    def confidence_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence_score must be in [0, 1], got {v}")
        return round(v, 3)


class DiagnosisResult(BaseModel):
    """Agent 完整诊断结果，包含用户回复和内部报告"""
    thread_id:               str
    intent:                  str
    user_reply:              str
    bug_report:              Optional[BugReportOutput] = None
    needs_more_info:         bool = False
    clarification_question:  Optional[str] = None
    athena_query_sql:        Optional[str] = None   # 审计用：实际执行的 SQL
    retrieved_doc_ids:       List[str] = Field(default_factory=list)
