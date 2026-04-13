# tests/unit/test_data_quality.py
"""
DataQualityChecker 单元测试

全部使用 mock，不依赖真实 Spark / AWS 环境。
运行：pytest tests/unit/test_data_quality.py -v
"""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch, call
from botocore.exceptions import ClientError

# ─── 避免导入真实 PySpark ───
import sys
import types

# Mock pyspark modules so tests can run without a Spark cluster
pyspark_mock = types.ModuleType("pyspark")
pyspark_sql  = types.ModuleType("pyspark.sql")
pyspark_sql_functions = types.ModuleType("pyspark.sql.functions")

# Make col, lit, etc. return MagicMocks
for fn_name in [
    "col", "lit", "when", "date_format", "regexp_extract",
    "current_timestamp", "to_timestamp", "unix_timestamp",
    "abs", "rlike",
]:
    setattr(pyspark_sql_functions, fn_name, MagicMock(return_value=MagicMock()))

pyspark_mock.sql      = pyspark_sql
pyspark_sql.DataFrame = MagicMock
pyspark_sql.functions = pyspark_sql_functions
sys.modules.setdefault("pyspark",             pyspark_mock)
sys.modules.setdefault("pyspark.sql",         pyspark_sql)
sys.modules.setdefault("pyspark.sql.functions", pyspark_sql_functions)

# Also mock awsglue
awsglue_mock = types.ModuleType("awsglue")
sys.modules.setdefault("awsglue",             awsglue_mock)
sys.modules.setdefault("awsglue.context",     types.ModuleType("awsglue.context"))
sys.modules.setdefault("awsglue.job",         types.ModuleType("awsglue.job"))
sys.modules.setdefault("awsglue.utils",       types.ModuleType("awsglue.utils"))

from glue_jobs.lib.data_quality import (
    DataQualityChecker,
    DQResult,
    DQRule,
    rule_not_null,
    rule_in_set,
    rule_valid_uuid,
    rule_timestamp_in_range,
)


# ════════════════════════════════════════════════════════════════════════
# load_threshold()
# ════════════════════════════════════════════════════════════════════════

class TestLoadThreshold:

    def test_load_threshold_from_dynamodb(self):
        """DynamoDB 有配置 → 返回 DynamoDB 中的值"""
        mock_db = MagicMock()
        mock_table = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {"table_name": "bronze_app_logs", "failure_threshold": Decimal("0.03")}
        }

        threshold = DataQualityChecker.load_threshold(
            table_name="bronze_app_logs",
            config_dynamodb_table="iodp-dq-threshold-config-test",
            dynamodb_resource=mock_db,
            fallback=0.05,
        )

        assert threshold == pytest.approx(0.03)
        mock_db.Table.assert_called_once_with("iodp-dq-threshold-config-test")

    def test_load_threshold_fallback_on_missing_item(self):
        """DynamoDB 无此 table_name 记录 → 返回 fallback"""
        mock_db = MagicMock()
        mock_table = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_table.get_item.return_value = {}  # No "Item" key

        threshold = DataQualityChecker.load_threshold(
            table_name="unknown_table",
            config_dynamodb_table="iodp-dq-threshold-config-test",
            dynamodb_resource=mock_db,
            fallback=0.05,
        )

        assert threshold == pytest.approx(0.05)

    def test_load_threshold_fallback_on_client_error(self):
        """DynamoDB ClientError（如表不存在）→ 返回 fallback，不抛异常"""
        mock_db = MagicMock()
        mock_table = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
            "GetItem",
        )

        threshold = DataQualityChecker.load_threshold(
            table_name="bronze_app_logs",
            config_dynamodb_table="nonexistent-table",
            dynamodb_resource=mock_db,
            fallback=0.07,
        )

        assert threshold == pytest.approx(0.07)

    def test_load_threshold_fallback_on_unexpected_error(self):
        """任意未知异常 → 返回 fallback，不抛异常"""
        mock_db = MagicMock()
        mock_db.Table.side_effect = RuntimeError("Connection timeout")

        threshold = DataQualityChecker.load_threshold(
            table_name="bronze_app_logs",
            config_dynamodb_table="iodp-config",
            dynamodb_resource=mock_db,
            fallback=0.05,
        )

        assert threshold == pytest.approx(0.05)

    def test_load_threshold_item_missing_threshold_key(self):
        """DynamoDB Item 存在但没有 failure_threshold 字段 → 返回 fallback"""
        mock_db = MagicMock()
        mock_table = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {"table_name": "bronze_app_logs", "description": "no threshold field"}
        }

        threshold = DataQualityChecker.load_threshold(
            table_name="bronze_app_logs",
            config_dynamodb_table="iodp-config",
            dynamodb_resource=mock_db,
            fallback=0.05,
        )

        assert threshold == pytest.approx(0.05)


# ════════════════════════════════════════════════════════════════════════
# Rule factory functions
# ════════════════════════════════════════════════════════════════════════

class TestRuleFactories:

    def test_rule_not_null_returns_dq_rule(self):
        rule = rule_not_null("log_id")
        assert isinstance(rule, DQRule)
        assert rule.error_type == "NULL_LOG_ID"
        assert rule.name == "not_null_log_id"

    def test_rule_not_null_custom_name(self):
        rule = rule_not_null("user_id", rule_name="my_custom_rule")
        assert rule.name == "my_custom_rule"

    def test_rule_in_set_returns_dq_rule(self):
        rule = rule_in_set("log_level", {"ERROR", "WARN", "INFO"})
        assert isinstance(rule, DQRule)
        assert rule.error_type == "INVALID_LOG_LEVEL"

    def test_rule_in_set_with_none_allowed(self):
        """None 在 valid_values 中时，check_fn 生成 nullable OR 条件"""
        rule = rule_in_set("error_code", {"E1001", None})
        assert rule is not None
        # check_fn should be callable
        mock_df = MagicMock()
        mock_df.withColumn.return_value = mock_df
        rule.check_fn(mock_df)
        mock_df.withColumn.assert_called_once()

    def test_rule_valid_uuid_returns_dq_rule(self):
        rule = rule_valid_uuid("event_id")
        assert isinstance(rule, DQRule)
        assert "UUID" in rule.error_type.upper()
        assert rule.name == "valid_uuid_event_id"

    def test_rule_valid_uuid_nullable_default(self):
        """Default: nullable=True — NULL values should pass"""
        rule = rule_valid_uuid("trace_id")
        assert rule.name == "valid_uuid_trace_id"
        # Verify check_fn is callable
        mock_df = MagicMock()
        mock_df.withColumn.return_value = mock_df
        rule.check_fn(mock_df)

    def test_rule_valid_uuid_not_nullable(self):
        rule = rule_valid_uuid("event_id", nullable=False)
        assert "nullable" not in rule.description

    def test_rule_timestamp_in_range_returns_dq_rule(self):
        rule = rule_timestamp_in_range("event_timestamp", max_lag_hours=48)
        assert isinstance(rule, DQRule)
        assert rule.error_type == "TIMESTAMP_OUT_OF_RANGE"
        assert "48" in rule.description


# ════════════════════════════════════════════════════════════════════════
# DataQualityChecker._write_dq_report()
# ════════════════════════════════════════════════════════════════════════

class TestWriteDqReport:

    def _make_checker(self, mock_db) -> DataQualityChecker:
        with patch("boto3.resource", return_value=mock_db):
            checker = DataQualityChecker(
                table_name="bronze_app_logs",
                batch_id="test-batch",
                job_run_id="test-run",
                dead_letter_base_path="s3://bronze/dead_letter/",
                dq_table_name="iodp-dq-reports-test",
                failure_threshold=0.05,
                environment="test",
            )
        return checker

    def test_write_dq_report_calls_put_item(self):
        mock_db    = MagicMock()
        mock_table = MagicMock()
        mock_db.Table.return_value = mock_table

        checker = self._make_checker(mock_db)
        result  = DQResult(
            table_name="bronze_app_logs",
            batch_id="test-batch",
            job_run_id="test-run",
            total_records=1000,
            failed_records=20,
            failure_rate=0.02,
            error_type="NULL_LOG_ID",
            threshold_breached=False,
            environment="test",
        )

        checker._write_dq_report(result)

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["table_name"] == "bronze_app_logs"
        assert item["failed_records"] == 20
        assert item["threshold_breached"] is False
        assert "TTL" in item

    def test_write_dq_report_silences_exception(self):
        """DynamoDB 写入失败不应中断主流程"""
        mock_db    = MagicMock()
        mock_table = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_table.put_item.side_effect = Exception("DynamoDB unreachable")

        checker = self._make_checker(mock_db)
        result  = DQResult(
            table_name="bronze_app_logs",
            batch_id="b1",
            job_run_id="r1",
            total_records=100,
            failed_records=5,
            failure_rate=0.05,
            error_type="NULL_LOG_ID",
            threshold_breached=False,
        )

        # Should NOT raise even though DynamoDB fails
        checker._write_dq_report(result)


# ════════════════════════════════════════════════════════════════════════
# DataQualityChecker.run() — threshold breach logic
# ════════════════════════════════════════════════════════════════════════

class TestThresholdBreach:

    def _make_checker_with_mocks(self, failure_threshold=0.05):
        """Build a checker with all AWS mocked out"""
        mock_db = MagicMock()
        mock_table = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_table.get_item.return_value = {}
        mock_table.put_item.return_value = {}

        with patch("boto3.resource", return_value=mock_db):
            checker = DataQualityChecker(
                table_name="bronze_app_logs",
                batch_id="b1",
                job_run_id="r1",
                dead_letter_base_path="s3://bronze/dead_letter/",
                dq_table_name="dq-table",
                failure_threshold=failure_threshold,
                environment="test",
            )
        return checker, mock_table

    def test_run_no_rules_returns_full_df_as_valid(self):
        checker, _ = self._make_checker_with_mocks()
        mock_df = MagicMock()
        mock_df.filter.return_value = mock_df
        mock_df.drop.return_value   = mock_df
        mock_df.count.return_value  = 100

        valid_df, invalid_df, results = checker.run(mock_df)

        assert valid_df is mock_df
        assert results == []

    def test_run_returns_three_tuple(self):
        checker, _ = self._make_checker_with_mocks()
        mock_df = MagicMock()
        mock_df.filter.return_value = mock_df
        mock_df.drop.return_value   = mock_df
        mock_df.count.return_value  = 100
        mock_df.withColumn.return_value = mock_df

        rule = DQRule(
            name="test_rule",
            check_fn=lambda df: df,
            error_type="TEST_ERROR",
            description="test",
        )
        checker.add_rule(rule)

        result_tuple = checker.run(mock_df)
        assert len(result_tuple) == 3

    def test_threshold_not_breached_no_dead_letter(self):
        checker, mock_table = self._make_checker_with_mocks(failure_threshold=0.05)
        mock_df = MagicMock()
        mock_df.withColumn.return_value = mock_df
        mock_df.filter.return_value = mock_df
        mock_df.drop.return_value   = mock_df

        # total=1000, failed=20 → failure_rate=0.02 < threshold 0.05
        mock_df.count.side_effect = [1000, 20, 0]  # total, failed, rule-check

        rule = DQRule(
            name="test_rule",
            check_fn=lambda df: df,
            error_type="TEST_ERROR",
            description="test",
        )
        checker.add_rule(rule)

        valid_df, invalid_df, results = checker.run(mock_df)

        assert len(results) == 1
        assert results[0].threshold_breached is False
        assert results[0].dead_letter_path is None

    def test_threshold_breached_sets_dead_letter_path(self):
        checker, mock_table = self._make_checker_with_mocks(failure_threshold=0.05)
        mock_df = MagicMock()
        mock_df.withColumn.return_value = mock_df
        mock_df.filter.return_value = mock_df
        mock_df.drop.return_value   = mock_df
        mock_df.write = MagicMock()
        mock_df.write.mode.return_value = mock_df.write
        mock_df.write.parquet = MagicMock()

        # total=1000, failed=150 → failure_rate=0.15 > threshold 0.05
        mock_df.count.side_effect = [1000, 150, 150]

        rule = DQRule(
            name="test_rule",
            check_fn=lambda df: df,
            error_type="NULL_LOG_ID",
            description="test",
        )
        checker.add_rule(rule)

        valid_df, invalid_df, results = checker.run(mock_df)

        assert len(results) == 1
        result = results[0]
        assert result.threshold_breached is True
        assert result.dead_letter_path is not None
        assert "bronze_app_logs" in result.dead_letter_path
