# IODP BigData —— Serverless Medallion 数据湖

完全 Serverless 的 AWS 大数据 pipeline：**Firehose Direct PUT → S3 Bronze → Glue Batch → S3 Silver/Gold (Iceberg) → Athena / Agent**。

> **方案 D（FinOps 优化版）**：MSK + Glue Streaming + VPC 全部移除，周费从基线 \$575 降到 ≈ \$15（**省 97%**）。设计决策与权衡见 [EXPLANATION.md](./EXPLANATION.md) 和根目录 [INTERVIEW.md](../INTERVIEW.md)。

---

## 架构总览

```
Producer (boto3 put_record_batch)
    │  NDJSON over HTTPS (IAM SigV4)
    ▼
Kinesis Data Firehose (Direct PUT)        ≈ $1/周
  ├─ iodp-clickstream-{env}               buffer 5 MB / 60s
  └─ iodp-app_logs-{env}                  GZip + dynamic partitioning
    │
    ▼
s3://<bronze>/<stream>/year=…/month=…/day=…/hour=…/*.gz   (NDJSON)
    │
    ▼  Glue Batch (默认手动触发, `make demo-pipeline`)
Silver Iceberg Parquet (列存, DQ + dedup + flatten)
    │
    ▼
Gold Iceberg Parquet (预聚合, 按小时/日)
    │
    ▼  Athena / S3 Vectors / Agent
最终用户 + AI 故障诊断
```

完整数据流图见 [EXPLANATION.md §4](./EXPLANATION.md)。

---

## 部署 —— 5 步走

### 0. 前置条件

```powershell
# 工具
aws --version          # AWS CLI v2
terraform -version     # >= 1.6.0

# 凭证（必须）
$env:AWS_ACCESS_KEY_ID     = "AKIAXXXXXXXXXXXXXXXX"
$env:AWS_SECRET_ACCESS_KEY = "xxxxxxxx"
$env:AWS_REGION            = "ap-southeast-1"   # 资源 region，与 Makefile 默认值一致
```

**编辑 `terraform/environments/dev.tfvars`**（首次部署必填，否则 terraform 会交互式问你）：

| 变量 | 必改 | 说明 |
|---|---|---|
| `aws_account_id` | ✅ | 你的 12 位 AWS 账号 ID（用于 S3 桶命名去重） |
| `alarm_email`    | ✅ | CloudWatch 告警通知邮箱 |
| `aws_region`     | ⬜ | 可不改 — Makefile 会用 `AWS_REGION` 覆盖它 |
| 其余变量         | ⬜ | 默认值已合理，按需调整 |

Makefile 部署时会自动加载 `-var-file=environments/$(ENV).tfvars`，所有变量一次配齐，无需逐个回答提示。

### 1. 上传 Glue 脚本到 S3 (一次性)

```powershell
bash scripts/upload_glue_scripts.sh dev
```

### 2. 部署基础设施 + 建 Iceberg 表

```powershell
make init ENV=dev AWS_REGION=ap-southeast-1
```

这一步等价于：
- `make deploy-infra` —— Terraform apply（Firehose / Glue Jobs / DynamoDB / S3 / IAM）；triggers 默认 OFF
- `make deploy-ddl` —— Athena 执行 5 张 Iceberg 表 DDL（Silver × 2 + Gold × 3）

完成后 Firehose 已经可以接收数据，但 Silver/Gold pipeline 还没跑。

### 3. 灌示例数据到 Firehose

```powershell
make produce-sample ENV=dev COUNT=2000
```

向两个 Firehose stream 各推 2000 条事件。等 ~60s（Firehose flush），到 S3 看 `s3://iodp-bronze-dev-<acct>/clickstream/year=.../*.gz` 应该有文件。

### 4. 手动跑 Medallion 流水线

```powershell
make demo-pipeline ENV=dev
```

按顺序触发：Silver × 2 → 等 5 min → Gold × 3。Athena 控制台查 Silver/Gold 表应该有数据。

### 5. 销毁（演示完）

```powershell
make destroy ENV=dev
```

---

## 常用命令

| 命令 | 作用 |
|---|---|
| `make help`              | 显示完整命令清单 |
| `make produce-sample`    | 向 2 个 Firehose stream 各推 1000 条事件（COUNT 可改） |
| `make produce-clicks`    | 仅推 clickstream |
| `make produce-logs`      | 仅推 app_logs (ERROR_RATE 可改) |
| `make demo-pipeline`     | 手动触发 Silver → 等 5 min → Gold |
| `make demo-silver`       | 仅触发 Silver 2 个 Job |
| `make demo-gold`         | 仅触发 Gold 3 个 Job |
| `make enable-triggers`   | 切到 cron 自动模式（~\$35/周） |
| `make disable-triggers`  | 切回手动模式（默认） |
| `make status`            | 查看 Glue / DynamoDB / Terraform 状态 |
| `make destroy`           | 销毁所有资源 |

---

## 项目结构

```
iodp-bigdata/
├── README.md                 ← 你在这里
├── USAGE.md                  ← 部署细节 + 参数传递链路 + Athena 查询规范
├── EXPLANATION.md            ← 逐模块讲解 + 全流程数据流图
├── Makefile                  ← 部署 + 演示 target
│
├── terraform/                ← IaC（8 个模块）
│   ├── main.tf
│   ├── variables.tf
│   ├── environments/         ← dev.tfvars / prod.tfvars
│   └── modules/
│       ├── storage/          ← S3 Bronze/Silver/Gold/Scripts
│       ├── dynamodb/         ← DQ Reports / Lineage / Threshold Config
│       ├── ingestion/        ← Firehose × 2（方案 D 入口）
│       ├── compute/          ← Glue Catalog + Glue Batch Jobs + Triggers
│       ├── observability/    ← CloudWatch Dashboard + Alarms + SNS
│       ├── dlq_replay/       ← 死信重放 Lambda
│       ├── replay_jobs/      ← 死信重灌 Bronze 的 Glue Job
│       └── vector_indexer/   ← Gold → S3 Vectors（给 Agent 用）
│
├── glue_jobs/
│   ├── batch/                ← Silver × 2 + Gold × 3 (Iceberg)
│   └── lib/                  ← data_quality.py / iceberg_utils.py / lineage.py
│
├── athena/
│   ├── ddl/                  ← Silver/Gold 5 张 Iceberg 表（Bronze 无 DDL）
│   └── views/                ← 给 Agent / BI 的查询视图
│
├── lambda/
│   ├── dlq_replay/           ← 死信搬运
│   └── vector_indexer/       ← Gold → S3 Vectors 索引
│
├── scripts/
│   ├── apply_ddl.sh          ← 用 Athena 执行 ddl/*.sql
│   ├── upload_glue_scripts.sh
│   └── produce_sample_events.py   ← 向 Firehose put_record_batch 演示 producer
│
├── eval/                     ← DQ 黄金 fixtures + reports
└── tests/                    ← 单元测试
```

---

## 关键设计决策（一句话版）

| 决策 | 选择 | 为什么 |
|---|---|---|
| 流式入口 | **Kinesis Data Firehose Direct PUT** | 取代 MSK Serverless，省 \$130/周，无 VPC、无 broker |
| 网络层 | **完全无 VPC** | Firehose / Glue Batch 都是托管公网服务，省 NAT \$10/周 |
| Bronze 格式 | **GZip NDJSON**（schema-on-read） | Producer 抢跑加字段时 raw 数据不丢；schema 维护仍要做但少一层 |
| Silver / Gold 格式 | **Iceberg + Parquet** | 列存 + 分区裁剪 + MERGE 去重，查询走这里 |
| Glue Batch 触发 | **默认手动**（`triggers_enabled=false`） | 演示项目无需 24×7，省 \$35/周；cron 模式可一键切回 |
| DQ 位置 | **Silver Batch Job** 内 | 原 Glue Streaming 已删；DQ 框架（[lib/data_quality.py](glue_jobs/lib/data_quality.py)）原样迁移 |
| 数据契约 | **silver_*.py docstring + produce_sample_events.py** | 不维护独立 schemas/ 目录，契约就近放在使用方代码注释里 |

---

## 周费对比

```
基线（原 MSK + 常驻 Streaming + 全 Iceberg）          ≈ $575 / 周
方案 D（Firehose + Glue Batch 手动 + Bronze NDJSON） ≈ $15  / 周
                                                        ↑ 省 97%
```

成本拆解 + Agent 端的优化（DashScope Qwen 双模型路由 + Prompt Caching）见 `INTERVIEW.md`。

---

## 相关文档

- [USAGE.md](./USAGE.md) —— 部署细节、Athena 查询规范、参数传递链路、跨项目 DynamoDB 链路
- [EXPLANATION.md](./EXPLANATION.md) —— 逐模块讲解 + 全流程数据流图 + 模块职责总结
- [`../INTERVIEW.md`](../INTERVIEW.md) —— 简历讲述用的 STAR 故事
- [`../iodp-agent/`](../iodp-agent/) —— 配套的 LangGraph 多 Agent 故障诊断系统（消费 Gold + RAG）
