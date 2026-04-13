# tests/unit/test_synthesizer_output.py
"""
Synthesizer (ReplyAgent + BugReportAgent) 单元测试
运行：pytest tests/unit/test_synthesizer_output.py -v
"""

import json
import pytest
from unittest.mock import MagicMock, patch


# ════════════════════════════════════════════════════════════════════════
# _validate_bug_report_schema
# ════════════════════════════════════════════════════════════════════════

class TestValidateBugReportSchema:

    def _valid_report_dict(self):
        return {
            "report_id":               "550e8400-e29b-41d4-a716-446655440000",
            "generated_at":            "2026-04-06T10:00:00+00:00",
            "severity":                "P1",
            "affected_service":        "payment-service",
            "affected_user_id":        "usr_123",
            "incident_time_range":     {"start": "2026-04-05T23:00:00Z", "end": "2026-04-06T01:00:00Z"},
            "root_cause":              "Database connection pool exhausted during peak traffic",
            "error_codes":             ["E2001", "E2002"],
            "evidence_trace_ids":      ["trace-001"],
            "error_rate_at_incident":  0.25,
            "reproduction_steps":      ["Trigger > 1000 concurrent payment requests"],
            "recommended_fix":         "Increase DB connection pool size from 50 to 200",
            "kb_references":           ["doc-001"],
            "confidence_score":        0.85,
        }

    def test_valid_report_passes(self):
        from src.graph.nodes.bug_report_agent import _validate_bug_report_schema
        report = _validate_bug_report_schema(self._valid_report_dict())
        assert report["severity"] == "P1"
        assert report["confidence_score"] == 0.85

    def test_missing_severity_raises(self):
        from src.graph.nodes.bug_report_agent import _validate_bug_report_schema
        d = self._valid_report_dict()
        del d["severity"]
        with pytest.raises(ValueError, match="severity"):
            _validate_bug_report_schema(d)

    def test_missing_root_cause_raises(self):
        from src.graph.nodes.bug_report_agent import _validate_bug_report_schema
        d = self._valid_report_dict()
        del d["root_cause"]
        with pytest.raises(ValueError, match="root_cause"):
            _validate_bug_report_schema(d)

    def test_invalid_severity_raises(self):
        from src.graph.nodes.bug_report_agent import _validate_bug_report_schema
        d = self._valid_report_dict()
        d["severity"] = "P99"
        with pytest.raises(ValueError, match="Invalid severity"):
            _validate_bug_report_schema(d)

    def test_confidence_out_of_range_raises(self):
        from src.graph.nodes.bug_report_agent import _validate_bug_report_schema
        d = self._valid_report_dict()
        d["confidence_score"] = 1.5
        with pytest.raises(ValueError, match="confidence_score out of range"):
            _validate_bug_report_schema(d)

    def test_confidence_zero_is_valid(self):
        from src.graph.nodes.bug_report_agent import _validate_bug_report_schema
        d = self._valid_report_dict()
        d["confidence_score"] = 0.0
        report = _validate_bug_report_schema(d)
        assert report["confidence_score"] == 0.0

    def test_p0_severity_is_valid(self):
        from src.graph.nodes.bug_report_agent import _validate_bug_report_schema
        d = self._valid_report_dict()
        d["severity"] = "P0"
        report = _validate_bug_report_schema(d)
        assert report["severity"] == "P0"


# ════════════════════════════════════════════════════════════════════════
# ReplyAgent
# ════════════════════════════════════════════════════════════════════════

class TestReplyAgentNode:

    @patch("src.graph.nodes.reply_agent.ChatBedrock")
    def test_returns_user_reply_in_synthesizer(
        self, mock_bedrock_cls, state_with_rag, mock_llm_response, mock_settings
    ):
        """Reply Agent 生成回复 → state.synthesizer.user_reply 被设置"""
        from src.graph.nodes.reply_agent import reply_agent_node

        mock_llm = MagicMock()
        mock_bedrock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = mock_llm_response(
            "非常抱歉给您带来了不便。我们已定位到支付系统的问题，正在紧急修复，预计1小时内恢复。"
        )

        result = reply_agent_node(state_with_rag)

        assert "synthesizer" in result
        assert result["synthesizer"]["user_reply"] is not None
        assert len(result["synthesizer"]["user_reply"]) > 10
        assert result["synthesizer"]["bug_report"] is None   # reply_agent 不生成 bug_report

    @patch("src.graph.nodes.reply_agent.ChatBedrock")
    def test_fallback_reply_when_llm_returns_empty(
        self, mock_bedrock_cls, state_with_rag, mock_llm_response, mock_settings
    ):
        """LLM 返回空字符串 → 使用兜底回复"""
        from src.graph.nodes.reply_agent import reply_agent_node

        mock_llm = MagicMock()
        mock_bedrock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = mock_llm_response("")

        result = reply_agent_node(state_with_rag)

        assert result["synthesizer"]["user_reply"] is not None
        assert len(result["synthesizer"]["user_reply"]) > 0

    @patch("src.graph.nodes.reply_agent.ChatBedrock")
    def test_includes_ai_message(
        self, mock_bedrock_cls, state_with_rag, mock_llm_response, mock_settings
    ):
        """Reply Agent 的输出应包含 AIMessage"""
        from src.graph.nodes.reply_agent import reply_agent_node
        from langchain_core.messages import AIMessage

        mock_llm = MagicMock()
        mock_bedrock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = mock_llm_response("很抱歉，我们正在处理。")

        result = reply_agent_node(state_with_rag)

        assert "messages" in result
        assert len(result["messages"]) == 1
        assert isinstance(result["messages"][0], AIMessage)


# ════════════════════════════════════════════════════════════════════════
# BugReportAgent
# ════════════════════════════════════════════════════════════════════════

class TestBugReportAgentNode:

    def _valid_bug_report_json(self):
        return json.dumps({
            "severity":           "P0",
            "affected_service":   "payment-service",
            "root_cause":         "DB connection pool exhausted during peak traffic",
            "recommended_fix":    "Increase pool size and add circuit breaker",
            "confidence_score":   0.85,
            "reproduction_steps": ["Send 1000+ concurrent requests"],
        })

    @patch("src.graph.nodes.bug_report_agent.ChatBedrock")
    def test_returns_bug_report_in_synthesizer(
        self, mock_bedrock_cls, state_with_rag, mock_llm_response, mock_settings
    ):
        """Bug Report Agent 生成报告 → state.synthesizer.bug_report 被设置"""
        from src.graph.nodes.bug_report_agent import bug_report_agent_node

        mock_llm = MagicMock()
        mock_bedrock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = mock_llm_response(self._valid_bug_report_json())

        result = bug_report_agent_node(state_with_rag)

        assert "synthesizer" in result
        assert result["synthesizer"]["bug_report"] is not None
        assert result["synthesizer"]["user_reply"] is None   # bug_report_agent 不生成 user_reply
        bug = result["synthesizer"]["bug_report"]
        assert bug["severity"] in ("P0", "P1", "P2", "P3")

    @patch("src.graph.nodes.bug_report_agent.ChatBedrock")
    def test_injects_system_fields(
        self, mock_bedrock_cls, state_with_rag, mock_llm_response, mock_settings
    ):
        """report_id、generated_at、affected_user_id 等系统字段由代码注入，不来自 LLM"""
        from src.graph.nodes.bug_report_agent import bug_report_agent_node

        mock_llm = MagicMock()
        mock_bedrock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = mock_llm_response(self._valid_bug_report_json())

        result = bug_report_agent_node(state_with_rag)

        bug = result["synthesizer"]["bug_report"]
        # These fields should be injected by the code, not the LLM
        assert bug["report_id"] is not None
        assert bug["generated_at"] is not None
        assert bug["affected_user_id"] == "usr_test_123"   # from state_with_router fixture
        assert bug["error_codes"] == ["E2001"]             # from error_logs fixture

    @patch("src.graph.nodes.bug_report_agent.ChatBedrock")
    def test_fallback_p3_report_on_json_error(
        self, mock_bedrock_cls, state_with_rag, mock_llm_response, mock_settings
    ):
        """LLM 返回非 JSON → 生成兜底 P3 报告，不抛异常"""
        from src.graph.nodes.bug_report_agent import bug_report_agent_node

        mock_llm = MagicMock()
        mock_bedrock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = mock_llm_response("这是一段无法解析的文字")

        result = bug_report_agent_node(state_with_rag)

        bug = result["synthesizer"]["bug_report"]
        assert bug is not None
        assert bug["severity"] == "P3"
        assert bug["confidence_score"] == 0.1
        assert "需人工介入" in bug["root_cause"]

    @patch("src.graph.nodes.bug_report_agent.ChatBedrock")
    def test_high_error_rate_maps_to_p0(
        self, mock_bedrock_cls, state_with_rag, mock_llm_response, mock_settings
    ):
        """错误率 25% → Bug Report severity 应为 P0"""
        from src.graph.nodes.bug_report_agent import bug_report_agent_node

        mock_llm = MagicMock()
        mock_bedrock_cls.return_value = mock_llm
        # LLM 正确判断 P0
        mock_llm.invoke.return_value = mock_llm_response(json.dumps({
            "severity":           "P0",
            "affected_service":   "payment-service",
            "root_cause":         "Critical database failure causing 25% error rate",
            "recommended_fix":    "Immediate database failover required",
            "confidence_score":   0.9,
            "reproduction_steps": [],
        }))

        result = bug_report_agent_node(state_with_rag)
        assert result["synthesizer"]["bug_report"]["severity"] == "P0"


# ════════════════════════════════════════════════════════════════════════
# merge_synthesizer reducer
# ════════════════════════════════════════════════════════════════════════

class TestMergeSynthesizerReducer:

    def test_merge_reply_and_bug_report(self):
        """两个并行节点各自设置一半字段 → 合并后两个字段都有值"""
        from src.graph.state import merge_synthesizer, SynthesizerOutput

        reply_output      = SynthesizerOutput(user_reply="很抱歉给您带来不便。", bug_report=None)
        bug_report_output = SynthesizerOutput(user_reply=None, bug_report={"report_id": "r1", "severity": "P1"})

        merged = merge_synthesizer(reply_output, bug_report_output)

        assert merged["user_reply"] == "很抱歉给您带来不便。"
        assert merged["bug_report"]["report_id"] == "r1"

    def test_merge_with_none_a(self):
        """a 为 None → 返回 b"""
        from src.graph.state import merge_synthesizer, SynthesizerOutput

        b = SynthesizerOutput(user_reply="reply", bug_report=None)
        assert merge_synthesizer(None, b) == b

    def test_merge_with_none_b(self):
        """b 为 None → 返回 a"""
        from src.graph.state import merge_synthesizer, SynthesizerOutput

        a = SynthesizerOutput(user_reply="reply", bug_report=None)
        assert merge_synthesizer(a, None) == a

    def test_merge_first_non_none_wins(self):
        """两边都有同一字段 → 取 a 的值（先执行的节点优先）"""
        from src.graph.state import merge_synthesizer, SynthesizerOutput

        a = SynthesizerOutput(user_reply="reply from a", bug_report=None)
        b = SynthesizerOutput(user_reply="reply from b", bug_report=None)

        merged = merge_synthesizer(a, b)
        assert merged["user_reply"] == "reply from a"
