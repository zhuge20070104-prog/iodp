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
    """配置 Spark Session 支持 Iceberg + Glue Catalog。

    spark.sql.extensions 是静态配置，必须在 SparkSession 创建前设。Glue 4.0 上
    --datalake-formats=iceberg 能加载所有 Iceberg jar，但**不可靠地**注入
    spark.sql.extensions（实测：DataFrame writeTo 成功、spark.sql MERGE INTO
    会抛 "MERGE INTO TABLE is not supported temporarily"）。
    所以 compute/main.tf 和 replay_jobs/main.tf 给所有 7 个 Glue Job 都显式加了
    --conf spark.sql.extensions=...IcebergSparkSessionExtensions（silver MERGE
    必需；gold/replay 用 DataFrame writeTo 不严格需要，但保持一致避免后续踩坑）。

    catalog.<name> 设置的是 Spark CatalogPlugin 实现 —— 必须是
    org.apache.iceberg.spark.SparkCatalog。把 Iceberg 的 GlueCatalog 当 Spark
    plugin 用会报「does not implement CatalogPlugin」。
    GlueCatalog 是 Iceberg 内部 catalog 实现，要通过 .catalog-impl 配置。
    """
    spark.conf.set("spark.sql.catalog.glue_catalog", "org.apache.iceberg.spark.SparkCatalog")
    spark.conf.set("spark.sql.catalog.glue_catalog.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
    spark.conf.set("spark.sql.catalog.glue_catalog.warehouse", warehouse_path)
    spark.conf.set("spark.sql.catalog.glue_catalog.io-impl", "org.apache.iceberg.aws.s3.S3FileIO")
    # Athena DDL 把 timestamp 列声明为 TIMESTAMP（无时区），Iceberg 把它当
    # timestamp-without-zone；Spark 的 TimestampType 等价于 timestamp-with-zone，
    # 默认会拒绝读写，抛 IllegalArgumentException。开关打开后 Spark 按 UTC 解释。
    spark.conf.set("spark.sql.iceberg.handle-timestamp-without-timezone", "true")


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


def iceberg_merge_upsert(
    spark: SparkSession,
    source_view: str,
    target_table: str,
    merge_keys: List[str],
) -> int:
    """
    使用 Iceberg MERGE INTO 做 UPSERT：键命中则 UPDATE 所有列，否则 INSERT。

    跟 iceberg_merge_dedup 的差异：dedup 只 INSERT 新行（不动旧行），
    upsert 命中则用 source 的列覆盖 target。

    适合 Gold 层「按小时聚合 + 任务按小时切片跑」的写入：
      - merge_keys 选业务唯一键（如 stat_hour + service + error_code）
      - 同一小时重跑：UPDATE 行内字段（聚合结果可能微调，例如延后到达的数据）
      - 新小时首次跑：INSERT 新行
      - 与 .overwritePartitions() 的本质区别：它擦的是整个分区里所有 stat_hour
        的行，破坏跨小时累积；MERGE 只动 merge_keys 命中的那几行。
    """
    on_clause = " AND ".join([f"t.{k} = s.{k}" for k in merge_keys])
    merge_sql = f"""
        MERGE INTO {target_table} t
        USING {source_view} s
        ON {on_clause}
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """
    spark.sql(merge_sql)
    count = spark.sql(f"SELECT COUNT(*) FROM {target_table}").collect()[0][0]
    logger.info("UPSERT %s complete. Table now has %d rows.", target_table, count)
    return count
