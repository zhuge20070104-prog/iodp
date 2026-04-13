# tests/unit/conftest.py
"""公共测试 fixtures"""

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_dynamodb_resource():
    """Mock boto3.resource('dynamodb')"""
    with patch("boto3.resource") as mock_resource:
        mock_db   = MagicMock()
        mock_resource.return_value = mock_db
        yield mock_db


@pytest.fixture
def mock_dynamodb_table(mock_dynamodb_resource):
    """Mock DynamoDB Table object"""
    mock_table = MagicMock()
    mock_dynamodb_resource.Table.return_value = mock_table
    return mock_table


@pytest.fixture
def mock_spark_df():
    """Mock PySpark DataFrame"""
    mock_df = MagicMock()
    mock_df.isEmpty.return_value = False
    mock_df.count.return_value = 100
    mock_df.filter.return_value = mock_df
    mock_df.drop.return_value = mock_df
    mock_df.withColumn.return_value = mock_df
    mock_df.write = MagicMock()
    mock_df.write.mode.return_value = mock_df.write
    mock_df.write.parquet = MagicMock()
    return mock_df


@pytest.fixture
def sample_dq_result_dict():
    """Sample DQResult dict for assertions"""
    return {
        "table_name":         "bronze_app_logs",
        "report_timestamp":   "2026-04-06T10:00:00+00:00",
        "job_run_id":         "test-job-run-001",
        "batch_id":           "42",
        "error_type":         "NULL_LOG_ID",
        "total_records":      1000,
        "failed_records":     20,
        "failure_rate":       "0.02",
        "threshold_breached": False,
        "dead_letter_path":   "",
        "environment":        "test",
    }
