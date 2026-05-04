# iodp-bigdata 基础设施详解

本文逐目录讲解 `terraform/`、`athena/`、`schemas/` 三个目录的作用，以及它们如何串联起整条数据流。

---

## 1. schemas/ — Kafka 消息的 JSON Schema 契约

这个目录定义了从 Kafka 进来的原始 JSON 消息长什么样，是整条数据流的"源头契约"。

### schemas/system_app_logs.json

- 定义了 `system_app_logs` Kafka Topic 的消息格式。
- 必填字段：`log_id`（UUID）、`service_name`、`log_level`（DEBUG/INFO/WARN/ERROR/FATAL）、`event_timestamp`、`message`。
- 可选嵌套对象：`error_details`（error_code、error_type、stack_trace、http_status）、`request_info`（method、path、user_id、duration_ms）。
- 对应的 PySpark Schema 定义在 `glue_jobs/lib/schema_definitions.py` 的 `APP_LOG_SCHEMA`。
- 对应的 Glue Streaming Job 是 `stream_app_logs.py`，它的 `from_json()` 就是按这个结构解析的。

### schemas/user_clickstream.json

- 定义了 `user_clickstream` Kafka Topic 的消息格式。
- 必填字段：`event_id`（UUID）、`user_id`（非空）、`session_id`、`event_type`（click/view/scroll/purchase/add_to_cart/checkout）、`event_timestamp`、`page_url`。
- 可选嵌套对象：`device_info`（device_type、os、browser、screen_resolution）、`geo_info`（country_code、city、ip_hash）、`properties`（业务自定义字段如 product_id、amount）。
- 对应的 PySpark Schema 定义在 `glue_jobs/lib/schema_definitions.py` 的 `CLICKSTREAM_SCHEMA`。
- 对应的 Glue Streaming Job 是 `stream_clickstream.py`。

### 这个目录的作用

- 它是给人看的文档，不是被代码直接 import 的。
- 上游服务（发消息到 Kafka 的应用）按这个 Schema 发数据。
- 如果上游改了字段，数据工程师需要同步更新这里的 JSON Schema、`schema_definitions.py` 里的 PySpark Schema、以及 `athena/ddl/` 里的建表 SQL。

---

## 2. terraform/modules/ — 基础设施即代码

这个目录用 Terraform 管理 AWS 资源。每个子目录是一个独立模块，可以在上层 `main.tf` 里被 `module "xxx" { source = "./modules/xxx" }` 引用。

目前有 4 个模块：

### 2.1 terraform/modules/dynamodb/ — 元数据存储

管理 3 张 DynamoDB 表，都是 Glue Job 运行时写入的元数据，不是业务数据。

#### 表 1：iodp-dq-reports-{env}（DQ 质量报告）

- 主键：`table_name`（分区键）+ `report_timestamp`（排序键）。
- 每次 Glue Streaming Job 做完 DQ 校验，都会往这张表写一条报告：总记录数、失败数、失败率、是否超阈值、死信路径。
- 90 天 TTL 自动过期删除。
- 消费方：Agent Log Analyzer 工具可以查这张表了解数据质量状况。
- 写入方：`glue_jobs/lib/data_quality.py` 的 `_write_dq_report()` 方法。

#### 表 2：iodp-lineage-events-{env}（数据血缘）

- 主键：`source_table`（分区键）+ `event_time`（排序键）。
- 每个 Glue Job 跑完都会记一条血缘：从哪张表读了多少条，写到哪张表多少条，做了什么转换。
- 180 天 TTL。
- 写入方：`glue_jobs/lib/lineage.py` 的 `write_lineage_event()` 函数，所有 Glue Job 都调用。

#### 表 3：iodp-dq-threshold-config-{env}（DQ 阈值配置）

- 主键：`table_name`。
- 运营人员可以通过 AWS Console 或 CLI 直接改这张表，调整某张表的 DQ 失败率阈值，不需要重新部署 Glue Job。
- Terraform 预置了 3 条初始配置：
  - `bronze_clickstream`：5%（点击流容忍度高，设备/浏览器数据常有异常）。
  - `bronze_app_logs`：2%（系统日志要求严格，Agent 诊断依赖这个数据）。
  - `silver_parsed_logs`：1%（Silver 层已经过一轮 DQ，允许更低失败率）。
- 读取方：`DataQualityChecker.__init__()` 里的 `load_threshold()` 方法。

### 2.2 terraform/modules/dlq_replay/ — 死信重放 Lambda

管理 DLQ Replay Lambda 函数及其触发机制。

#### 它创建了什么

- **Lambda 函数** `iodp-dlq-replay-{env}`：Python 3.12，512MB 内存，5 分钟超时。
- **IAM Role**：允许读 `dead_letter/*`，写 `replay/*`，写 CloudWatch Logs。
- **EventBridge 规则** `iodp-dlq-replay-trigger-{env}`：默认 **DISABLED**，需要运维手动启用。
- **CloudWatch Log Group**：14 天保留。
- **并发保护**：`reserved_concurrent_executions = 1`，防止多人同时重放导致数据重复。

#### 它在数据流中的位置

```
streaming job 产生死信 → s3://bronze/dead_letter/
                              ↓ (手动触发 Lambda)
                         s3://bronze/replay/
                              ↓ (手动触发 Glue Job)
                         Bronze Iceberg 表
```

Lambda 只做"搬运"（从 dead_letter/ 复制到 replay/），不做数据转换。

#### 手动触发方式

```bash
aws lambda invoke \
  --function-name iodp-dlq-replay-prod \
  --payload '{"table_name":"bronze_app_logs","batch_date":"2026-04-06","dry_run":true}' \
  response.json
```

先 `dry_run: true` 看文件数量，确认后改 `false` 执行复制。

### 2.3 terraform/modules/replay_jobs/ — 死信重灌 Bronze 的 Glue Job

管理 2 个 Glue Batch Job，负责把 Lambda 搬运到 `replay/` 的数据写回 Bronze Iceberg 表。

#### 它创建了什么

- **IAM Role**（两个 Job 共用）：允许读 `replay/*`，读写 `app_logs/*` 和 `clickstream/*`，访问 Glue Catalog，写 DynamoDB lineage 表。
- **Glue Job 1** `iodp-replay-app-logs-to-bronze-{env}`：读 `replay/bronze_app_logs/{date}/` → 去除 `_dq_error_type` 列 → 刷新 `ingest_timestamp` → 补 `event_date` 分区列 → append 到 Bronze Iceberg `app_logs` 表。
- **Glue Job 2** `iodp-replay-clickstream-to-bronze-{env}`：读 `replay/bronze_clickstream/{date}/` → 去除 `_dq_error_type` 列 → 刷新 `ingest_timestamp` → append 到 Bronze Iceberg `clickstream` 表。
- **无 Trigger**：纯手动触发。

#### 关键配置

- Glue 4.0，2 个 G.1X Worker，开启 auto-scaling。
- `max_concurrent_runs = 1`：防止并发重灌。
- `job-bookmark-option = job-bookmark-disable`：replay 是一次性操作，不需要书签。
- `extra-py-files = lib.zip`：打包了 `glue_jobs/lib/` 下的公共模块。

#### 手动触发方式

```bash
# app_logs
aws glue start-job-run \
  --job-name iodp-replay-app-logs-to-bronze-prod \
  --arguments '{"--TABLE_NAME":"bronze_app_logs","--BATCH_DATE":"2026-04-06"}'

# clickstream
aws glue start-job-run \
  --job-name iodp-replay-clickstream-to-bronze-prod \
  --arguments '{"--TABLE_NAME":"bronze_clickstream","--BATCH_DATE":"2026-04-06"}'
```

### 2.4 terraform/modules/vector_indexer/ — Gold 数据索引到 S3 Vectors

管理事件驱动的 S3 Vectors 索引 Lambda，当 Gold 层产出 incident_summary 数据时自动触发索引。
（替代了旧的 OpenSearch Serverless 方案，成本降低约 90%。S3 Vectors 于 2025-12 GA。）

#### 它创建了什么

- **S3 Event Notification**：当 `gold_bucket` 下 `incident_summary/` 路径有新 `.parquet` 文件写入时，触发 Lambda。
- **Lambda 函数** `iodp-vector-indexer-{env}`：Python 3.12，1024MB 内存，5 分钟超时。
- **SQS Dead Letter Queue**：Lambda 处理失败的事件会进入 SQS DLQ，消息保留 14 天。
- **IAM Role**：允许读 Gold bucket、调用 Bedrock Embedding API（`amazon.titan-embed-text-v2:0`）、`s3vectors:PutVectors` 等写入权限、发 SQS、写 CloudWatch Logs。
- **CloudWatch Alarm**：Lambda 连续 2 个 5 分钟窗口内错误超 3 次 → 发 SNS 告警。

注意：vector bucket 与 index 物理资源由 `iodp-agent` 项目的 Terraform 创建，本模块通过 tfvars (`vector_bucket_name` / `vector_bucket_arn`) 引用。

#### 它在数据流中的位置

```
gold_incident_summary.py 写 parquet 到 Gold bucket
    ↓ S3 Event Notification（自动触发）
Lambda: 读 parquet → Bedrock 生成 embedding → put_vectors 到 S3 Vectors index "incident_solutions"
    ↓
Agent RAG Agent 通过 query_vectors 做语义搜索，找历史相似事件
```

#### 关键设计

- `reserved_concurrent_executions = 2`：控制 Bedrock embedding 调用的并发与费率。
- `BATCH_SIZE = 50`：每次最多索引 50 条向量（S3 Vectors `put_vectors` 单次上限 500）。
- 使用 Bedrock Titan Embedding（1024 维, normalize=True）配合 cosine distance 做向量化，支持语义搜索。

---

## 3. athena/ — Iceberg 表的 DDL 和分析视图

这个目录存放需要在 Athena 里执行的 SQL，分两类：`ddl/`（建表）和 `views/`（视图）。

**重要**：这些 SQL 不会被 Terraform 自动执行。它们是手动在 Athena Console 或 CLI 里跑的，或者首次部署时作为初始化脚本执行。Glue Streaming Job 运行前，对应的 Iceberg 表必须已经存在。

### 3.1 athena/ddl/ — Iceberg 建表语句

#### ddl/bronze_clickstream.sql

- 建表：`iodp_bronze_prod.clickstream`。
- 格式：Iceberg + Parquet + Snappy 压缩。
- 分区：`event_type`（click/view/scroll/purchase/add_to_cart/checkout）。
- 18 个业务列 + `ingest_timestamp` + `processing_timestamp`。
- 额外属性：`delete-after-commit.enabled = true`，最多保留 10 个历史元数据版本。
- 写入方：`stream_clickstream.py`（Glue Streaming Job，60 秒 micro-batch）。

#### ddl/silver_enriched_clicks.sql

- 建表：`iodp_silver_prod.enriched_clicks`。
- 格式：Iceberg + Parquet + Snappy。
- 分区：`event_date`（日期）+ `event_type`。比 Bronze 多了 `event_date` 分区，查询效率更高。
- 与 Bronze 结构基本一致，但 `event_id` 保证唯一（MERGE 去重）。
- 额外属性：`write.delete.mode = merge-on-read`，`write.update.mode = merge-on-read`。
- 写入方：`silver_enrich_clicks.py`（每小时 batch）。

#### ddl/gold_api_error_stats.sql

- 建表：`iodp_gold_prod.api_error_stats`。
- 格式：Iceberg + Parquet + Snappy。
- 分区：`stat_date`（日期）+ `service_name`。
- 11 个聚合指标列：`stat_hour`（小时桶）、`service_name`、`error_code`、`total_requests`、`error_count`、`error_rate`、`p99_duration_ms`、`unique_users`、`sample_trace_ids`（最多 5 个 trace_id 样本）。
- 写入方：`gold_api_error_stats.py`（每小时 batch）。
- 消费方：Athena 查询（通过 `v_error_log_enriched` 视图）、Agent Log Analyzer。

### 3.2 athena/views/ — 分析视图

#### views/v_error_log_enriched.sql

- 视图：`iodp_gold_prod.v_error_log_enriched`。
- 用途：**Agent Log Analyzer 工具的主要查询入口**。
- 做了什么：把 Gold 层的聚合错误统计（`api_error_stats`）和 Silver 层的详细日志（`parsed_logs`）JOIN 起来。
- JOIN 条件：`service_name` + `stat_hour = DATE_TRUNC('hour', event_timestamp)` + `error_code`。
- 过滤：只看 `log_level IN ('ERROR', 'FATAL')`。
- 输出：一行里既有聚合指标（错误率、P99、受影响用户数），又有具体日志详情（user_id、请求路径、stack_trace、trace_id）。
- Agent 调用示例：`SELECT * FROM v_error_log_enriched WHERE service_name = 'payment-service' AND stat_hour >= TIMESTAMP '2026-04-06 14:00:00'`。

#### views/v_user_session.sql

- 视图：`iodp_silver_prod.v_user_session`。
- 用途：**BI 工具分析用户行为路径和转化漏斗**。
- 做了什么：把 Silver 层 `enriched_clicks` 按 `session_id + user_id + event_date` 聚合成一行。
- 输出 15 个字段：
  - `session_start` / `session_end`（首尾事件时间）。
  - `session_duration_sec`（会话时长秒数）。
  - `total_events`（会话内总事件数）。
  - 漏斗标记：`has_view`、`has_add_to_cart`、`has_checkout`、`has_purchase`（0/1，表示这个 session 是否到达了该阶段）。
  - `device_type`、`country_code`、`city`（取会话内第一条记录的值）。
  - `total_purchase_amount`（该会话的购买总金额）。
- BI 可以直接查这个视图做漏斗分析：`SELECT COUNT(*) FILTER (WHERE has_view = 1), COUNT(*) FILTER (WHERE has_purchase = 1) FROM v_user_session WHERE event_date = DATE '2026-04-06'`。

---

## 4. 全流程串联：从 Kafka 到 Agent 查询

把上面所有组件串起来，完整数据流如下：

```
                        schemas/
                    (JSON Schema 契约)
                          │
                          │ 上游应用按 Schema 发消息
                          ▼
                    ┌─────────────┐
                    │  MSK Kafka  │
                    │  2 Topics   │
                    └──────┬──────┘
                           │
         ┌─────────────────┴──────────────────┐
         ▼                                    ▼
  stream_app_logs.py                stream_clickstream.py
  (Glue Streaming)                  (Glue Streaming)
         │                                    │
         │ DQ 校验                             │ DQ 校验
         │                                    │
    ┌────┴────┐                          ┌────┴────┐
    ▼         ▼                          ▼         ▼
 Bronze    dead_letter/               Bronze    dead_letter/
 app_logs  (DQ失败>5%)              clickstream  (DQ失败>5%)
    │                                    │
    │ athena/ddl/                        │ athena/ddl/
    │ bronze_clickstream.sql             │ bronze_clickstream.sql
    │ (建表 SQL)                         │ (建表 SQL)
    │                                    │
    ▼                                    ▼
  silver_parse_logs.py            silver_enrich_clicks.py
  (每小时 Batch)                  (每小时 Batch)
    │                                    │
    ▼                                    ▼
 Silver                              Silver
 parsed_logs                       enriched_clicks
    │                                    │
    │                                    │ athena/views/
    │                                    │ v_user_session.sql
    │                                    ▼
    │                              BI 漏斗分析
    │
    ├──→ gold_api_error_stats.py (每小时 Batch)
    │         │
    │         ▼
    │    Gold api_error_stats
    │         │
    │         │ athena/views/
    │         │ v_error_log_enriched.sql (JOIN Gold + Silver)
    │         ▼
    │    Agent Log Analyzer 查询
    │
    └──→ gold_incident_summary.py (每天 Batch)
              │
              ▼
         Gold incident_summary
              │
              │ terraform/modules/vector_indexer/
              │ (S3 Event → Lambda → Bedrock Embedding → S3 Vectors)
              ▼
         Agent 语义搜索历史相似事件


  dead_letter/ 重灌回路（手动）：
  ─────────────────────────────
  terraform/modules/dlq_replay/      terraform/modules/replay_jobs/
  (Lambda: 搬运文件)                  (Glue Job: 写回 Bronze)

  dead_letter/ ──Lambda──→ replay/ ──Glue Job──→ Bronze Iceberg
                                                      │
                                               Silver Job 自动消费


  terraform/modules/dynamodb/
  (元数据存储，贯穿全流程)
  ─────────────────────────────
  • dq_reports：每次 DQ 校验写一条报告
  • lineage_events：每个 Job 写一条血缘
  • dq_threshold_config：运营动态调整 DQ 阈值
```

### Terraform 模块职责总结

| 模块 | 管理什么 | 被谁用 | 何时触发 |
|------|---------|--------|---------|
| `dynamodb` | 3 张 DynamoDB 元数据表 | 所有 Glue Job（写 DQ 报告 + 血缘）| 随 Job 运行自动写入 |
| `dlq_replay` | 1 个 Lambda + EventBridge | 运维手动 | CLI 调用或启用 EventBridge 规则 |
| `replay_jobs` | 2 个 Glue Batch Job | 运维手动 | `aws glue start-job-run` |
| `vector_indexer` | 1 个 Lambda + SQS DLQ + 告警 | S3 Event 自动触发 | Gold incident_summary 写入新 parquet 时 |
