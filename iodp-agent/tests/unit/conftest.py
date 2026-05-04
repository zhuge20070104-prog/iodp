# tests/unit/conftest.py
"""Agent 单元测试公共 fixtures"""

import pytest
from unittest.mock import MagicMock, patch

from langchain_core.messages import HumanMessage


@pytest.fixture
def mock_settings(monkeypatch):
    """Mock settings with safe test defaults"""
    from src.config import Settings
    test_settings = Settings(
        aws_region="us-east-1",
        athena_result_bucket="test-bucket",
        agent_state_table="test-state-table",
        agent_jobs_table="test-jobs-table",
        dq_reports_table="test-dq-table",
        vector_bucket_name="iodp-rag-test",
        bedrock_model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
        environment="test",
        max_clarification_iterations=3,
        athena_max_rows=50,
        async_job_ttl_seconds=3600,
    )
    monkeypatch.setattr("src.config.settings", test_settings)
    # Also patch in each module that imports settings
    for module_path in [
        "src.graph.nodes.router_agent.settings",
        "src.graph.nodes.log_analyzer_agent.settings",
        "src.graph.nodes.reply_agent.settings",
        "src.graph.nodes.bug_report_agent.settings",
        "src.tools.athena_tool.settings",
    ]:
        try:
            monkeypatch.setattr(module_path, test_settings)
        except AttributeError:
            pass
    return test_settings


@pytest.fixture
def base_agent_state():
    """Minimal valid AgentState for testing"""
    return {
        "messages":        [HumanMessage(content="支付失败，错误E2001")],
        "raw_user_input":  "支付失败，错误E2001",
        "iteration_count": 0,
        "router":          None,
        "log_analyzer":    None,
        "rag":             None,
        "synthesizer":     None,
        "thread_id":       "thread-test-001",
        "environment":     "test",
        "job_id":          None,
    }


@pytest.fixture
def state_with_router(base_agent_state):
    """State with router output (tech_issue, user_id set)"""
    base_agent_state["router"] = {
        "intent":                 "tech_issue",
        "user_id":                "usr_test_123",
        "incident_time_hint":     "昨晚11点",
        "missing_info":           [],
        "clarification_question": None,
    }
    return base_agent_state


@pytest.fixture
def state_with_error_logs(state_with_router):
    """State with log_analyzer output"""
    state_with_router["log_analyzer"] = {
        "athena_query_sql": "SELECT * FROM v_error_log_enriched LIMIT 50",
        "error_logs": [
            {
                "stat_hour":        "2026-04-05 23:00:00",
                "service_name":     "payment-service",
                "error_code":       "E2001",
                "error_rate":       0.25,
                "error_count":      500,
                "total_requests":   2000,
                "p99_duration_ms":  3500.0,
                "unique_users":     50,
                "sample_trace_ids": ["trace-001", "trace-002"],
                "error_message":    "Payment processing failed",
                "stack_trace":      None,
                "trace_id":         "trace-001",
                "event_timestamp":  "2026-04-05T23:15:00Z",
            }
        ],
        "dq_anomaly":    None,
        "rows_truncated": False,
    }
    return state_with_router


@pytest.fixture
def state_with_rag(state_with_error_logs):
    """State with RAG output"""
    state_with_error_logs["rag"] = {
        "rag_query":      "E2001 payment processing failure high error rate",
        "retrieved_docs": [
            {
                "doc_id":          "doc-001",
                "title":           "E2001 Payment Gateway Timeout Resolution",
                "content":         "When E2001 occurs, check payment gateway connectivity...",
                "doc_type":        "incident_solution",
                "relevance_score": 0.92,
                "error_codes":     ["E2001"],
            }
        ],
    }
    return state_with_error_logs


@pytest.fixture
def mock_llm_response():
    """Factory for mock LLM responses"""
    def _make_response(content: str):
        mock_resp = MagicMock()
        mock_resp.content = content
        return mock_resp
    return _make_response
