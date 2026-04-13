# glue_jobs/lib/data_quality.py
"""
公共数据质量校验框架 v2
改进点：
  - DataQualityChecker 支持从 DynamoDB dq_threshold_config 表按表名加载阈值
  - 新增 rule_valid_uuid() 规则工厂
  - failure_threshold 仅作为兜底默认值，优先读取 DynamoDB 配置
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

import boto3
from botocore.exceptions import ClientError
from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit

logger = logging.getLogger(__name__)

# ─── 合法的 error_code 字典（从配置中心加载，此处硬编码示例）───
VALID_ERROR_CODES = {
    "E1001", "E1002", "E1003",   # 认证错误
    "E2001", "E2002", "E2003",   # 支付错误
    "E3001", "E3002",            # 网关超时
    "E4001", "E4002", "E4003",   # 业务逻辑错误
}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class DQRule:
    """单条数据质量规则"""
    name: str
    check_fn: Callable[[DataFrame], DataFrame]  # 返回带 _dq_{name} 布尔列的 DataFrame
    error_type: str
    description: str


@dataclass
class DQResult:
    """单次批次的 DQ 检验结果"""
    table_name: str
    batch_id: str
    job_run_id: str
    total_records: int
    failed_records: int
    failure_rate: float
    error_type: str
    threshold_breached: bool
    dead_letter_path: Optional[str] = None
    environment: str = "prod"


class DataQualityChecker:
    """
    数据质量检查器 v2 —— 支持每张表独立阈值

    使用方式:
        checker = DataQualityChecker(
            table_name="bronze_app_logs",
            threshold_config_table="iodp_dq_threshold_config_prod",  # 新增
            ...
        )
        checker.add_rule(rule1).add_rule(rule2)
        valid_df, dl_df, results = checker.run(df)
    """

    def __init__(
        self,
        table_name: str,
        batch_id: str,
        job_run_id: str,
        dead_letter_base_path: str,
        dq_table_name: str,
        failure_threshold: float = 0.05,          # 兜底默认值
        threshold_config_table: Optional[str] = None,  # NEW: per-table DynamoDB config
        environment: str = "prod",
        aws_region: str = "us-east-1",
    ):
        self.table_name = table_name
        self.batch_id = batch_id
        self.job_run_id = job_run_id
        self.dead_letter_base_path = dead_letter_base_path
        self.dq_table_name = dq_table_name
        self.environment = environment
        self._rules: List[DQRule] = []
        self._dynamodb = boto3.resource("dynamodb", region_name=aws_region)

        # 优先从 DynamoDB 配置表读取阈值，失败时回退到构造参数
        if threshold_config_table:
            self.failure_threshold = self.load_threshold(
                table_name=table_name,
                config_dynamodb_table=threshold_config_table,
                dynamodb_resource=self._dynamodb,
                fallback=failure_threshold,
            )
        else:
            self.failure_threshold = failure_threshold

    @staticmethod
    def load_threshold(
        table_name: str,
        config_dynamodb_table: str,
        dynamodb_resource=None,
        fallback: float = 0.05,
        aws_region: str = "us-east-1",
    ) -> float:
        """
        从 DynamoDB dq_threshold_config 表按 table_name 加载阈值。
        任何异常（网络、表不存在、项目不存在）均返回 fallback 值。

        DynamoDB Schema:
          PK:  table_name  (String)
          Att: failure_threshold (Number)
        """
        if dynamodb_resource is None:
            dynamodb_resource = boto3.resource("dynamodb", region_name=aws_region)
        try:
            config_table = dynamodb_resource.Table(config_dynamodb_table)
            response = config_table.get_item(Key={"table_name": table_name})
            item = response.get("Item")
            if item and "failure_threshold" in item:
                threshold = float(item["failure_threshold"])
                logger.info(
                    "Loaded per-table DQ threshold for %s: %.4f (from DynamoDB)",
                    table_name, threshold,
                )
                return threshold
            else:
                logger.info(
                    "No threshold config found for %s in DynamoDB; using fallback %.4f",
                    table_name, fallback,
                )
                return fallback
        except ClientError as e:
            logger.warning(
                "DynamoDB ClientError reading threshold config for %s: %s. Using fallback %.4f",
                table_name, e, fallback,
            )
            return fallback
        except Exception as e:
            logger.warning(
                "Unexpected error reading threshold config for %s: %s. Using fallback %.4f",
                table_name, e, fallback,
            )
            return fallback

    def add_rule(self, rule: DQRule) -> "DataQualityChecker":
        self._rules.append(rule)
        return self

    def run(self, df: DataFrame):
        """
        执行所有 DQ 规则，返回 (valid_df, dead_letter_df, dq_results)
        """
        if not self._rules:
            return df, df.filter(lit(False)), []

        # 对每条规则标记有效性
        validated_df = df
        for rule in self._rules:
            validated_df = rule.check_fn(validated_df)

        rule_cols = [f"_dq_{rule.name}" for rule in self._rules]

        valid_df = validated_df
        for c in rule_cols:
            valid_df = valid_df.filter(col(c) == True)
        valid_df = valid_df.drop(*rule_cols)

        invalid_df = validated_df
        # 有任意一列为 False 则判定为无效
        from pyspark.sql.functions import when as spark_when
        condition = None
        for c in rule_cols:
            cond = col(c) == False
            condition = cond if condition is None else condition | cond
        invalid_df = validated_df.filter(condition)

        total = df.count()
        failed = invalid_df.count()
        failure_rate = failed / total if total > 0 else 0.0
        threshold_breached = failure_rate > self.failure_threshold

        # 确定主要错误类型（取第一个有失败记录的规则）
        dominant_error_type = "UNKNOWN"
        for rule in self._rules:
            col_name = f"_dq_{rule.name}"
            if invalid_df.filter(col(col_name) == False).count() > 0:
                dominant_error_type = rule.error_type
                break

        dead_letter_path = None
        if threshold_breached and failed > 0:
            dead_letter_path = (
                f"{self.dead_letter_base_path}"
                f"table={self.table_name}/"
                f"batch_id={self.batch_id}/"
                f"date={datetime.now(timezone.utc).strftime('%Y-%m-%d')}/"
            )
            dead_letter_df = invalid_df.drop(*rule_cols).withColumn(
                "_dq_error_type", lit(dominant_error_type)
            )
            dead_letter_df.write.mode("overwrite").parquet(dead_letter_path)
            logger.warning(
                "DQ threshold breached: %.2f%% failure rate for %s (threshold: %.2f%%). "
                "Dead letter written to: %s",
                failure_rate * 100,
                self.table_name,
                self.failure_threshold * 100,
                dead_letter_path,
            )

        result = DQResult(
            table_name=self.table_name,
            batch_id=self.batch_id,
            job_run_id=self.job_run_id,
            total_records=total,
            failed_records=failed,
            failure_rate=round(failure_rate, 6),
            error_type=dominant_error_type,
            threshold_breached=threshold_breached,
            dead_letter_path=dead_letter_path,
            environment=self.environment,
        )

        self._write_dq_report(result)

        return valid_df, invalid_df.drop(*rule_cols), [result]

    def _write_dq_report(self, result: DQResult):
        """将 DQ 报告写入 DynamoDB"""
        try:
            table = self._dynamodb.Table(self.dq_table_name)
            now_iso = datetime.now(timezone.utc).isoformat()
            ttl_seconds = int(datetime.now(timezone.utc).timestamp()) + 90 * 86400

            table.put_item(Item={
                "table_name":         result.table_name,
                "report_timestamp":   now_iso,
                "job_run_id":         result.job_run_id,
                "batch_id":           result.batch_id,
                "error_type":         result.error_type,
                "total_records":      result.total_records,
                "failed_records":     result.failed_records,
                "failure_rate":       str(result.failure_rate),
                "threshold_breached": result.threshold_breached,
                "dead_letter_path":   result.dead_letter_path or "",
                "environment":        result.environment,
                "TTL":                ttl_seconds,
            })
        except Exception as e:
            logger.error("Failed to write DQ report to DynamoDB: %s", e)


# ─── 内置规则工厂函数 ───

def rule_not_null(column: str, rule_name: Optional[str] = None) -> DQRule:
    """字段非 Null 规则"""
    name = rule_name or f"not_null_{column}"
    def check(df: DataFrame) -> DataFrame:
        return df.withColumn(f"_dq_{name}", col(column).isNotNull())
    return DQRule(
        name=name,
        check_fn=check,
        error_type=f"NULL_{column.upper()}",
        description=f"Column '{column}' must not be null",
    )


def rule_timestamp_in_range(
    column: str,
    max_lag_hours: int = 24,
    rule_name: Optional[str] = None,
) -> DQRule:
    """时间戳必须在当前时间的 max_lag_hours 小时内"""
    from pyspark.sql.functions import (
        abs as spark_abs, current_timestamp, to_timestamp, unix_timestamp,
    )
    name = rule_name or f"ts_in_range_{column}"
    def check(df: DataFrame) -> DataFrame:
        max_diff_seconds = max_lag_hours * 3600
        return df.withColumn(
            f"_dq_{name}",
            spark_abs(
                unix_timestamp(current_timestamp()) -
                unix_timestamp(to_timestamp(col(column)))
            ) <= max_diff_seconds,
        )
    return DQRule(
        name=name,
        check_fn=check,
        error_type="TIMESTAMP_OUT_OF_RANGE",
        description=f"Column '{column}' must be within {max_lag_hours}h of current time",
    )


def rule_in_set(column: str, valid_values: set, rule_name: Optional[str] = None) -> DQRule:
    """字段值必须在字典范围内（允许 None 值，None 视为通过）"""
    name = rule_name or f"in_set_{column}"
    # Filter out None so isin() doesn't break; handle null separately
    valid_list = [v for v in valid_values if v is not None]
    allow_null = None in valid_values

    def check(df: DataFrame) -> DataFrame:
        in_set_cond = col(column).isin(valid_list)
        if allow_null:
            in_set_cond = in_set_cond | col(column).isNull()
        return df.withColumn(f"_dq_{name}", in_set_cond)

    return DQRule(
        name=name,
        check_fn=check,
        error_type=f"INVALID_{column.upper()}",
        description=f"Column '{column}' must be in {valid_list[:5]}...",
    )


def rule_valid_uuid(column: str, rule_name: Optional[str] = None, nullable: bool = True) -> DQRule:
    """
    字段值必须符合 UUID v4 格式。
    nullable=True（默认）：NULL 值视为通过（字段可选）
    nullable=False：NULL 值视为失败
    """
    from pyspark.sql.functions import regexp_extract
    name = rule_name or f"valid_uuid_{column}"
    uuid_pattern = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"

    def check(df: DataFrame) -> DataFrame:
        # regexp_extract returns "" if no match; use rlike for boolean
        from pyspark.sql.functions import rlike
        matched = col(column).rlike(uuid_pattern)
        if nullable:
            cond = col(column).isNull() | matched
        else:
            cond = matched
        return df.withColumn(f"_dq_{name}", cond)

    return DQRule(
        name=name,
        check_fn=check,
        error_type=f"INVALID_UUID_{column.upper()}",
        description=f"Column '{column}' must be a valid UUID v4{' (nullable)' if nullable else ''}",
    )
