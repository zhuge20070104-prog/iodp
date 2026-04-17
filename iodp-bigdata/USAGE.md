# IODP BigData 使用规范

## 部署顺序：为什么必须三步 bootstrap

Glue Database 只能由 Terraform 建（`aws_glue_catalog_database`），而 Iceberg Table 只能由 Athena DDL 建（`CREATE TABLE ... TBLPROPERTIES ('table_type' = 'ICEBERG')`）。Terraform Provider 对 Iceberg `TBLPROPERTIES` 支持有限，强行在 `aws_glue_catalog_table` 里写出来很啰嗦且容易漂移，所以本项目把"建表"这一步交给 Athena DDL。

由此引出一个严格的线性依赖：

```
[1] Terraform apply              [2] Athena DDL                [3] Glue Triggers
    创建 Database          →         创建 Iceberg Table    →         开始 cron 触发
    (iodp_silver_dev)              (silver.parsed_logs)           Glue Job 正常写表
```

如果顺序错乱：

| 错误场景 | 后果 |
|---------|------|
| 先跑 DDL，后跑 Terraform | Athena 报 `Database does not exist` ❌ |
| Terraform 启用 Triggers 后才跑 DDL | Cron 触发的 Glue Job 报 `TableNotFoundException` ❌ |

### Makefile 三段式部署

```bash
make init   # = deploy-infra + deploy-ddl + enable-triggers（一键完成）
```

或手动分步执行（排障时更清楚是哪一步失败）：

```bash
make deploy-infra      # [1/3] terraform apply -var='triggers_enabled=false'
make deploy-ddl        # [2/3] bash scripts/apply_ddl.sh ENV ACCOUNT_ID REGION
make enable-triggers   # [3/3] terraform apply -var='triggers_enabled=true'
```

### triggers_enabled 变量

`terraform/variables.tf` 中的 `triggers_enabled`（默认 `true`）控制三个 `aws_glue_trigger` 资源的 `enabled` 属性：

- **首次部署**：`make deploy-infra` 强制传 `-var='triggers_enabled=false'`，Triggers 创建但不激活，避免 cron 在 DDL 之前触发 Glue Job。
- **启用阶段**：`make enable-triggers` 传 `-var='triggers_enabled=true'`，Triggers 切换到 active 状态。
- **日常 `make deploy`**：不传 var，走变量默认值 `true`，Triggers 保持启用。

这样 Terraform state 和 AWS 实际状态始终一致，不需要额外的 `aws glue start-trigger` / `stop-trigger` CLI 操作，也不会出现 drift。

---

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

---

## Storage 模块详解

`terraform/main.tf` 中的 `module "storage"` 调用 `./modules/storage` 子模块，负责创建项目的所有 S3 存储桶，是整个数据湖的存储基座。

```hcl
module "storage" {
  source = "./modules/storage"

  environment         = var.environment
  bronze_bucket_name  = "iodp-bronze-${var.environment}-${var.aws_account_id}"
  silver_bucket_name  = "iodp-silver-${var.environment}-${var.aws_account_id}"
  gold_bucket_name    = "iodp-gold-${var.environment}-${var.aws_account_id}"
  scripts_bucket_name = "iodp-glue-scripts-${var.environment}-${var.aws_account_id}"
  dead_letter_prefix  = "dead_letter/"
  ia_transition_days      = 30
  glacier_transition_days = 90
  expiration_days         = 365
  tags                    = local.mandatory_tags
}
```

### S3 桶命名（数据湖三层架构）

| 参数 | 用途 | 命名示例 |
|------|------|---------|
| `bronze_bucket_name` | **Bronze 层**（原始数据）：从 MSK 消费的 raw JSON 直接落到这里 | `iodp-bronze-dev-123456789012` |
| `silver_bucket_name` | **Silver 层**（清洗数据）：经过 DQ 校验、解析、enrichment 后的数据 | `iodp-silver-dev-123456789012` |
| `gold_bucket_name` | **Gold 层**（聚合数据）：面向业务的汇总表（如 `gold_api_error_stats`、`gold_hourly_active_users`） | `iodp-gold-dev-123456789012` |
| `scripts_bucket_name` | **Glue 脚本桶**：存放所有 Glue Job 的 Python 脚本和 `lib.zip`，Glue 运行时从这里拉取代码 | `iodp-glue-scripts-dev-123456789012` |

桶名拼接了 `${var.environment}` 和 `${var.aws_account_id}` 保证全局唯一（S3 桶名是全球命名空间）。

### DLQ 前缀

`dead_letter_prefix = "dead_letter/"` — DQ 校验不通过的"死信"记录写入 Bronze 桶下的 `dead_letter/` 路径，方便后续用 `dlq_replay` 模块重放。

### FinOps 生命周期策略（控制存储成本）

这三个参数对应 S3 Lifecycle Rule，实现 **热 → 温 → 冷 → 删除** 的数据生命周期管理：

| 参数 | 含义 |
|------|------|
| `ia_transition_days = 30` | 数据写入 **30 天后**自动迁移到 S3 Infrequent Access（IA），存储费降低约 45% |
| `glacier_transition_days = 90` | **90 天后**迁移到 S3 Glacier，存储费再降约 80% |
| `expiration_days = 365` | **365 天后**自动删除，避免无限增长 |

### 下游依赖

`compute`、`dlq_replay`、`replay_jobs`、`opensearch_indexer` 模块都依赖 storage 模块输出的 bucket name 和 ARN。

---

## Glue Streaming Job 参数传递链路

以 `stream_app_logs.py` 为例，Python 代码中通过 `getResolvedOptions` 获取的参数：

```python
args = getResolvedOptions(sys.argv, [
    "JOB_NAME",
    "MSK_BOOTSTRAP_SERVERS",
    "BRONZE_BUCKET",
    "DQ_TABLE",
    "LINEAGE_TABLE",
    "ENVIRONMENT",
])
```

这些参数在 `terraform/modules/compute/main.tf` 的 `aws_glue_job.stream_app_logs` 资源中通过 `default_arguments` 传入：

```hcl
default_arguments = {
  "--MSK_BOOTSTRAP_SERVERS" = var.msk_bootstrap_brokers
  "--BRONZE_BUCKET"         = "s3://${var.bronze_bucket_name}/"
  "--DQ_TABLE"              = var.dq_reports_table_name
  "--LINEAGE_TABLE"         = var.lineage_table_name
  "--ENVIRONMENT"           = var.environment
}
```

### 完整传递链路

```
terraform/environments/dev.tfvars  或  prod.tfvars
    │
    ▼
terraform/main.tf  (根模块)
    │
    ├── module "storage"    → bronze_bucket_name
    ├── module "streaming"  → bootstrap_brokers_sasl_iam
    └── module "dynamodb"   → dq_reports_table_name, lineage_events_table_name
            │
            ▼  (通过 module output 传递)
    module "compute"
        │
        ▼
    aws_glue_job.stream_app_logs  →  default_arguments
        │
        ▼  (Glue 运行时注入 --key=value)
    Python getResolvedOptions()  →  args["KEY"]
```

### 各参数值来源

| Python 参数 | Terraform `default_arguments` key | 值来源 |
|---|---|---|
| `JOB_NAME` | Glue 自动注入（无需显式声明） | 资源 `name = "iodp-stream-app-logs-${var.environment}"` |
| `MSK_BOOTSTRAP_SERVERS` | `--MSK_BOOTSTRAP_SERVERS` | `module.streaming.bootstrap_brokers_sasl_iam` → MSK Serverless 集群 |
| `BRONZE_BUCKET` | `--BRONZE_BUCKET` | `module.storage.bronze_bucket_name` → S3 bucket |
| `DQ_TABLE` | `--DQ_TABLE` | `module.dynamodb.dq_reports_table_name` → DynamoDB 表 |
| `LINEAGE_TABLE` | `--LINEAGE_TABLE` | `module.dynamodb.lineage_events_table_name` → DynamoDB 表 |
| `ENVIRONMENT` | `--ENVIRONMENT` | 根模块 `var.environment`（在 `environments/dev.tfvars` / `prod.tfvars` 中设置） |

`JOB_NAME` 比较特殊：Glue 服务会自动将资源的 `name` 作为 `--JOB_NAME` 注入，不需要在 `default_arguments` 中显式声明。
