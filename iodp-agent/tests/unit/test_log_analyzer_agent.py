# tests/unit/test_log_analyzer_agent.py
"""
Log Analyzer Agent 单元测试
运行：pytest tests/unit/test_log_analyzer_agent.py -v
"""

import pytest
from unittest.mock import MagicMock, patch


class TestLogAnalyzerAgentNode:

    def _make_mock_query_result(self, num_rows=5, truncated=False):
        """Helper: build mock Athena result"""
        rows = [
            {
                "stat_hour":        "2026-04-05 23:00:00",
                "service_name":     "payment-service",
                "error_code":       "E2001",
                "error_rate":       str(0.25),
                "error_count":      str(500),
                "total_requests":   str(2000),
                "p99_duration_ms":  str(3500.0),
                "unique_users":     str(50),
                "sample_trace_ids": [],
                "error_message":    "Payment failed",
                "stack_trace":      None,
                "trace_id":         "trace-001",
                "event_timestamp":  "2026-04-05T23:00:00Z",
            }
            for _ in range(num_rows)
        ]
        return {
            "rows": rows,
            "query_execution_id": "qe-test-001",
            "rows_truncated": truncated,
            "total_rows_before_truncation": num_rows + (10 if truncated else 0),
        }

    @patch("src.graph.nodes.log_analyzer_agent.query_dq_reports", return_value=None)
    @patch("src.graph.nodes.log_analyzer_agent.execute_athena_query")
    @patch("src.graph.nodes.log_analyzer_agent.ChatBedrock")
    def test_returns_error_logs_in_state(
        self, mock_bedrock_cls, mock_athena, mock_dq, state_with_router, mock_llm_response, mock_settings
    ):
        """正常路径：Athena 查询 → error_logs 写入 state.log_analyzer"""
        from src.graph.nodes.log_analyzer_agent import log_analyzer_agent_node

        mock_llm = MagicMock()
        mock_bedrock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = mock_llm_response("SELECT * FROM v_error_log_enriched LIMIT 50")

        mock_athena.return_value = self._make_mock_query_result(num_rows=3)

        result = log_analyzer_agent_node(state_with_router)

        assert "log_analyzer" in result
        assert len(result["log_analyzer"]["error_logs"]) == 3
        assert result["log_analyzer"]["error_logs"][0]["error_code"] == "E2001"
        assert result["log_analyzer"]["athena_query_sql"] is not None

    @patch("src.graph.nodes.log_analyzer_agent.query_dq_reports", return_value=None)
    @patch("src.graph.nodes.log_analyzer_agent.execute_athena_query")
    @patch("src.graph.nodes.log_analyzer_agent.ChatBedrock")
    def test_truncates_at_max_rows(
        self, mock_bedrock_cls, mock_athena, mock_dq, state_with_router, mock_llm_response, mock_settings
    ):
        """Athena 返回超限行数 → rows_truncated=True 被记录"""
        from src.graph.nodes.log_analyzer_agent import log_analyzer_agent_node

        mock_llm = MagicMock()
        mock_bedrock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = mock_llm_response("SELECT * FROM v_error_log_enriched LIMIT 50")

        # Athena tool 已截断，返回 50 行 + rows_truncated=True
        mock_athena.return_value = self._make_mock_query_result(num_rows=50, truncated=True)

        result = log_analyzer_agent_node(state_with_router)

        assert result["log_analyzer"]["rows_truncated"] is True
        assert len(result["log_analyzer"]["error_logs"]) == 50

    @patch("src.graph.nodes.log_analyzer_agent.query_dq_reports")
    @patch("src.graph.nodes.log_analyzer_agent.execute_athena_query")
    @patch("src.graph.nodes.log_analyzer_agent.ChatBedrock")
    def test_attaches_dq_anomaly_when_present(
        self, mock_bedrock_cls, mock_athena, mock_dq, state_with_router, mock_llm_response, mock_settings
    ):
        """DynamoDB 返回 DQ 异常 → 包含在 state.log_analyzer.dq_anomaly"""
        from src.graph.nodes.log_analyzer_agent import log_analyzer_agent_node

        dq_anomaly = {"error_type": "NULL_LOG_ID", "failure_rate": "0.08"}
        mock_dq.return_value = dq_anomaly

        mock_llm = MagicMock()
        mock_bedrock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = mock_llm_response("SELECT * FROM view LIMIT 50")
        mock_athena.return_value = self._make_mock_query_result(num_rows=1)

        result = log_analyzer_agent_node(state_with_router)

        assert result["log_analyzer"]["dq_anomaly"] == dq_anomaly

    @patch("src.graph.nodes.log_analyzer_agent.query_dq_reports", return_value=None)
    @patch("src.graph.nodes.log_analyzer_agent.execute_athena_query")
    @patch("src.graph.nodes.log_analyzer_agent.ChatBedrock")
    def test_handles_athena_timeout(
        self, mock_bedrock_cls, mock_athena, mock_dq, state_with_router, mock_llm_response, mock_settings
    ):
        """Athena TimeoutError 应该向上传播"""
        from src.graph.nodes.log_analyzer_agent import log_analyzer_agent_node

        mock_llm = MagicMock()
        mock_bedrock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = mock_llm_response("SELECT 1")
        mock_athena.side_effect = TimeoutError("Athena query timed out after 60s")

        with pytest.raises(TimeoutError, match="timed out"):
            log_analyzer_agent_node(state_with_router)


class TestParseTimeHint:

    def test_parse_yesterday_evening(self):
        """'昨晚11点' → 昨天的23时范围"""
        from src.graph.nodes.log_analyzer_agent import _parse_time_hint
        start, end = _parse_time_hint("昨晚11点")
        assert "22:00:00" in start   # hour - 1
        # end should be next day or same day +2h
        assert start < end

    def test_parse_yesterday_prefix(self):
        """'昨' 关键词 → base_date 是昨天"""
        from src.graph.nodes.log_analyzer_agent import _parse_time_hint
        from datetime import datetime, timezone, timedelta

        start, _ = _parse_time_hint("昨天下午3点")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        assert yesterday in start

    def test_parse_evening_hour_adds_12(self):
        """'晚上8点' → hour=20（+12）"""
        from src.graph.nodes.log_analyzer_agent import _parse_time_hint
        start, _ = _parse_time_hint("晚上8点")
        assert "19:00:00" in start   # 20-1=19

    def test_parse_no_hour_uses_current(self):
        """没有明确小时数 → 不崩溃，返回有效时间段"""
        from src.graph.nodes.log_analyzer_agent import _parse_time_hint
        start, end = _parse_time_hint("最近发生的故障")
        assert start < end
        assert ":" in start
