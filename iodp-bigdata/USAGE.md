# Athena 查询使用规范

## 两条消费线路

Silver 层数据向下分为 BI 和 Agent 两条独立的消费线路：

```
Silver 数据
    │
    ├──→ BI 线路（给人看的）
    │    ├── gold.hourly_active_users   → Dashboard 漏斗趋势图
    │    │   全站每小时活跃/浏览/加购/结算/付款用户数 + 新用户数
    │    │
    │    └── v_user_session（视图）     → 分析师探索性查询
    │        按 session 聚合：会话时长、漏斗标记、设备/地理、购买金额
    │
    └──→ Agent 线路（给 AI 故障诊断用的）
         ├── gold.incident_summary      → OpenSearch RAG 知识库
         │   每天识别持续 >=2 小时的高错误率故障，生成摘要
         │   经 Bedrock Embedding 向量化后写入 OpenSearch
         │   Agent 用途："历史上有没有类似的故障？当时怎么解决的？"
         │
         └── v_error_log_enriched（视图）→ Agent 实时查询入口
             JOIN Gold 错误统计 + Silver 日志详情
             Agent 用途："payment-service 14:00 报了什么错？影响了哪些用户？"
```

---

## 视图 vs Gold 表：什么时候该用哪个

同一份 Silver 数据服务不同的消费场景：

| 消费方 | 数据源 | 粒度 | 扫描量 | 适用场景 |
|--------|--------|------|--------|---------|
| Dashboard "运营概览" | `gold.hourly_active_users` | 每小时一行 | 极小（几百行） | 全站漏斗趋势、老板看板 |
| Dashboard "服务健康" | `gold.api_error_stats` | 每服务每小时一行 | 小 | SRE 错误率监控、P99 趋势 |
| Agent RAG 知识库 | `gold.incident_summary` | 每天每故障一行 | 小 | AI 语义搜索历史相似故障 |
| Agent 错误诊断 | `gold.v_error_log_enriched`（视图） | JOIN 后的明细 | 中等 | Agent 关联错误统计 + 日志详情 |
| 分析师探索性查询 | `silver.v_user_session`（视图） | 每 session 一行 | **大，必须加过滤条件** | 单用户行为路径分析 |

**原则**：能用 Gold 表的就用 Gold 表（预聚合、便宜、快）。视图只用于 Gold 表无法覆盖的探索性分析场景。

---

## v_user_session 视图查询规范

`v_user_session` 是一个基于 `silver.enriched_clicks` 的聚合视图，**没有内置过滤条件**。每次查询都会实时扫描 Silver 层原始数据做 GROUP BY。

### 必须加 event_date 过滤

`event_date` 是 `enriched_clicks` 表的 Iceberg 分区键。加了这个 WHERE 条件后，Iceberg 会跳过其他日期的 parquet 文件，扫描量降低几个数量级。

```sql
-- 危险：扫描全量 Silver 数据，可能几亿行，Athena 账单爆炸
SELECT * FROM v_user_session;

-- 正确：利用 Iceberg 分区裁剪，只扫一天的数据
SELECT * FROM v_user_session
WHERE event_date = DATE '2026-04-06'
  AND user_id = 'u_12345';
```

### 典型查询示例

```sql
-- 查看某用户昨天的会话转化情况
SELECT session_id, session_duration_sec, total_events,
       has_view, has_add_to_cart, has_checkout, has_purchase,
       total_purchase_amount
FROM iodp_silver_prod.v_user_session
WHERE event_date = DATE '2026-04-14'
  AND user_id = 'u_12345';

-- 查看昨天所有"加购未付款"的会话（流失分析）
SELECT session_id, user_id, device_type, country_code
FROM iodp_silver_prod.v_user_session
WHERE event_date = DATE '2026-04-14'
  AND has_checkout = 1
  AND has_purchase = 0;

-- 查看某天各设备类型的平均会话时长
SELECT device_type,
       COUNT(*)                       AS sessions,
       AVG(session_duration_sec)      AS avg_duration_sec,
       AVG(total_purchase_amount)     AS avg_purchase_amount
FROM iodp_silver_prod.v_user_session
WHERE event_date = DATE '2026-04-14'
GROUP BY device_type;
```

---

## v_error_log_enriched 视图查询规范

`v_error_log_enriched` JOIN 了 Gold `api_error_stats` 和 Silver `parsed_logs`，同样需要控制扫描范围。

### 必须加 stat_hour 或 service_name 过滤

```sql
-- 正确：限定时间范围和服务
SELECT service_name, error_code, error_rate, error_count,
       user_id, req_path, stack_trace, trace_id
FROM iodp_gold_prod.v_error_log_enriched
WHERE stat_hour >= TIMESTAMP '2026-04-14 10:00:00'
  AND stat_hour <  TIMESTAMP '2026-04-14 14:00:00'
  AND service_name = 'payment-service';

-- 查看某时段所有高错误率服务
SELECT service_name, error_code, error_rate, unique_users
FROM iodp_gold_prod.v_error_log_enriched
WHERE stat_hour >= TIMESTAMP '2026-04-14 14:00:00'
  AND stat_hour <  TIMESTAMP '2026-04-14 15:00:00'
  AND error_rate > 0.05;
```

---

## DynamoDB dq_reports 表的跨项目使用链路

`iodp-dq-reports-{env}` 表是 bigdata 端写入、agent 端读取的跨项目数据桥梁。

### 写入方（iodp-bigdata）

```
stream_app_logs.py / stream_clickstream.py
    → DataQualityChecker.run()          # glue_jobs/lib/data_quality.py:160
        → _write_dq_report()            # glue_jobs/lib/data_quality.py:239
            → DynamoDB put_item
              → iodp-dq-reports-{env}
```

每次 Streaming Job 做完 DQ 校验，都会往这张表写一条报告，包含：失败率、是否超阈值、死信路径、错误类型。

### 读取方（iodp-agent）

```
log_analyzer_agent.py:109-115
    → query_dq_reports(                 # iodp-agent/src/tools/dynamodb_tool.py:16
          table_name="bronze_app_logs",
          time_start=...,
          time_end=...,
      )
    → DynamoDB query
      → iodp-dq-reports-{env}
```

Agent 诊断故障时，先查该时段的 DQ 报告。如果发现 DQ 失败率飙高、大量数据进了死信区，Agent 就知道"这不是服务本身的故障，是上游数据质量出了问题"，避免误判。

### 涉及的代码文件

| 文件 | 作用 |
|------|------|
| `iodp-bigdata/glue_jobs/lib/data_quality.py` | `_write_dq_report()` 写入 DQ 报告 |
| `iodp-bigdata/terraform/modules/dynamodb/main.tf` | 建表（90 天 TTL 自动过期） |
| `iodp-agent/src/tools/dynamodb_tool.py` | `query_dq_reports()` 查询封装 |
| `iodp-agent/src/graph/nodes/log_analyzer_agent.py` | Agent 调用点 |
| `iodp-agent/src/config.py` | 表名配置：`dq_reports_table = "iodp-dq-reports-prod"` |

---

## OpenSearch Indexer SQS DLQ 用途与重新消费

`iodp-opensearch-indexer-dlq-{env}` 是 OpenSearch 索引 Lambda 的死信队列，定义在 `terraform/modules/opensearch_indexer/main.tf`。

### 什么时候会有消息进去

正常情况下 DLQ 是空的。只有 Lambda 执行失败（且 AWS 自动重试 2 次仍然失败）时，AWS 才会把触发事件投递到 DLQ：

```
Gold S3 写入新 parquet
    → S3 Event 触发 Lambda
        ├── 成功 → DLQ 无事发生
        └── 失败 → AWS 自动重试 2 次
                    ├── 重试成功 → DLQ 无事发生
                    └── 仍然失败 → AWS 把 S3 Event 投递到 SQS DLQ（保留 14 天）
```

### DLQ 里存的是什么

原始的 S3 Event Notification JSON，就是 Lambda handler 的 `event` 参数：

```json
{
  "Records": [{
    "s3": {
      "bucket": { "name": "iodp-gold-prod-123456" },
      "object": { "key": "incident_summary/stat_date=2026-04-06/part-00000.parquet" }
    }
  }]
}
```

### 如何重新消费

排查并修复失败原因（OpenSearch 不可达、Bedrock 限流等）后，手动取出消息重新触发：

```bash
# 1. 从 DLQ 取消息
aws sqs receive-message \
  --queue-url https://sqs.us-east-1.amazonaws.com/{account}/iodp-opensearch-indexer-dlq-prod

# 2. 拿到 Body 里的 S3 Event JSON，喂给同一个 Lambda
aws lambda invoke \
  --function-name iodp-opensearch-indexer-prod \
  --payload '上一步拿到的 Body 内容' \
  response.json

# 3. 成功后删除 DLQ 里的消息
aws sqs delete-message \
  --queue-url https://sqs.us-east-1.amazonaws.com/{account}/iodp-opensearch-indexer-dlq-prod \
  --receipt-handle "从 step 1 拿到的 ReceiptHandle"
```

---

## Terraform IAM 权限结构：托管策略 vs 内联策略

Glue Job / Lambda 的 IAM Role 通常由两部分权限叠加而成：

```
aws_iam_role（自定义角色）
    │
    ├── aws_iam_role_policy_attachment     ← 引用 AWS 托管策略（AWS 预定义好的）
    │   例如 AWSGlueServiceRole，包含 Glue 运行必需的基础权限：
    │     - 写 CloudWatch Logs
    │     - 读写 Glue 临时目录（TempDir）
    │     - 上报 Glue Job 运行指标
    │     - 访问 Glue Data Catalog 基础 API
    │
    └── aws_iam_role_policy                ← 自定义内联策略（我们业务特有的）
        例如 replay_jobs 的自定义权限：
          - S3 读 replay/、写 app_logs/ clickstream/
          - Glue Catalog 读写 Bronze 数据库
          - DynamoDB 写 lineage 表
```

两个叠加在一起才是完整权限。缺了托管策略，Glue Job 连日志都写不出来就会报权限错误。

### 本项目中的使用位置

| Terraform 文件 | 托管策略 | 自定义内联策略 |
|---|---|---|
| `terraform/modules/replay_jobs/main.tf:30-33` | `AWSGlueServiceRole`（Glue 基础权限） | S3 replay/bronze + Glue Catalog + DynamoDB lineage |
| `terraform/modules/dlq_replay/main.tf:18-65` | 无（Lambda 不需要托管策略） | S3 dead_letter 读 + replay 写 + CloudWatch Logs |
| `terraform/modules/opensearch_indexer/main.tf:28-87` | 无 | S3 Gold 读 + Bedrock + OpenSearch + SQS DLQ + CloudWatch Logs |

Lambda 的基础权限（CloudWatch Logs）直接写在内联策略里，不需要额外的托管策略。Glue Job 因为涉及 Spark 集群管理、临时文件等复杂运行时，AWS 打包了一个专用的托管策略。

---

## Terraform output 机制

Terraform 模块的 `outputs.tf` 声明了模块对外暴露的值，**相当于函数的返回值**。

### 自动输出

`terraform apply` 完成后自动打印所有 output：

```
Apply complete! Resources: 5 added.

Outputs:

lambda_arn = "arn:aws:lambda:us-east-1:123456:function:iodp-dlq-replay-prod"
lambda_function_name = "iodp-dlq-replay-prod"
eventbridge_rule_name = "iodp-dlq-replay-trigger-prod"
```

之后可以用 `terraform output lambda_arn` 随时查看。

### 跨模块引用

output 更重要的用途是让上层模块能引用子模块创建的资源：

```hcl
# 上层 main.tf
module "dlq_replay" {
  source             = "./modules/dlq_replay"
  environment        = "prod"
  bronze_bucket_name = "iodp-bronze-prod-123456"
}

# 其他地方通过 module.dlq_replay.xxx 引用 output 值
resource "aws_something" "example" {
  lambda_arn = module.dlq_replay.lambda_arn
}
```

如果 `outputs.tf` 里没有声明某个值，上层模块就无法引用它。不声明 = 模块内部私有，声明了才是公开接口。

---

## Makefile 语法备忘

### 每行命令在独立 shell 中执行

Makefile 的每一行命令都启动一个新的 shell 进程，上一行的 `cd` 对下一行无效：

```makefile
# 错误：第二行还在原目录，找不到 .tf 文件
init:
	cd terraform
	terraform init

# 正确：用 && 串在同一行，同一个 shell 里执行
init:
	cd terraform && terraform init
	cd terraform && terraform plan     # 又是新 shell，需要重新 cd
```

这就是为什么 Makefile 里每一步都重复 `cd $(TF_DIR) &&`。

### @ 前缀：静默命令本身

`@` 让 Make 不打印命令本身，只打印命令的输出：

```makefile
# 不加 @：终端显示两行（命令 + 输出）
echo "初始化 Terraform..."
# → echo "初始化 Terraform..."
# → 初始化 Terraform...

# 加了 @：终端只显示输出
@echo "初始化 Terraform..."
# → 初始化 Terraform...
```

`echo` 前面基本都加 `@`，否则会显示两遍。`terraform init` 这种实际命令不加 `@`，因为你想看到正在执行什么。

---

## 成本保护建议

1. **Athena Workgroup 扫描上限**：在 Athena Workgroup 设置 `per-query data usage limit`（例如 10GB），防止意外全表扫描。
2. **优先查 Gold 表**：Dashboard 和定期报表只用 Gold 表，不用视图。
3. **视图查询必须带分区键**：`v_user_session` 带 `event_date`，`v_error_log_enriched` 带 `stat_hour` 范围。
4. **避免 SELECT ***：只取需要的列，Parquet 列式存储按列计费，少选列 = 少花钱。
