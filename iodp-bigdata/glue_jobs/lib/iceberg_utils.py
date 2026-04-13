# glue_jobs/lib/iceberg_utils.py
"""
Iceberg 表管理工具函数
封装建表、分区管理等重复操作，各 Glue Job 共用。
"""

import logging
from typing import List

from pyspark.sql import SparkSession

logger = logging.getLogger(__name__)


def configure_iceberg(spark: SparkSession, warehouse_path: str) -> None:
    """配置 Spark Session 支持 Iceberg + Glue Catalog"""
    spark.conf.set(
        "spark.sql.extensions",
        "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
    )
    spark.conf.set("spark.sql.catalog.glue_catalog", "org.apache.iceberg.aws.glue.GlueCatalog")
    spark.conf.set("spark.sql.catalog.glue_catalog.warehouse", warehouse_path)
    spark.conf.set("spark.sql.catalog.glue_catalog.io-impl", "org.apache.iceberg.aws.s3.S3FileIO")


def ensure_iceberg_table(
    spark: SparkSession,
    full_table_name: str,
    ddl_sql: str,
) -> None:
    """
    若表不存在则建表，存在则跳过。
    full_table_name 格式: glue_catalog.{database}.{table}
    ddl_sql: CREATE TABLE IF NOT EXISTS ... 语句
    """
    try:
        spark.sql(ddl_sql)
        logger.info("Table ensured: %s", full_table_name)
    except Exception as e:
        logger.warning("ensure_iceberg_table skipped (%s): %s", full_table_name, e)


def iceberg_merge_dedup(
    spark: SparkSession,
    source_view: str,
    target_table: str,
    merge_keys: List[str],
) -> int:
    """
    使用 Iceberg MERGE INTO 对目标表进行去重写入。
    source_view: 已注册为 Spark 临时视图的 DataFrame 名称
    merge_keys:  用于匹配去重的字段列表，如 ["log_id"]
    返回：写入行数
    """
    on_clause = " AND ".join([f"t.{k} = s.{k}" for k in merge_keys])
    merge_sql = f"""
        MERGE INTO {target_table} t
        USING {source_view} s
        ON {on_clause}
        WHEN NOT MATCHED THEN INSERT *
    """
    spark.sql(merge_sql)
    count = spark.sql(f"SELECT COUNT(*) FROM {target_table}").collect()[0][0]
    logger.info("MERGE INTO %s complete. Table now has %d rows.", target_table, count)
    return count
