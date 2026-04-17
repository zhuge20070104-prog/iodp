# 项目一：企业级实时与离线融合数据流水线

> **定位**：整个统一平台（智能运营数据平台，IODP）的**数据基础设施层**。  
> 本文档描述如何将原始的用户行为与系统日志从 Kafka 摄入，经三层 Medallion 架构清洗、聚合后，沉淀至 S3 数据湖，供 [项目二 Agent 系统](./Agent.md) 及 BI 报表消费。

---

## 目录

1. [统一项目概览与联系](#0-统一项目概览与联系)
2. [整体架构](#1-整体架构)
3. [目录结构](#2-目录结构)
4. [数据模型定义](#3-数据模型定义)
5. [Terraform 模块详解](#4-terraform-模块详解)
6. [PySpark 脚本详解](#5-pyspark-脚本详解)
7. [数据质量框架](#6-数据质量框架)
8. [Medallion 三层架构规格](#7-medallion-三层架构规格)
9. [血缘追踪设计](#8-血缘追踪设计)
10. [监控与告警规格](#9-监控与告警规格)
11. [FinOps 成本控制规格](#10-finops-成本控制规格)
12. [CI/CD 流水线](#11-cicd-流水线)
13. [与 Agent 层的数据接口契约](#12-与-agent-层的数据接口契约)

---

## 0. 统一项目概览与联系

两个项目共同构成 **IODP（Intelligent Operations Data Platform）**：

```
┌──────────────────────────────────────────────────────────────────────┐
│                   IODP 统一平台数据流向                                │
│                                                                      │
│  [用户/系统事件]                                                       │
│       │                                                              │
│       ▼                                                              │
│  ┌─────────────────────────────────────────────────────┐            │
│  │  项目一：数据前处理层 (BigData Pipeline)               │            │
│  │                                                     │            │
│  │  MSK ──► Glue Streaming ──► S3 Bronze               │            │
│  │               │                                     │            │
│  │               ▼                                     │            │
│  │         Glue Batch ─────► S3 Silver/Gold            │            │
│  │               │                                     │            │
│  │               ▼                                     │            │
│  │         Athena Views ◄─── Glue Data Catalog         │            │
│  │               │                 │                   │            │
│  │               ▼                 ▼                   │            │
│  │         DynamoDB (DQ报告)   CloudWatch              │            │
│  └──────────────┬──────────────────────────────────────┘            │
│                 │  数据接口契约 (Athena SQL / S3 Parquet)             │
│                 ▼                                                    │
│  ┌─────────────────────────────────────────────────────┐            │
│  │  项目二：智能 Agent 后处理层 (Multi-Agent System)      │            │
│  │                                                     │            │
│  │  API GW ──► Lambda/Fargate ──► LangGraph Graph      │            │
│  │                                    │                │            │
│  │              ┌─────────────────────┤                │            │
│  │              │                     │                │            │
│  │         Log Analyzer          RAG Agent             │            │
│  │         (查 Athena/S3)      (查 OpenSearch)         │            │
│  │              │                     │                │            │
│  │              └──────────┬──────────┘                │            │
│  │                         ▼                           │            │
│  │                  Synthesizer Agent                  │            │
│  │                  (输出用户回复 + Bug报告)              │            │
│  └─────────────────────────────────────────────────────┘            │
└──────────────────────────────────────────────────────────────────────┘
```

**关键集成点**：
| 接口 | 提供方（项目一） | 消费方（项目二） | 格式 |
|------|---------------|---------------|------|
| Gold 层错误日志宽表 | Glue Batch Job | Log Analyzer Agent 的 Athena 工具 | Parquet / Iceberg on S3 |
| DQ 告警记录 | DynamoDB `iodp_dq_reports_{env}` | Log Analyzer Agent 的 DynamoDB 工具 | JSON |
| 历史故障工单摘要 | Glue Gold Job 输出 | RAG Agent 的 OpenSearch 索引数据源 | JSON Lines → 向量化 |
| Glue Catalog 表元数据 | Glue Data Catalog | Agent 的 Athena 工具（获取 Schema） | Catalog API |

---

## 1. 整体架构

### 1.1 架构图（详细）

```
                         ┌──────────────────────────┐
  用户点击行为            │   Amazon MSK Serverless   │
  ──────────────────────►│  Topic: user_clickstream  │
                         │  Topic: system_app_logs   │
  系统应用日志            └────────────┬─────────────┘
  ──────────────────────►             │
                                      │ Kafka Consumer (IAM Auth)
                         ┌────────────▼─────────────┐
                         │  AWS Glue Streaming Job   │
                         │  (PySpark Structured      │
                         │   Streaming)              │
                         │  - Schema 校验            │
                         │  - 数据质量检查            │
                         │  - 死信路由               │
                         └────────────┬─────────────┘
                          ┌───────────┴──────────────┐
                          │                          │
               ┌──────────▼──────────┐  ┌───────────▼───────────┐
               │  S3 Bronze Bucket   │  │  S3 Dead Letter Zone  │
               │  /clickstream/      │  │  /dead_letter/        │
               │  /app_logs/         │  │  (脏数据隔离)          │
               │  (原始 Iceberg 表)  │  └───────────────────────┘
               └──────────┬──────────┘
                          │ 每小时 Glue Batch Job
               ┌──────────▼──────────┐
               │  S3 Silver Bucket   │
               │  /enriched_clicks/  │
               │  /parsed_logs/      │
               │  (去重、补维度)      │
               └──────────┬──────────┘
                          │ 每小时 / 每天 Glue Batch Job
               ┌──────────▼──────────┐
               │  S3 Gold Bucket     │
               │  /hourly_active_    │
               │   users/            │
               │  /api_error_stats/  │
               │  /incident_summary/ │ ◄──── 供 Agent RAG 消费
               └──────────┬──────────┘
                          │
               ┌──────────▼──────────┐
               │   Amazon Athena     │ ◄──── 供 Log Analyzer Agent 查询
               │  (Iceberg Catalog)  │
               └─────────────────────┘

  ┌────────────────────────┐     ┌────────────────────────┐
  │  AWS Glue Data Catalog │     │  Amazon DynamoDB        │
  │  (Schema Registry /    │     │  Table: iodp_dq_reports_{env}      │
  │   Iceberg Catalog)     │     │  Table: iodp_lineage_events_{env}  │ ◄── Agent 消费
  └────────────────────────┘     └────────────────────────┘

  ┌────────────────────────┐
  │  Amazon CloudWatch     │
  │  - MSK Consumer Lag    │
  │  - Glue Job Metrics    │
  │  - DQ 失败告警 SNS     │
  └────────────────────────┘
```

### 1.2 AWS 服务清单与选型依据

| 服务 | 用途 | 选型依据 |
|------|------|---------|
| MSK Serverless | 消息队列接入 | 免运维，按使用量计费，适合流量波动大的场景 |
| Glue Streaming | 实时消费 Kafka | 原生支持结构化流，集成 Glue Catalog，无需管理 Spark 集群 |
| Glue ETL Batch | 离线批处理 | 与 Catalog 集成，支持 PySpark，自动扩缩容 |
| S3 + Iceberg | 数据湖存储 | Iceberg 支持 ACID、时间旅行，配合 Athena 可做 DML |
| Athena | Ad-hoc 查询 | Serverless SQL，与 Iceberg 深度集成 |
| Glue Data Catalog | 元数据管理 | 统一 Schema，与 Athena/Glue/EMR 原生集成 |
| DynamoDB | DQ 报告 + 血缘存储 | 单毫秒延迟，Serverless，适合告警写入 |
| CloudWatch | 监控告警 | 原生 AWS 集成，支持自定义 Metric |

---

## 2. 目录结构

```
iodp-bigdata/
├── terraform/
│   ├── main.tf                        # 根模块，调用各子模块
│   ├── variables.tf                   # 全局变量（environment, cost_center, region 等）
│   ├── outputs.tf                     # 模块输出（bucket ARN、MSK endpoint 等）
│   ├── backend.tf                     # S3 远程 State + DynamoDB 锁
│   ├── locals.tf                      # 公共标签定义（FinOps 强制标签）
│   │
│   ├── modules/
│   │   ├── networking/                # VPC、子网、安全组
│   │   │   ├── main.tf
│   │   │   ├── variables.tf
│   │   │   └── outputs.tf
│   │   │
│   │   ├── storage/                   # S3 存储桶 + 生命周期策略
│   │   │   ├── main.tf
│   │   │   ├── variables.tf
│   │   │   └── outputs.tf
│   │   │
│   │   ├── streaming/                 # MSK Serverless 集群
│   │   │   ├── main.tf
│   │   │   ├── variables.tf
│   │   │   └── outputs.tf
│   │   │
│   │   ├── compute/                   # Glue Jobs（7个）、Glue Catalog、IAM Roles
│   │   │   ├── main.tf
│   │   │   ├── variables.tf
│   │   │   └── outputs.tf
│   │   │
│   │   ├── observability/             # CloudWatch Dashboards、Alarms、SNS
│   │   │   ├── main.tf
│   │   │   ├── variables.tf
│   │   │   └── outputs.tf
│   │   │
│   │   ├── dynamodb/                  # DQ 报告表、血缘事件表、阈值配置表
│   │   │   ├── main.tf
│   │   │   ├── variables.tf
│   │   │   └── outputs.tf
│   │   │
│   │   ├── dlq_replay/               # DLQ 死信数据重处理 Lambda
│   │   │   ├── main.tf
│   │   │   ├── variables.tf
│   │   │   └── outputs.tf
│   │   │
│   │   ├── opensearch_indexer/        # S3 事件驱动 OpenSearch 索引 Lambda
│   │   │   ├── main.tf
│   │   │   ├── variables.tf
│   │   │   └── outputs.tf
│   │   │
│   │   └── replay_jobs/              # DLQ 死信重灌 Bronze 的 Glue Batch Jobs
│   │       ├── main.tf
│   │       ├── variables.tf
│   │       └── outputs.tf
│   │
│   └── environments/
│       ├── dev.tfvars
│       └── prod.tfvars
│
├── glue_jobs/
│   ├── streaming/
│   │   ├── stream_clickstream.py      # Glue Streaming：消费 user_clickstream
│   │   └── stream_app_logs.py         # Glue Streaming：消费 system_app_logs
│   │
│   ├── batch/
│   │   ├── silver_enrich_clicks.py    # Bronze → Silver：点击流去重 + 补维度
│   │   ├── silver_parse_logs.py       # Bronze → Silver：日志解析 + 嵌套 JSON 展开
│   │   ├── gold_hourly_active_users.py # Silver → Gold：每小时活跃用户
│   │   ├── gold_api_error_stats.py     # Silver → Gold：API 错误率聚合
│   │   ├── gold_incident_summary.py    # Gold：生成故障工单摘要（供 Agent RAG）
│   │   ├── replay_clickstream_to_bronze.py  # DLQ 重灌：点击流死信 → Bronze
│   │   └── replay_app_logs_to_bronze.py     # DLQ 重灌：日志死信 → Bronze
│   │
│   └── lib/
│       ├── data_quality.py            # 公共 DQ 校验框架（可复用）
│       ├── lineage.py                 # 血缘事件写入工具函数
│       ├── iceberg_utils.py           # Iceberg 读写工具
│       └── schema_definitions.py     # 所有 Schema 的集中定义
│
├── lambda/
│   ├── dlq_replay/
│   │   └── handler.py                # DLQ 重处理 Lambda（读 dead_letter/ 写 replay/）
│   └── opensearch_indexer/
│       ├── handler.py                # S3 事件 → OpenSearch 向量索引 Lambda
│       └── requirements.txt          # opensearch-py、boto3 等依赖
│
├── schemas/
│   ├── user_clickstream.json          # Kafka Topic Schema（JSON Schema）
│   └── system_app_logs.json           # Kafka Topic Schema（JSON Schema）
│
├── athena/
│   ├── views/
│   │   ├── v_error_log_enriched.sql   # 供 Agent 查询的错误日志视图
│   │   └── v_user_session.sql         # 用户会话聚合视图
│   └── ddl/
│       ├── bronze_clickstream.sql
│       ├── bronze_app_logs.sql
│       ├── silver_enriched_clicks.sql
│       ├── silver_parsed_logs.sql
│       ├── gold_api_error_stats.sql
│       ├── gold_hourly_active_users.sql
│       └── gold_incident_summary.sql
│
├── tests/
│   └── unit/
│       ├── conftest.py
│       └── test_data_quality.py
│
├── scripts/
│   ├── bootstrap_kafka_topics.sh      # 创建 MSK Topic 的初始化脚本
│   └── upload_glue_scripts.sh         # 将 Glue 脚本上传到 S3
│
└── README.md
```

---

## 3. 数据模型定义

### 3.1 Kafka Topic Schema

#### `user_clickstream` — 用户点击流

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "UserClickstream",
  "type": "object",
  "required": ["event_id", "user_id", "session_id", "event_type", "event_timestamp", "page_url"],
  "properties": {
    "event_id":         { "type": "string", "format": "uuid" },
    "user_id":          { "type": "string", "minLength": 1 },
    "session_id":       { "type": "string" },
    "event_type":       { "type": "string", "enum": ["click", "view", "scroll", "purchase", "add_to_cart", "checkout"] },
    "event_timestamp":  { "type": "string", "format": "date-time" },
    "page_url":         { "type": "string", "format": "uri" },
    "referrer_url":     { "type": ["string", "null"] },
    "device_info": {
      "type": "object",
      "properties": {
        "device_type":  { "type": "string", "enum": ["mobile", "desktop", "tablet"] },
        "os":           { "type": "string" },
        "browser":      { "type": "string" },
        "screen_resolution": { "type": "string" }
      }
    },
    "geo_info": {
      "type": "object",
      "properties": {
        "country_code": { "type": "string", "pattern": "^[A-Z]{2}$" },
        "city":         { "type": ["string", "null"] },
        "ip_hash":      { "type": "string" }
      }
    },
    "properties": {
      "type": "object",
      "description": "业务自定义字段，如商品 ID、金额等"
    }
  }
}
```

#### `system_app_logs` — 系统应用日志

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "SystemAppLog",
  "type": "object",
  "required": ["log_id", "service_name", "log_level", "event_timestamp", "message"],
  "properties": {
    "log_id":           { "type": "string", "format": "uuid" },
    "trace_id":         { "type": ["string", "null"] },
    "span_id":          { "type": ["string", "null"] },
    "service_name":     { "type": "string" },
    "instance_id":      { "type": "string" },
    "log_level":        { "type": "string", "enum": ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"] },
    "event_timestamp":  { "type": "string", "format": "date-time" },
    "message":          { "type": "string" },
    "error_details": {
      "type": ["object", "null"],
      "properties": {
        "error_code":   { "type": "string" },
        "error_type":   { "type": "string" },
        "stack_trace":  { "type": ["string", "null"] },
        "http_status":  { "type": ["integer", "null"] }
      }
    },
    "request_info": {
      "type": ["object", "null"],
      "properties": {
        "method":       { "type": "string" },
        "path":         { "type": "string" },
        "user_id":      { "type": ["string", "null"] },
        "duration_ms":  { "type": "number" }
      }
    },
    "environment":      { "type": "string", "enum": ["prod", "staging", "dev"] }
  }
}
```

### 3.2 S3 分区键设计

| 层级 | 路径模式 | 分区键 | 说明 |
|------|---------|--------|------|
| Bronze | `s3://iodp-bronze-{env}/clickstream/event_date=YYYY-MM-DD/event_hour=HH/` | date + hour | 与流式写入时间对齐 |
| Bronze | `s3://iodp-bronze-{env}/app_logs/service_name=xxx/log_level=ERROR/date=YYYY-MM-DD/` | service + level + date | 按服务和级别分区便于 Agent 查询 |
| Silver | `s3://iodp-silver-{env}/enriched_clicks/event_date=YYYY-MM-DD/event_type=xxx/` | date + event_type | 支持按业务类型过滤 |
| Gold | `s3://iodp-gold-{env}/api_error_stats/stat_date=YYYY-MM-DD/stat_hour=HH/` | date + hour | 与 Agent 告警时间窗口对齐 |

### 3.3 DynamoDB 表 Schema

#### `dq_reports` — 数据质量告警报告

```
PK:   table_name (String)           例: "bronze_clickstream"
SK:   report_timestamp (String)     例: "2026-04-06T14:30:00Z"

Attributes:
  job_run_id          String   Glue 作业运行 ID
  batch_id            String   微批次 ID
  error_type          String   例: "NULL_USER_ID" | "TIMESTAMP_OUT_OF_RANGE" | "INVALID_ERROR_CODE"
  total_records       Number   本批次总记录数
  failed_records      Number   校验失败的记录数
  failure_rate        Number   失败率（0.0 ~ 1.0）
  threshold_breached  Boolean  是否超过 5% 阈值
  dead_letter_path    String   死信数据在 S3 的路径
  environment         String   "prod" | "dev"
  TTL                 Number   Unix 时间戳（90天后自动删除，FinOps）
```

#### `lineage_events` — 数据血缘事件

```
PK:   source_table  (String)   例: "msk::user_clickstream"
SK:   event_time    (String)   例: "2026-04-06T14:00:00Z#bronze_clickstream"

Attributes:
  target_table      String   例: "s3://bronze/clickstream/"
  transformation    String   例: "SCHEMA_VALIDATION + JSON_FLATTEN"
  job_name          String   Glue Job 名称
  job_run_id        String
  record_count_in   Number
  record_count_out  Number
  record_count_dl   Number   死信数量
  duration_seconds  Number
  TTL               Number   180 天后自动删除
```

---

## 4. Terraform 模块详解

### 4.1 根模块 (`terraform/main.tf`)

```hcl
# terraform/main.tf

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  backend "s3" {
    bucket         = "iodp-terraform-state-prod"
    key            = "bigdata/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "iodp-terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.mandatory_tags
  }
}

# ─────────── 模块调用 ───────────

module "networking" {
  source = "./modules/networking"

  environment        = var.environment
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
  tags               = local.mandatory_tags
}

module "storage" {
  source = "./modules/storage"

  environment           = var.environment
  bronze_bucket_name    = "iodp-bronze-${var.environment}-${var.aws_account_id}"
  silver_bucket_name    = "iodp-silver-${var.environment}-${var.aws_account_id}"
  gold_bucket_name      = "iodp-gold-${var.environment}-${var.aws_account_id}"
  scripts_bucket_name   = "iodp-glue-scripts-${var.environment}-${var.aws_account_id}"
  dead_letter_prefix    = "dead_letter/"
  # FinOps 生命周期
  ia_transition_days    = 30
  glacier_transition_days = 90
  expiration_days       = 365
  tags                  = local.mandatory_tags
}

module "streaming" {
  source = "./modules/streaming"

  environment         = var.environment
  cluster_name        = "iodp-msk-${var.environment}"
  subnet_ids          = module.networking.private_subnet_ids
  security_group_ids  = [module.networking.msk_sg_id]
  kafka_topics        = var.kafka_topics
  tags                = local.mandatory_tags
}

module "dynamodb" {
  source = "./modules/dynamodb"

  environment = var.environment
  tags        = local.mandatory_tags
}

module "compute" {
  source = "./modules/compute"

  environment          = var.environment
  bronze_bucket_arn    = module.storage.bronze_bucket_arn
  silver_bucket_arn    = module.storage.silver_bucket_arn
  gold_bucket_arn      = module.storage.gold_bucket_arn
  scripts_bucket_name  = module.storage.scripts_bucket_name
  msk_cluster_arn      = module.streaming.msk_cluster_arn
  msk_bootstrap_brokers = module.streaming.bootstrap_brokers_sasl_iam
  dq_reports_table_arn = module.dynamodb.dq_reports_table_arn
  lineage_table_arn    = module.dynamodb.lineage_events_table_arn
  glue_catalog_id      = data.aws_caller_identity.current.account_id
  tags                 = local.mandatory_tags
}

module "observability" {
  source = "./modules/observability"

  environment          = var.environment
  msk_cluster_name     = module.streaming.msk_cluster_name
  glue_job_names       = module.compute.glue_job_names
  dq_reports_table_name = module.dynamodb.dq_reports_table_name
  alarm_email          = var.alarm_email
  tags                 = local.mandatory_tags
}

data "aws_caller_identity" "current" {}
```

### 4.2 公共标签 (`terraform/locals.tf`)

```hcl
# terraform/locals.tf
# FinOps 强制标签：所有资源必须携带，否则 Terraform plan 会因 variable validation 失败

locals {
  mandatory_tags = {
    Environment    = var.environment        # "prod" | "dev" | "staging"
    CostCenter     = var.cost_center        # 例: "engineering-data-platform"
    Project        = "IODP-BigData"
    ManagedBy      = "Terraform"
    Owner          = var.team_owner         # 例: "data-engineering@company.com"
    DataClass      = "internal"             # 数据分级合规标签
    CreatedDate    = formatdate("YYYY-MM-DD", timestamp())  # 用于成本追踪
  }
}
```

### 4.3 存储模块 (`terraform/modules/storage/main.tf`)

```hcl
# terraform/modules/storage/main.tf

# ─── Bronze Bucket ───
resource "aws_s3_bucket" "bronze" {
  bucket = var.bronze_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "bronze" {
  bucket = aws_s3_bucket.bronze.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "bronze" {
  bucket = aws_s3_bucket.bronze.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true   # FinOps: 降低 KMS API 调用成本
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "bronze" {
  bucket = aws_s3_bucket.bronze.id

  # FinOps: 普通对象生命周期
  rule {
    id     = "bronze-standard-lifecycle"
    status = "Enabled"
    filter { prefix = "" }

    transition {
      days          = var.ia_transition_days      # 30 天 → Standard-IA
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = var.glacier_transition_days # 90 天 → Glacier Instant Retrieval
      storage_class = "GLACIER_IR"
    }
    expiration {
      days = var.expiration_days                  # 365 天后删除（可按需调整）
    }
    noncurrent_version_expiration {
      noncurrent_days = 30                        # 非当前版本 30 天后删除
    }
  }

  # FinOps: 死信区数据保留时间更短
  rule {
    id     = "dead-letter-lifecycle"
    status = "Enabled"
    filter { prefix = var.dead_letter_prefix }
    expiration { days = 30 }
  }
}

resource "aws_s3_bucket_public_access_block" "bronze" {
  bucket                  = aws_s3_bucket.bronze.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ─── Silver / Gold Bucket（结构相同，省略重复注释）───
resource "aws_s3_bucket" "silver" {
  bucket = var.silver_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_lifecycle_configuration" "silver" {
  bucket = aws_s3_bucket.silver.id
  rule {
    id     = "silver-lifecycle"
    status = "Enabled"
    filter { prefix = "" }
    transition { days = 60;  storage_class = "STANDARD_IA"  }
    transition { days = 180; storage_class = "GLACIER_IR"   }
    expiration { days = 730 }
  }
}

resource "aws_s3_bucket" "gold" {
  bucket = var.gold_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_lifecycle_configuration" "gold" {
  bucket = aws_s3_bucket.gold.id
  rule {
    id     = "gold-lifecycle"
    status = "Enabled"
    filter { prefix = "" }
    # Gold 层数据访问频率较高，延迟 IA 转换
    transition { days = 90;  storage_class = "STANDARD_IA"  }
    transition { days = 270; storage_class = "GLACIER_IR"   }
    expiration { days = 1095 }   # 3年（业务合规要求）
  }
}

# ─── Glue 脚本 Bucket（不需要生命周期，只需小存储）───
resource "aws_s3_bucket" "scripts" {
  bucket = var.scripts_bucket_name
  tags   = var.tags
}
```

### 4.4 MSK Serverless 模块 (`terraform/modules/streaming/main.tf`)

```hcl
# terraform/modules/streaming/main.tf

resource "aws_msk_serverless_cluster" "main" {
  cluster_name = var.cluster_name

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

  client_authentication {
    sasl {
      iam { enabled = true }   # 使用 IAM 认证，无需管理密码
    }
  }

  tags = var.tags
}

# ─── Kafka Topics（通过 Glue Connection 或 bootstrap 脚本创建）───
# MSK Serverless 不直接支持 aws_msk_topic 资源，使用 null_resource + bootstrap 脚本
resource "null_resource" "create_topics" {
  depends_on = [aws_msk_serverless_cluster.main]

  triggers = {
    topics = join(",", [for t in var.kafka_topics : t.name])
  }

  provisioner "local-exec" {
    command = <<-EOT
      bash ${path.module}/../../scripts/bootstrap_kafka_topics.sh \
        --bootstrap-servers "${aws_msk_serverless_cluster.main.bootstrap_brokers_sasl_iam}" \
        --topics '${jsonencode(var.kafka_topics)}'
    EOT
  }
}

output "msk_cluster_arn" {
  value = aws_msk_serverless_cluster.main.arn
}

output "bootstrap_brokers_sasl_iam" {
  value     = aws_msk_serverless_cluster.main.bootstrap_brokers_sasl_iam
  sensitive = true
}

output "msk_cluster_name" {
  value = aws_msk_serverless_cluster.main.cluster_name
}
```

### 4.5 计算模块 — Glue Jobs (`terraform/modules/compute/main.tf`)

```hcl
# terraform/modules/compute/main.tf

# ─── Glue IAM Role ───
resource "aws_iam_role" "glue_execution" {
  name = "iodp-glue-execution-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "glue_execution_policy" {
  name = "iodp-glue-policy-${var.environment}"
  role = aws_iam_role.glue_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # S3 读写权限（最小权限原则）
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [
          var.bronze_bucket_arn, "${var.bronze_bucket_arn}/*",
          var.silver_bucket_arn, "${var.silver_bucket_arn}/*",
          var.gold_bucket_arn,   "${var.gold_bucket_arn}/*",
          "arn:aws:s3:::${var.scripts_bucket_name}",
          "arn:aws:s3:::${var.scripts_bucket_name}/*"
        ]
      },
      {
        # MSK IAM 认证
        Effect   = "Allow"
        Action   = ["kafka-cluster:Connect", "kafka-cluster:ReadGroup",
                    "kafka-cluster:DescribeGroup", "kafka-cluster:AlterGroup",
                    "kafka-cluster:DescribeTopic", "kafka-cluster:ReadData",
                    "kafka:GetBootstrapBrokers", "kafka:DescribeClusterV2"]
        Resource = [var.msk_cluster_arn, "${var.msk_cluster_arn}/*"]
      },
      {
        # DynamoDB 写入 DQ 报告和血缘事件
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query"]
        Resource = [var.dq_reports_table_arn, var.lineage_table_arn]
      },
      {
        # Glue Catalog 读写
        Effect   = "Allow"
        Action   = ["glue:GetDatabase", "glue:GetTable", "glue:CreateTable",
                    "glue:UpdateTable", "glue:GetPartitions", "glue:BatchCreatePartition"]
        Resource = ["arn:aws:glue:*:${var.glue_catalog_id}:catalog",
                    "arn:aws:glue:*:${var.glue_catalog_id}:database/*",
                    "arn:aws:glue:*:${var.glue_catalog_id}:table/*/*"]
      },
      {
        # CloudWatch Logs（Job 日志输出）
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:log-group:/aws-glue/*"
      }
    ]
  })
}

# ─── Glue Data Catalog Database ───
resource "aws_glue_catalog_database" "bronze" {
  name        = "iodp_bronze_${var.environment}"
  description = "IODP Bronze Layer - Raw ingested data"
}

resource "aws_glue_catalog_database" "silver" {
  name        = "iodp_silver_${var.environment}"
  description = "IODP Silver Layer - Cleaned and enriched data"
}

resource "aws_glue_catalog_database" "gold" {
  name        = "iodp_gold_${var.environment}"
  description = "IODP Gold Layer - Business aggregates, consumed by Agent"
}

# ─── Glue Streaming Job（点击流）───
resource "aws_glue_job" "stream_clickstream" {
  name     = "iodp-stream-clickstream-${var.environment}"
  role_arn = aws_iam_role.glue_execution.arn

  command {
    name            = "gluestreaming"
    script_location = "s3://${var.scripts_bucket_name}/streaming/stream_clickstream.py"
    python_version  = "3"
  }

  glue_version      = "4.0"
  number_of_workers = 2       # FinOps: 最小化初始 Worker 数
  worker_type       = "G.1X"  # FinOps: G.1X 适合流式小作业

  default_arguments = {
    "--enable-auto-scaling"              = "true"   # FinOps: 自动扩缩容
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--enable-spark-ui"                  = "true"
    "--TempDir"                          = "s3://${var.scripts_bucket_name}/tmp/"
    "--job-bookmark-option"              = "job-bookmark-enable"
    "--MSK_BOOTSTRAP_SERVERS"            = var.msk_bootstrap_brokers
    "--BRONZE_BUCKET"                    = "s3://${var.bronze_bucket_name}/"
    "--DQ_TABLE"                         = "iodp_dq_reports_${var.environment}"
    "--LINEAGE_TABLE"                    = "iodp_lineage_events_${var.environment}"
    "--ENVIRONMENT"                      = var.environment
  }

  execution_property {
    max_concurrent_runs = 1
  }

  tags = var.tags
}

# ─── Glue Batch Jobs ───

resource "aws_glue_job" "gold_api_error_stats" {
  name     = "iodp-gold-api-error-stats-${var.environment}"
  role_arn = aws_iam_role.glue_execution.arn

  command {
    name            = "glueetl"
    script_location = "s3://${var.scripts_bucket_name}/batch/gold_api_error_stats.py"
    python_version  = "3"
  }

  glue_version      = "4.0"
  number_of_workers = 2
  worker_type       = "G.1X"

  default_arguments = {
    "--enable-auto-scaling"              = "true"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--job-bookmark-option"              = "job-bookmark-enable"
    "--SILVER_BUCKET"                    = "s3://${var.silver_bucket_name}/"
    "--GOLD_BUCKET"                      = "s3://${var.gold_bucket_name}/"
    "--ENVIRONMENT"                      = var.environment
    "--GLUE_DATABASE_SILVER"             = "iodp_silver_${var.environment}"
    "--GLUE_DATABASE_GOLD"               = "iodp_gold_${var.environment}"
  }

  tags = var.tags
}

# ─── Glue Triggers（定时调度）───

resource "aws_glue_trigger" "gold_hourly" {
  name     = "iodp-gold-hourly-trigger-${var.environment}"
  type     = "SCHEDULED"
  schedule = "cron(0 * * * ? *)"   # 每小时整点运行

  actions {
    job_name = aws_glue_job.gold_api_error_stats.name
    arguments = {
      "--PROCESSING_HOUR" = "--"  # 由 Job 自动计算上一小时
    }
  }

  tags = var.tags
}

output "glue_job_names" {
  value = [
    aws_glue_job.stream_clickstream.name,
    aws_glue_job.gold_api_error_stats.name,
  ]
}
```

### 4.6 监控模块 (`terraform/modules/observability/main.tf`)

```hcl
# terraform/modules/observability/main.tf

# ─── SNS Topic（告警通知）───
resource "aws_sns_topic" "alerts" {
  name = "iodp-data-alerts-${var.environment}"
  tags = var.tags
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# ─── MSK Consumer Lag 告警 ───
resource "aws_cloudwatch_metric_alarm" "msk_consumer_lag" {
  for_each = toset(["user_clickstream", "system_app_logs"])

  alarm_name          = "iodp-msk-${each.key}-lag-${var.environment}"
  alarm_description   = "MSK consumer lag for ${each.key} exceeds threshold"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "EstimatedMaxTimeLag"
  namespace           = "AWS/Kafka"
  period              = 300   # 5 分钟
  statistic           = "Maximum"
  threshold           = 300   # 超过 5 分钟积压则告警

  dimensions = {
    Cluster  = var.msk_cluster_name
    Topic    = each.key
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
  tags          = var.tags
}

# ─── Glue Job 失败告警 ───
resource "aws_cloudwatch_metric_alarm" "glue_job_failure" {
  for_each = toset(var.glue_job_names)

  alarm_name          = "iodp-glue-failure-${each.key}"
  alarm_description   = "Glue job ${each.key} failed"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "glue.driver.aggregate.numFailedTasks"
  namespace           = "Glue"
  period              = 300
  statistic           = "Sum"
  threshold           = 1

  dimensions = {
    JobName = each.key
    JobRunId = "ALL"
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  tags          = var.tags
}

# ─── CloudWatch Dashboard ───
resource "aws_cloudwatch_dashboard" "iodp" {
  dashboard_name = "IODP-BigData-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric"
        properties = {
          title  = "MSK Consumer Lag"
          metrics = [
            for topic in ["user_clickstream", "system_app_logs"] : [
              "AWS/Kafka", "EstimatedMaxTimeLag",
              "Cluster", var.msk_cluster_name, "Topic", topic
            ]
          ]
          period = 300
          view   = "timeSeries"
        }
      },
      {
        type = "metric"
        properties = {
          title   = "Glue Job Duration"
          metrics = [
            for job in var.glue_job_names : [
              "Glue", "glue.driver.ExecutorRunTime", "JobName", job
            ]
          ]
          period = 300
          view   = "timeSeries"
        }
      }
    ]
  })
}
```

---

## 5. PySpark 脚本详解

### 5.1 公共库 — 数据质量框架 (`glue_jobs/lib/data_quality.py`)

```python
# glue_jobs/lib/data_quality.py
"""
公共数据质量校验框架
- 支持规则链式注册
- 自动统计失败率
- 超阈值时路由死信队列 + 写入 DynamoDB 告警
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

import boto3
from pyspark.sql import DataFrame
from pyspark.sql.functions import col, count, lit, when

logger = logging.getLogger(__name__)

# ─── 合法的 error_code 字典（从配置中心加载，此处硬编码示例）───
VALID_ERROR_CODES = {
    "E1001", "E1002", "E1003",   # 认证错误
    "E2001", "E2002", "E2003",   # 支付错误
    "E3001", "E3002",            # 网关超时
    "E4001", "E4002", "E4003",   # 业务逻辑错误
}


@dataclass
class DQRule:
    """单条数据质量规则"""
    name: str
    check_fn: Callable[[DataFrame], DataFrame]  # 返回包含 is_valid 布尔列的 DataFrame
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
    数据质量检查器
    使用方式:
        checker = DataQualityChecker(table_name="bronze_clickstream", ...)
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
        failure_threshold: float = 0.05,
        environment: str = "prod",
        aws_region: str = "us-east-1",
    ):
        self.table_name = table_name
        self.batch_id = batch_id
        self.job_run_id = job_run_id
        self.dead_letter_base_path = dead_letter_base_path
        self.dq_table_name = dq_table_name
        self.failure_threshold = failure_threshold
        self.environment = environment
        self._rules: List[DQRule] = []
        self._dynamodb = boto3.resource("dynamodb", region_name=aws_region)

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
            # 每个规则添加一列 _dq_{rule.name}，True=通过，False=失败
            validated_df = rule.check_fn(validated_df)

        # 合并所有规则：所有规则均通过才算 valid
        rule_cols = [f"_dq_{rule.name}" for rule in self._rules]
        all_valid_expr = " AND ".join([f"`{c}` = true" for c in rule_cols])
        any_invalid_expr = " OR ".join([f"`{c}` = false" for c in rule_cols])

        valid_df = validated_df.filter(all_valid_expr).drop(*rule_cols)
        invalid_df = validated_df.filter(any_invalid_expr)

        total = df.count()
        failed = invalid_df.count()
        failure_rate = failed / total if total > 0 else 0.0
        threshold_breached = failure_rate > self.failure_threshold

        # 聚合失败类型（取第一个失败的规则名）
        dominant_error_type = "UNKNOWN"
        for rule in self._rules:
            col_name = f"_dq_{rule.name}"
            rule_fail_count = invalid_df.filter(f"`{col_name}` = false").count()
            if rule_fail_count > 0:
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
            # 写入死信区（移除 DQ 标记列，只保留原始数据 + 错误说明）
            dead_letter_df = invalid_df.drop(*rule_cols).withColumn(
                "_dq_error_type", lit(dominant_error_type)
            )
            dead_letter_df.write.mode("overwrite").parquet(dead_letter_path)
            logger.warning(
                "DQ threshold breached: %.2f%% failure rate for %s. "
                "Dead letter written to: %s",
                failure_rate * 100,
                self.table_name,
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
            ttl_seconds = int(datetime.now(timezone.utc).timestamp()) + 90 * 86400  # 90 天 TTL

            table.put_item(Item={
                "table_name":         result.table_name,
                "report_timestamp":   now_iso,
                "job_run_id":         result.job_run_id,
                "batch_id":           result.batch_id,
                "error_type":         result.error_type,
                "total_records":      result.total_records,
                "failed_records":     result.failed_records,
                # DynamoDB 不支持 float，使用 Decimal 或 str
                "failure_rate":       str(result.failure_rate),
                "threshold_breached": result.threshold_breached,
                "dead_letter_path":   result.dead_letter_path or "",
                "environment":        result.environment,
                "TTL":                ttl_seconds,
            })
        except Exception as e:
            # DQ 报告写入失败不应中断主流程
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
    from pyspark.sql.functions import current_timestamp, to_timestamp, abs as spark_abs
    from pyspark.sql.functions import unix_timestamp

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
    """字段值必须在字典范围内"""
    name = rule_name or f"in_set_{column}"
    valid_list = list(valid_values)
    def check(df: DataFrame) -> DataFrame:
        return df.withColumn(f"_dq_{name}", col(column).isin(valid_list))
    return DQRule(
        name=name,
        check_fn=check,
        error_type=f"INVALID_{column.upper()}",
        description=f"Column '{column}' must be in {valid_list[:5]}...",
    )
```

### 5.2 Glue Streaming Job — 消费 `system_app_logs` (`glue_jobs/streaming/stream_app_logs.py`)

```python
# glue_jobs/streaming/stream_app_logs.py
"""
Glue Streaming Job: MSK system_app_logs → S3 Bronze (Apache Iceberg)
架构决策:
  - 使用 Structured Streaming，每 60 秒一个触发器（mini-batch）
  - Schema 校验使用公共 DQ 框架，失败数据路由死信区
  - 写入 Iceberg 格式，支持 ACID 和时间旅行
  - DQ 结果写入 DynamoDB，供 Agent 的 Log Analyzer 工具查询
"""

import sys
import uuid
from datetime import datetime, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, from_json, get_json_object,
    lit, to_timestamp, when
)
from pyspark.sql.types import (
    IntegerType, StringType, StructField, StructType
)

# 导入公共库（上传到 S3，通过 Glue --extra-py-files 引入）
from lib.data_quality import (
    DataQualityChecker, VALID_ERROR_CODES,
    rule_not_null, rule_timestamp_in_range, rule_in_set,
)
from lib.lineage import write_lineage_event
from lib.iceberg_utils import ensure_iceberg_table

# ─── 解析 Glue Job 参数 ───
args = getResolvedOptions(sys.argv, [
    "JOB_NAME",
    "MSK_BOOTSTRAP_SERVERS",
    "BRONZE_BUCKET",
    "DQ_TABLE",
    "LINEAGE_TABLE",
    "ENVIRONMENT",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

BRONZE_PATH  = f"{args['BRONZE_BUCKET']}app_logs/"
DEAD_LETTER  = f"{args['BRONZE_BUCKET']}dead_letter/"
ENVIRONMENT  = args["ENVIRONMENT"]
JOB_RUN_ID   = args.get("JOB_RUN_ID", str(uuid.uuid4()))

# ─── Spark 配置：启用 Iceberg ───
spark.conf.set("spark.sql.extensions",
    "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
spark.conf.set("spark.sql.catalog.glue_catalog",
    "org.apache.iceberg.aws.glue.GlueCatalog")
spark.conf.set("spark.sql.catalog.glue_catalog.warehouse", args["BRONZE_BUCKET"])

# ─── 原始 Kafka 消息 Schema（只有 key, value, topic, timestamp）───
RAW_KAFKA_SCHEMA = StructType([
    StructField("key",       StringType(), True),
    StructField("value",     StringType(), True),
    StructField("topic",     StringType(), True),
    StructField("partition", IntegerType(), True),
    StructField("offset",    IntegerType(), True),
    StructField("timestamp", StringType(), True),
])

# ─── system_app_logs Payload Schema（嵌套 JSON 展开）───
APP_LOG_SCHEMA = StructType([
    StructField("log_id",          StringType(), False),
    StructField("trace_id",        StringType(), True),
    StructField("span_id",         StringType(), True),
    StructField("service_name",    StringType(), False),
    StructField("instance_id",     StringType(), True),
    StructField("log_level",       StringType(), False),
    StructField("event_timestamp", StringType(), False),
    StructField("message",         StringType(), False),
    StructField("error_details",   StructType([
        StructField("error_code",  StringType(), True),
        StructField("error_type",  StringType(), True),
        StructField("stack_trace", StringType(), True),
        StructField("http_status", IntegerType(), True),
    ]), True),
    StructField("request_info", StructType([
        StructField("method",      StringType(), True),
        StructField("path",        StringType(), True),
        StructField("user_id",     StringType(), True),
        StructField("duration_ms", StringType(), True),
    ]), True),
    StructField("environment", StringType(), True),
])


def parse_and_flatten(df: DataFrame) -> DataFrame:
    """
    从 Kafka value (base64 encoded JSON string) 解析出结构化字段
    并对嵌套对象进行展开 (flatten)
    """
    # 1. 解析 JSON Payload
    parsed = df.select(
        from_json(col("value").cast("string"), APP_LOG_SCHEMA).alias("payload"),
        col("timestamp").alias("kafka_ingest_timestamp"),
        col("partition"),
        col("offset"),
    )

    # 2. 展开顶层字段 + 展开 error_details 嵌套
    flattened = parsed.select(
        col("payload.log_id"),
        col("payload.trace_id"),
        col("payload.span_id"),
        col("payload.service_name"),
        col("payload.instance_id"),
        col("payload.log_level"),
        to_timestamp(col("payload.event_timestamp")).alias("event_timestamp"),
        col("payload.message"),
        # 嵌套 error_details 展开
        col("payload.error_details.error_code").alias("error_code"),
        col("payload.error_details.error_type").alias("error_type"),
        col("payload.error_details.http_status").alias("http_status"),
        col("payload.error_details.stack_trace").alias("stack_trace"),
        # 嵌套 request_info 展开
        col("payload.request_info.method").alias("req_method"),
        col("payload.request_info.path").alias("req_path"),
        col("payload.request_info.user_id").alias("user_id"),
        col("payload.request_info.duration_ms").cast("double").alias("req_duration_ms"),
        col("payload.environment"),
        # 分区字段
        col("kafka_ingest_timestamp").alias("ingest_timestamp"),
        current_timestamp().alias("processing_timestamp"),
    )
    return flattened


def process_batch(batch_df: DataFrame, batch_id: int):
    """
    Structured Streaming foreachBatch 处理函数
    每个 mini-batch 执行：解析 → DQ 校验 → 有效数据写 Iceberg → 脏数据写死信
    """
    if batch_df.isEmpty():
        return

    str_batch_id = str(batch_id)

    # ─── 1. 解析与展开嵌套 JSON ───
    parsed_df = parse_and_flatten(batch_df)

    # ─── 2. 数据质量校验（三条规则）───
    checker = DataQualityChecker(
        table_name="bronze_app_logs",
        batch_id=str_batch_id,
        job_run_id=JOB_RUN_ID,
        dead_letter_base_path=DEAD_LETTER,
        dq_table_name=args["DQ_TABLE"],
        failure_threshold=0.05,          # 5% 脏数据阈值
        environment=ENVIRONMENT,
    )

    checker.add_rule(
        rule_not_null("log_id")          # log_id 不能为 Null
    ).add_rule(
        rule_not_null("service_name")    # service_name 不能为 Null
    ).add_rule(
        rule_timestamp_in_range(
            "event_timestamp",
            max_lag_hours=24             # 时间戳必须在 24 小时内
        )
    ).add_rule(
        # error_code 如果存在，必须在字典范围内
        rule_in_set(
            "error_code",
            valid_values=VALID_ERROR_CODES | {None},  # 允许 Null（非 Error 级别日志）
            rule_name="valid_error_code",
        )
    )

    valid_df, dead_letter_df, dq_results = checker.run(parsed_df)

    # ─── 3. 有效数据写入 Bronze Iceberg 表 ───
    if not valid_df.isEmpty():
        # 写入 Iceberg，按 service_name + log_level + date 分区
        valid_df.writeTo(
            "glue_catalog.iodp_bronze_{}.app_logs".format(ENVIRONMENT)
        ).using("iceberg") \
         .partitionedBy("service_name", "log_level") \
         .tableProperty("write.parquet.compression-codec", "snappy") \
         .append()

    # ─── 4. 写入血缘事件 ───
    write_lineage_event(
        source_table="msk::system_app_logs",
        target_table=f"s3://bronze/app_logs/",
        transformation="JSON_PARSE + FLATTEN + DQ_CHECK",
        job_name=args["JOB_NAME"],
        job_run_id=JOB_RUN_ID,
        record_count_in=batch_df.count(),
        record_count_out=valid_df.count(),
        record_count_dead_letter=dead_letter_df.count(),
        lineage_table=args["LINEAGE_TABLE"],
    )


# ─── 读取 MSK Kafka 流 ───
kafka_df = spark \
    .readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", args["MSK_BOOTSTRAP_SERVERS"]) \
    .option("subscribe", "system_app_logs") \
    .option("startingOffsets", "latest") \
    .option("kafka.security.protocol", "SASL_SSL") \
    .option("kafka.sasl.mechanism", "AWS_MSK_IAM") \
    .option("kafka.sasl.jaas.config",
            "software.amazon.msk.auth.iam.IAMLoginModule required;") \
    .option("kafka.sasl.client.callback.handler.class",
            "software.amazon.msk.auth.iam.IAMClientCallbackHandler") \
    .option("failOnDataLoss", "false") \
    .load()

# ─── 启动流式处理 ───
query = kafka_df \
    .writeStream \
    .foreachBatch(process_batch) \
    .option("checkpointLocation",
            f"s3://{args['BRONZE_BUCKET']}checkpoints/app_logs/") \
    .trigger(processingTime="60 seconds") \
    .start()

query.awaitTermination()
job.commit()
```

### 5.3 Gold Layer Batch Job — API 错误率统计 (`glue_jobs/batch/gold_api_error_stats.py`)

```python
# glue_jobs/batch/gold_api_error_stats.py
"""
Glue Batch Job: Silver app_logs → Gold api_error_stats
每小时运行一次，输出数据供 Agent Log Analyzer 通过 Athena 查询

输出 Schema:
  stat_hour         TIMESTAMP   统计小时（整点）
  service_name      STRING      服务名
  error_code        STRING      错误码
  total_requests    BIGINT      该小时该服务总请求数
  error_count       BIGINT      错误数
  error_rate        DOUBLE      错误率
  p99_duration_ms   DOUBLE      P99 响应时间
  unique_users      BIGINT      受影响的唯一用户数
  sample_trace_ids  ARRAY<STR>  最多 5 个 trace_id 样本（供 Agent 关联）
"""

import sys
from datetime import datetime, timedelta, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    col, collect_list, count, countDistinct, date_trunc,
    expr, lit, percentile_approx, slice as spark_slice,
    sum as spark_sum, when,
)

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "SILVER_BUCKET", "GOLD_BUCKET",
    "ENVIRONMENT", "GLUE_DATABASE_SILVER", "GLUE_DATABASE_GOLD",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

# 计算处理窗口：上一个完整小时
now_utc = datetime.now(timezone.utc)
processing_hour_end   = now_utc.replace(minute=0, second=0, microsecond=0)
processing_hour_start = processing_hour_end - timedelta(hours=1)

print(f"Processing window: {processing_hour_start} ~ {processing_hour_end}")

# ─── 读取 Silver 层 app_logs ───
silver_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.parsed_logs"
).filter(
    (col("event_timestamp") >= lit(processing_hour_start.isoformat())) &
    (col("event_timestamp") <  lit(processing_hour_end.isoformat())) &
    (col("log_level").isin(["ERROR", "FATAL"]))
)

# ─── 聚合计算 ───
# 先计算总请求数（包含所有 log_level）
all_logs_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.parsed_logs"
).filter(
    (col("event_timestamp") >= lit(processing_hour_start.isoformat())) &
    (col("event_timestamp") <  lit(processing_hour_end.isoformat()))
).groupBy("service_name").agg(
    count("*").alias("total_requests")
)

error_stats_df = silver_df.groupBy(
    date_trunc("hour", col("event_timestamp")).alias("stat_hour"),
    col("service_name"),
    col("error_code"),
).agg(
    count("*").alias("error_count"),
    percentile_approx("req_duration_ms", 0.99).alias("p99_duration_ms"),
    countDistinct("user_id").alias("unique_users"),
    # 采样 trace_id 供 Agent 关联（最多取 5 个）
    spark_slice(collect_list("trace_id"), 1, 5).alias("sample_trace_ids"),
)

# ─── Join 计算错误率 ───
gold_df = error_stats_df.join(
    all_logs_df, on="service_name", how="left"
).withColumn(
    "error_rate",
    when(col("total_requests") > 0,
         col("error_count") / col("total_requests")
    ).otherwise(0.0)
).withColumn(
    "stat_date", col("stat_hour").cast("date")
).withColumn(
    "environment", lit(args["ENVIRONMENT"])
)

# ─── 写入 Gold Iceberg 表（按 stat_date 分区）───
gold_df.writeTo(
    f"glue_catalog.{args['GLUE_DATABASE_GOLD']}.api_error_stats"
).using("iceberg") \
 .partitionedBy("stat_date", "service_name") \
 .tableProperty("write.parquet.compression-codec", "snappy") \
 .overwritePartitions()   # 幂等写入，支持重跑

print(f"Gold api_error_stats written: {gold_df.count()} rows")

job.commit()
```

### 5.4 公共库 — 血缘追踪 (`glue_jobs/lib/lineage.py`)

```python
# glue_jobs/lib/lineage.py
"""
数据血缘事件写入工具
所有 Glue Job 在处理完一个批次后调用 write_lineage_event()，
将 source→target 的数据流转记录写入 DynamoDB lineage_events 表。
"""

import logging
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)


def write_lineage_event(
    source_table: str,
    target_table: str,
    transformation: str,
    job_name: str,
    job_run_id: str,
    record_count_in: int,
    record_count_out: int,
    record_count_dead_letter: int,
    lineage_table: str,
    duration_seconds: int = 0,
    aws_region: str = "us-east-1",
) -> None:
    """
    写入一条血缘事件到 DynamoDB。

    PK: source_table
    SK: event_time#target_table  （保证同一 source 的多次 target 可区分）
    """
    dynamodb = boto3.resource("dynamodb", region_name=aws_region)
    table = dynamodb.Table(lineage_table)

    now_iso = datetime.now(timezone.utc).isoformat()
    sk = f"{now_iso}#{target_table.split('/')[-2] if '/' in target_table else target_table}"
    ttl_seconds = int(datetime.now(timezone.utc).timestamp()) + 180 * 86400  # 180 天 TTL

    try:
        table.put_item(Item={
            "source_table":       source_table,
            "event_time":         sk,
            "target_table":       target_table,
            "transformation":     transformation,
            "job_name":           job_name,
            "job_run_id":         job_run_id,
            "record_count_in":    record_count_in,
            "record_count_out":   record_count_out,
            "record_count_dl":    record_count_dead_letter,
            "duration_seconds":   duration_seconds,
            "TTL":                ttl_seconds,
        })
        logger.info(
            "Lineage event written: %s → %s (%d → %d records)",
            source_table, target_table, record_count_in, record_count_out,
        )
    except Exception as e:
        # 血缘写入失败不中断主流程
        logger.error("Failed to write lineage event: %s", e)
```

---

### 5.5 公共库 — Iceberg 工具 (`glue_jobs/lib/iceberg_utils.py`)

```python
# glue_jobs/lib/iceberg_utils.py
"""
Iceberg 表管理工具函数
封装建表、分区管理等重复操作，各 Glue Job 共用。
"""

import logging
from typing import List, Optional

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
```

---

### 5.6 公共库 — Schema 定义 (`glue_jobs/lib/schema_definitions.py`)

```python
# glue_jobs/lib/schema_definitions.py
"""
集中管理所有 Kafka Topic 和各层表的 PySpark Schema。
各 Glue Job 从此处 import，避免重复定义导致不一致。
"""

from pyspark.sql.types import (
    ArrayType, DoubleType, IntegerType, LongType,
    StringType, StructField, StructType, TimestampType,
)

# ─── Kafka Raw Schema（所有 Topic 共用）───
KAFKA_RAW_SCHEMA = StructType([
    StructField("key",       StringType(),  True),
    StructField("value",     StringType(),  True),
    StructField("topic",     StringType(),  True),
    StructField("partition", IntegerType(), True),
    StructField("offset",    LongType(),    True),
    StructField("timestamp", StringType(),  True),
])

# ─── user_clickstream Payload Schema ───
CLICKSTREAM_SCHEMA = StructType([
    StructField("event_id",        StringType(),  False),
    StructField("user_id",         StringType(),  True),
    StructField("session_id",      StringType(),  True),
    StructField("event_type",      StringType(),  True),
    StructField("event_timestamp", StringType(),  False),
    StructField("page_url",        StringType(),  True),
    StructField("referrer_url",    StringType(),  True),
    StructField("device_info", StructType([
        StructField("device_type",       StringType(), True),
        StructField("os",                StringType(), True),
        StructField("browser",           StringType(), True),
        StructField("screen_resolution", StringType(), True),
    ]), True),
    StructField("geo_info", StructType([
        StructField("country_code", StringType(), True),
        StructField("city",         StringType(), True),
        StructField("ip_hash",      StringType(), True),
    ]), True),
    StructField("properties", StructType([
        StructField("product_id", StringType(), True),
        StructField("amount",     DoubleType(), True),
    ]), True),
])

# ─── system_app_logs Payload Schema ───
APP_LOG_SCHEMA = StructType([
    StructField("log_id",          StringType(),  False),
    StructField("trace_id",        StringType(),  True),
    StructField("span_id",         StringType(),  True),
    StructField("service_name",    StringType(),  False),
    StructField("instance_id",     StringType(),  True),
    StructField("log_level",       StringType(),  False),
    StructField("event_timestamp", StringType(),  False),
    StructField("message",         StringType(),  False),
    StructField("error_details", StructType([
        StructField("error_code",  StringType(),  True),
        StructField("error_type",  StringType(),  True),
        StructField("stack_trace", StringType(),  True),
        StructField("http_status", IntegerType(), True),
    ]), True),
    StructField("request_info", StructType([
        StructField("method",      StringType(), True),
        StructField("path",        StringType(), True),
        StructField("user_id",     StringType(), True),
        StructField("duration_ms", DoubleType(), True),
    ]), True),
    StructField("environment", StringType(), True),
])

# ─── Silver parsed_logs 扁平化 Schema ───
SILVER_APP_LOG_SCHEMA = StructType([
    StructField("log_id",               StringType(),    False),
    StructField("trace_id",             StringType(),    True),
    StructField("span_id",              StringType(),    True),
    StructField("service_name",         StringType(),    False),
    StructField("instance_id",          StringType(),    True),
    StructField("log_level",            StringType(),    False),
    StructField("event_timestamp",      TimestampType(), False),
    StructField("message",              StringType(),    True),
    StructField("error_code",           StringType(),    True),
    StructField("error_type",           StringType(),    True),
    StructField("http_status",          IntegerType(),   True),
    StructField("stack_trace",          StringType(),    True),
    StructField("req_method",           StringType(),    True),
    StructField("req_path",             StringType(),    True),
    StructField("user_id",              StringType(),    True),
    StructField("req_duration_ms",      DoubleType(),    True),
    StructField("environment",          StringType(),    True),
    StructField("ingest_timestamp",     TimestampType(), True),
    StructField("processing_timestamp", TimestampType(), True),
])

# ─── Silver enriched_clicks 扁平化 Schema ───
SILVER_CLICKSTREAM_SCHEMA = StructType([
    StructField("event_id",             StringType(),    False),
    StructField("user_id",              StringType(),    False),
    StructField("session_id",           StringType(),    True),
    StructField("event_type",           StringType(),    True),
    StructField("event_timestamp",      TimestampType(), False),
    StructField("page_url",             StringType(),    True),
    StructField("referrer_url",         StringType(),    True),
    StructField("device_type",          StringType(),    True),
    StructField("os",                   StringType(),    True),
    StructField("browser",              StringType(),    True),
    StructField("country_code",         StringType(),    True),
    StructField("city",                 StringType(),    True),
    StructField("product_id",           StringType(),    True),
    StructField("amount",               DoubleType(),    True),
    StructField("environment",          StringType(),    True),
    StructField("ingest_timestamp",     TimestampType(), True),
    StructField("processing_timestamp", TimestampType(), True),
])
```

---

### 5.7 Glue Streaming Job — 消费 `user_clickstream` (`glue_jobs/streaming/stream_clickstream.py`)

```python
# glue_jobs/streaming/stream_clickstream.py
"""
Glue Streaming Job: MSK user_clickstream → S3 Bronze (Apache Iceberg)
与 stream_app_logs.py 结构一致，处理点击流数据。
DQ 规则：user_id 非空、event_timestamp 在 24 小时内、event_type 合法枚举值。
"""

import sys
import uuid
from datetime import datetime, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import DataFrame
from pyspark.sql.functions import (
    col, current_timestamp, from_json, lit, to_timestamp,
)

from lib.data_quality import (
    DataQualityChecker, rule_not_null, rule_timestamp_in_range, rule_in_set,
)
from lib.lineage import write_lineage_event
from lib.iceberg_utils import configure_iceberg
from lib.schema_definitions import CLICKSTREAM_SCHEMA

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "MSK_BOOTSTRAP_SERVERS", "BRONZE_BUCKET",
    "DQ_TABLE", "LINEAGE_TABLE", "ENVIRONMENT",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

BRONZE_PATH = f"{args['BRONZE_BUCKET']}clickstream/"
DEAD_LETTER = f"{args['BRONZE_BUCKET']}dead_letter/"
ENVIRONMENT = args["ENVIRONMENT"]
JOB_RUN_ID  = args.get("JOB_RUN_ID", str(uuid.uuid4()))

configure_iceberg(spark, args["BRONZE_BUCKET"])

VALID_EVENT_TYPES = {"click", "view", "scroll", "purchase", "add_to_cart", "checkout"}


def parse_and_flatten(df: DataFrame) -> DataFrame:
    """解析 Kafka value JSON，展开嵌套的 device_info / geo_info / properties"""
    parsed = df.select(
        from_json(col("value").cast("string"), CLICKSTREAM_SCHEMA).alias("payload"),
        col("timestamp").alias("kafka_ingest_timestamp"),
        col("partition"),
        col("offset"),
    )
    return parsed.select(
        col("payload.event_id"),
        col("payload.user_id"),
        col("payload.session_id"),
        col("payload.event_type"),
        to_timestamp(col("payload.event_timestamp")).alias("event_timestamp"),
        col("payload.page_url"),
        col("payload.referrer_url"),
        # device_info 展开
        col("payload.device_info.device_type").alias("device_type"),
        col("payload.device_info.os").alias("os"),
        col("payload.device_info.browser").alias("browser"),
        # geo_info 展开
        col("payload.geo_info.country_code").alias("country_code"),
        col("payload.geo_info.city").alias("city"),
        col("payload.geo_info.ip_hash").alias("ip_hash"),
        # properties 展开（业务自定义字段）
        col("payload.properties.product_id").alias("product_id"),
        col("payload.properties.amount").alias("amount"),
        col("payload.environment"),
        col("kafka_ingest_timestamp").alias("ingest_timestamp"),
        current_timestamp().alias("processing_timestamp"),
    )


def process_batch(batch_df: DataFrame, batch_id: int):
    if batch_df.isEmpty():
        return

    str_batch_id = str(batch_id)
    parsed_df = parse_and_flatten(batch_df)

    checker = DataQualityChecker(
        table_name="bronze_clickstream",
        batch_id=str_batch_id,
        job_run_id=JOB_RUN_ID,
        dead_letter_base_path=DEAD_LETTER,
        dq_table_name=args["DQ_TABLE"],
        failure_threshold=0.05,
        environment=ENVIRONMENT,
    )
    checker.add_rule(
        rule_not_null("event_id")
    ).add_rule(
        rule_not_null("user_id")
    ).add_rule(
        rule_timestamp_in_range("event_timestamp", max_lag_hours=24)
    ).add_rule(
        rule_in_set("event_type", valid_values=VALID_EVENT_TYPES, rule_name="valid_event_type")
    )

    valid_df, dead_letter_df, _ = checker.run(parsed_df)

    if not valid_df.isEmpty():
        valid_df.writeTo(
            f"glue_catalog.iodp_bronze_{ENVIRONMENT}.clickstream"
        ).using("iceberg") \
         .partitionedBy("event_type") \
         .tableProperty("write.parquet.compression-codec", "snappy") \
         .append()

    write_lineage_event(
        source_table="msk::user_clickstream",
        target_table=f"s3://iodp-bronze-{ENVIRONMENT}/clickstream/",
        transformation="JSON_PARSE + FLATTEN + DQ_CHECK",
        job_name=args["JOB_NAME"],
        job_run_id=JOB_RUN_ID,
        record_count_in=batch_df.count(),
        record_count_out=valid_df.count(),
        record_count_dead_letter=dead_letter_df.count(),
        lineage_table=args["LINEAGE_TABLE"],
    )


kafka_df = spark \
    .readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", args["MSK_BOOTSTRAP_SERVERS"]) \
    .option("subscribe", "user_clickstream") \
    .option("startingOffsets", "latest") \
    .option("kafka.security.protocol", "SASL_SSL") \
    .option("kafka.sasl.mechanism", "AWS_MSK_IAM") \
    .option("kafka.sasl.jaas.config",
            "software.amazon.msk.auth.iam.IAMLoginModule required;") \
    .option("kafka.sasl.client.callback.handler.class",
            "software.amazon.msk.auth.iam.IAMClientCallbackHandler") \
    .option("failOnDataLoss", "false") \
    .load()

query = kafka_df \
    .writeStream \
    .foreachBatch(process_batch) \
    .option("checkpointLocation",
            f"s3://{args['BRONZE_BUCKET']}checkpoints/clickstream/") \
    .trigger(processingTime="60 seconds") \
    .start()

query.awaitTermination()
job.commit()
```

---

### 5.8 Glue Batch Job — Bronze→Silver 日志解析 (`glue_jobs/batch/silver_parse_logs.py`)

```python
# glue_jobs/batch/silver_parse_logs.py
"""
Glue Batch Job: Bronze app_logs → Silver parsed_logs
每小时运行，对上一小时的 Bronze 数据做：
  1. 去重（log_id 唯一）
  2. 类型校正（event_timestamp 为 TIMESTAMP，req_duration_ms 为 DOUBLE）
  3. 写入 Silver Iceberg 表，按 service_name + log_level 分区
"""

import sys
from datetime import datetime, timedelta, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    col, current_timestamp, lit, row_number, to_timestamp,
)
from pyspark.sql.window import Window

from lib.lineage import write_lineage_event
from lib.iceberg_utils import configure_iceberg, iceberg_merge_dedup

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "BRONZE_BUCKET", "SILVER_BUCKET",
    "GLUE_DATABASE_BRONZE", "GLUE_DATABASE_SILVER",
    "LINEAGE_TABLE", "ENVIRONMENT",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

configure_iceberg(spark, args["SILVER_BUCKET"])

now_utc = datetime.now(timezone.utc)
hour_end   = now_utc.replace(minute=0, second=0, microsecond=0)
hour_start = hour_end - timedelta(hours=1)

print(f"Processing window: {hour_start} ~ {hour_end}")

# ─── 1. 读取 Bronze 上一小时数据 ───
bronze_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_BRONZE']}.app_logs"
).filter(
    (col("ingest_timestamp") >= lit(hour_start.isoformat())) &
    (col("ingest_timestamp") <  lit(hour_end.isoformat()))
)

input_count = bronze_df.count()
print(f"Bronze records read: {input_count}")

# ─── 2. 去重：同一 log_id 保留 ingest_timestamp 最早的那条 ───
window = Window.partitionBy("log_id").orderBy(col("ingest_timestamp").asc())
deduped_df = bronze_df \
    .withColumn("_rn", row_number().over(window)) \
    .filter(col("_rn") == 1) \
    .drop("_rn")

# ─── 3. 类型标准化（Bronze 已做 to_timestamp，这里补充其他类型校正）───
silver_df = deduped_df \
    .withColumn("req_duration_ms", col("req_duration_ms").cast("double")) \
    .withColumn("http_status",     col("http_status").cast("integer")) \
    .withColumn("processing_timestamp", current_timestamp()) \
    .withColumn("event_date", col("event_timestamp").cast("date"))

# ─── 4. 写入 Silver Iceberg（MERGE 去重，幂等重跑）───
silver_df.createOrReplaceTempView("silver_source")
iceberg_merge_dedup(
    spark=spark,
    source_view="silver_source",
    target_table=f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.parsed_logs",
    merge_keys=["log_id"],
)

output_count = silver_df.count()
print(f"Silver records written: {output_count}")

# ─── 5. 血缘记录 ───
write_lineage_event(
    source_table=f"s3://iodp-bronze-{args['ENVIRONMENT']}/app_logs/",
    target_table=f"s3://iodp-silver-{args['ENVIRONMENT']}/parsed_logs/",
    transformation="DEDUP(log_id) + TYPE_CAST",
    job_name=args["JOB_NAME"],
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    record_count_in=input_count,
    record_count_out=output_count,
    record_count_dead_letter=input_count - output_count,
    lineage_table=args["LINEAGE_TABLE"],
)

job.commit()
```

---

### 5.9 Glue Batch Job — Bronze→Silver 点击流 (`glue_jobs/batch/silver_enrich_clicks.py`)

```python
# glue_jobs/batch/silver_enrich_clicks.py
"""
Glue Batch Job: Bronze clickstream → Silver enriched_clicks
每小时运行，对上一小时的点击流 Bronze 数据做：
  1. 去重（event_id 唯一）
  2. 补充城市级地理维度（此处示意，实际可查 MaxMind IP 库）
  3. 写入 Silver Iceberg 表，按 event_type + event_date 分区
"""

import sys
from datetime import datetime, timedelta, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    col, current_timestamp, lit, row_number, when,
)
from pyspark.sql.window import Window

from lib.lineage import write_lineage_event
from lib.iceberg_utils import configure_iceberg, iceberg_merge_dedup

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "BRONZE_BUCKET", "SILVER_BUCKET",
    "GLUE_DATABASE_BRONZE", "GLUE_DATABASE_SILVER",
    "LINEAGE_TABLE", "ENVIRONMENT",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

configure_iceberg(spark, args["SILVER_BUCKET"])

now_utc = datetime.now(timezone.utc)
hour_end   = now_utc.replace(minute=0, second=0, microsecond=0)
hour_start = hour_end - timedelta(hours=1)

# ─── 1. 读取 Bronze ───
bronze_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_BRONZE']}.clickstream"
).filter(
    (col("ingest_timestamp") >= lit(hour_start.isoformat())) &
    (col("ingest_timestamp") <  lit(hour_end.isoformat()))
)

input_count = bronze_df.count()

# ─── 2. 去重：同一 event_id 保留最早的一条 ───
window = Window.partitionBy("event_id").orderBy(col("ingest_timestamp").asc())
deduped_df = bronze_df \
    .withColumn("_rn", row_number().over(window)) \
    .filter(col("_rn") == 1) \
    .drop("_rn")

# ─── 3. 补充维度：对缺失城市的记录用 country_code 兜底 ───
enriched_df = deduped_df.withColumn(
    "city",
    when(col("city").isNull(), lit("unknown")).otherwise(col("city"))
).withColumn(
    "event_date", col("event_timestamp").cast("date")
).withColumn(
    "processing_timestamp", current_timestamp()
)

# ─── 4. 写入 Silver（MERGE 去重）───
enriched_df.createOrReplaceTempView("silver_clicks_source")
iceberg_merge_dedup(
    spark=spark,
    source_view="silver_clicks_source",
    target_table=f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.enriched_clicks",
    merge_keys=["event_id"],
)

output_count = enriched_df.count()

# ─── 5. 血缘 ───
write_lineage_event(
    source_table=f"s3://iodp-bronze-{args['ENVIRONMENT']}/clickstream/",
    target_table=f"s3://iodp-silver-{args['ENVIRONMENT']}/enriched_clicks/",
    transformation="DEDUP(event_id) + ENRICH_CITY",
    job_name=args["JOB_NAME"],
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    record_count_in=input_count,
    record_count_out=output_count,
    record_count_dead_letter=input_count - output_count,
    lineage_table=args["LINEAGE_TABLE"],
)

job.commit()
```

---

### 5.10 Glue Batch Job — 每小时活跃用户 (`glue_jobs/batch/gold_hourly_active_users.py`)

```python
# glue_jobs/batch/gold_hourly_active_users.py
"""
Glue Batch Job: Silver enriched_clicks → Gold hourly_active_users
每小时运行，统计各漏斗阶段的去重用户数，供 BI Dashboard 画转化漏斗。

输出 Schema:
  stat_hour           TIMESTAMP  统计小时（整点）
  stat_date           DATE       分区键
  active_users        BIGINT     该小时有任意行为的去重用户数
  view_users          BIGINT     浏览用户数
  add_to_cart_users   BIGINT     加购用户数
  checkout_users      BIGINT     进入结算用户数
  purchase_users      BIGINT     完成购买用户数
  new_users           BIGINT     首次出现的用户数（该小时前无记录）
  environment         STRING
"""

import sys
from datetime import datetime, timedelta, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    col, count, countDistinct, date_trunc, lit, when,
)

from lib.lineage import write_lineage_event
from lib.iceberg_utils import configure_iceberg

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "SILVER_BUCKET", "GOLD_BUCKET",
    "GLUE_DATABASE_SILVER", "GLUE_DATABASE_GOLD",
    "LINEAGE_TABLE", "ENVIRONMENT",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

configure_iceberg(spark, args["GOLD_BUCKET"])

now_utc    = datetime.now(timezone.utc)
hour_end   = now_utc.replace(minute=0, second=0, microsecond=0)
hour_start = hour_end - timedelta(hours=1)

# ─── 读取 Silver 上一小时点击流 ───
silver_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.enriched_clicks"
).filter(
    (col("event_timestamp") >= lit(hour_start.isoformat())) &
    (col("event_timestamp") <  lit(hour_end.isoformat()))
)

input_count = silver_df.count()

# ─── 查询历史，识别 new_users（当前小时前从未出现的 user_id）───
# 简化实现：查 Silver 全表中 event_timestamp < hour_start 的 user_id 集合
existing_users_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_SILVER']}.enriched_clicks"
).filter(
    col("event_timestamp") < lit(hour_start.isoformat())
).select("user_id").distinct()

current_users_df = silver_df.select("user_id").distinct()
new_users_df = current_users_df.subtract(existing_users_df)
new_user_count = new_users_df.count()

# ─── 漏斗聚合（按小时统计各 event_type 的去重用户数）───
gold_df = silver_df.groupBy(
    date_trunc("hour", col("event_timestamp")).alias("stat_hour"),
).agg(
    countDistinct("user_id").alias("active_users"),
    countDistinct(when(col("event_type") == "view",         col("user_id"))).alias("view_users"),
    countDistinct(when(col("event_type") == "add_to_cart",  col("user_id"))).alias("add_to_cart_users"),
    countDistinct(when(col("event_type") == "checkout",     col("user_id"))).alias("checkout_users"),
    countDistinct(when(col("event_type") == "purchase",     col("user_id"))).alias("purchase_users"),
).withColumn(
    "new_users", lit(new_user_count)
).withColumn(
    "stat_date", col("stat_hour").cast("date")
).withColumn(
    "environment", lit(args["ENVIRONMENT"])
)

# ─── 写入 Gold Iceberg（覆盖当前分区，支持幂等重跑）───
gold_df.writeTo(
    f"glue_catalog.{args['GLUE_DATABASE_GOLD']}.hourly_active_users"
).using("iceberg") \
 .partitionedBy("stat_date") \
 .tableProperty("write.parquet.compression-codec", "snappy") \
 .overwritePartitions()

output_count = gold_df.count()
print(f"Gold hourly_active_users written: {output_count} rows for {hour_start}")

write_lineage_event(
    source_table=f"s3://iodp-silver-{args['ENVIRONMENT']}/enriched_clicks/",
    target_table=f"s3://iodp-gold-{args['ENVIRONMENT']}/hourly_active_users/",
    transformation="FUNNEL_AGGREGATION",
    job_name=args["JOB_NAME"],
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    record_count_in=input_count,
    record_count_out=output_count,
    record_count_dead_letter=0,
    lineage_table=args["LINEAGE_TABLE"],
)

job.commit()
```

---

### 5.11 Glue Batch Job — 故障工单摘要 (`glue_jobs/batch/gold_incident_summary.py`)

```python
# glue_jobs/batch/gold_incident_summary.py
"""
Glue Batch Job: Gold api_error_stats → Gold incident_summary
每天运行（凌晨），识别过去 24 小时内的故障事件，生成自然语言摘要。
输出 JSON Lines 格式，供 Lambda 向量化后写入 OpenSearch RAG 知识库。

识别规则：同一 service_name + error_code 连续 ≥ 2 小时 error_rate > 5%，
          视为一次 incident。
"""

import json
import sys
from datetime import datetime, timedelta, timezone

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import (
    col, collect_list, lit, max as spark_max,
    min as spark_min, round as spark_round, sum as spark_sum,
    count,
)

from lib.lineage import write_lineage_event
from lib.iceberg_utils import configure_iceberg

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "GOLD_BUCKET",
    "GLUE_DATABASE_GOLD", "LINEAGE_TABLE", "ENVIRONMENT",
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

configure_iceberg(spark, args["GOLD_BUCKET"])

now_utc    = datetime.now(timezone.utc)
day_end    = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
day_start  = day_end - timedelta(days=1)

INCIDENT_ERROR_RATE_THRESHOLD = 0.05   # 5% 才算故障
INCIDENT_MIN_HOURS = 2                  # 至少持续 2 小时

# ─── 读取昨天的 api_error_stats ───
stats_df = spark.read.format("iceberg").load(
    f"glue_catalog.{args['GLUE_DATABASE_GOLD']}.api_error_stats"
).filter(
    (col("stat_hour") >= lit(day_start.isoformat())) &
    (col("stat_hour") <  lit(day_end.isoformat())) &
    (col("error_rate") >= lit(INCIDENT_ERROR_RATE_THRESHOLD))
)

# ─── 按 service + error_code 聚合：只保留持续 ≥ 2 小时的 ───
incident_candidates = stats_df.groupBy(
    col("service_name"),
    col("error_code"),
).agg(
    count("stat_hour").alias("affected_hours"),
    spark_min("stat_hour").alias("start_time"),
    spark_max("stat_hour").alias("end_time"),
    spark_max("error_rate").alias("peak_error_rate"),
    spark_sum("unique_users").alias("total_affected_users"),
    spark_max("p99_duration_ms").alias("peak_p99_ms"),
    collect_list("sample_trace_ids").alias("all_trace_samples"),
).filter(
    col("affected_hours") >= INCIDENT_MIN_HOURS
)

rows = incident_candidates.collect()
print(f"Incidents identified: {len(rows)}")

# ─── 生成自然语言摘要，写入 Gold incident_summary（JSON Lines）───
incident_records = []
for row in rows:
    incident_id = (
        f"INC-{day_start.strftime('%Y-%m-%d')}"
        f"-{row['service_name']}-{row['error_code']}"
    ).replace("/", "-")

    # 展平 trace samples（list of lists → list）
    flat_traces = [t for sublist in (row["all_trace_samples"] or []) for t in (sublist or [])]

    severity = "P0" if row["peak_error_rate"] >= 0.20 else \
               "P1" if row["peak_error_rate"] >= 0.05 else "P2"

    record = {
        "incident_id":         incident_id,
        "title":               f"{row['service_name']} {row['error_code']} 故障",
        "service_name":        row["service_name"],
        "error_codes":         json.dumps([row["error_code"]]),
        "severity":            severity,
        "start_time":          str(row["start_time"]),
        "end_time":            str(row["end_time"]),
        "peak_error_rate":     round(row["peak_error_rate"], 4),
        "total_affected_users": int(row["total_affected_users"] or 0),
        "peak_p99_ms":         round(row["peak_p99_ms"] or 0, 1),
        "symptoms": (
            f"{row['service_name']} 在 {row['start_time']} ~ {row['end_time']} 出现 "
            f"{row['error_code']} 错误，错误率峰值 {row['peak_error_rate']:.1%}，"
            f"P99 响应时间 {row['peak_p99_ms']:.0f}ms，"
            f"累计影响 {row['total_affected_users']} 名用户。"
        ),
        "root_cause":   "待人工补充或历史经验推断",
        "resolution":   "待人工补充",
        "resolved_at":  str(day_end),
        "sample_traces": json.dumps(flat_traces[:5]),
        "stat_date":    day_start.strftime("%Y-%m-%d"),
        "environment":  args["ENVIRONMENT"],
    }
    incident_records.append(record)

if incident_records:
    incidents_rdd = spark.sparkContext.parallelize(incident_records)
    incidents_df = spark.read.json(incidents_rdd)
    incidents_df.writeTo(
        f"glue_catalog.{args['GLUE_DATABASE_GOLD']}.incident_summary"
    ).using("iceberg") \
     .partitionedBy("stat_date") \
     .tableProperty("write.parquet.compression-codec", "snappy") \
     .overwritePartitions()

    print(f"incident_summary written: {len(incident_records)} rows")
else:
    print("No incidents detected for this period.")

write_lineage_event(
    source_table=f"s3://iodp-gold-{args['ENVIRONMENT']}/api_error_stats/",
    target_table=f"s3://iodp-gold-{args['ENVIRONMENT']}/incident_summary/",
    transformation="INCIDENT_DETECTION + SUMMARY_GENERATION",
    job_name=args["JOB_NAME"],
    job_run_id=args.get("JOB_RUN_ID", "unknown"),
    record_count_in=stats_df.count(),
    record_count_out=len(incident_records),
    record_count_dead_letter=0,
    lineage_table=args["LINEAGE_TABLE"],
)

job.commit()
```

---

## 6. 数据质量框架

### 6.1 规则清单

| 规则名 | 目标字段 | 校验逻辑 | 错误类型 | 适用 Topic |
|--------|---------|---------|---------|-----------|
| `not_null_user_id` | `user_id` | 非 NULL 且非空字符串 | `NULL_USER_ID` | clickstream |
| `not_null_log_id` | `log_id` | 非 NULL | `NULL_LOG_ID` | app_logs |
| `not_null_service_name` | `service_name` | 非 NULL 且非空 | `NULL_SERVICE_NAME` | app_logs |
| `ts_in_range_event_timestamp` | `event_timestamp` | 距当前时间 ≤ 24h | `TIMESTAMP_OUT_OF_RANGE` | 两者 |
| `valid_error_code` | `error_code` | 在 `VALID_ERROR_CODES` 字典中（允许 NULL） | `INVALID_ERROR_CODE` | app_logs |
| `valid_event_type` | `event_type` | 枚举值之一 | `INVALID_EVENT_TYPE` | clickstream |
| `valid_log_level` | `log_level` | `DEBUG/INFO/WARN/ERROR/FATAL` | `INVALID_LOG_LEVEL` | app_logs |

### 6.2 死信处理流程

```
批次数据 (N 条)
     │
     ▼
DQ 校验（所有规则并行打标记）
     │
     ├── 失败率 ≤ 5%：写日志，继续正常流程（失败记录被丢弃）
     │
     └── 失败率 > 5%：
              │
              ├── 失败记录写入 s3://bronze/dead_letter/table=xxx/batch_id=yyy/
              │
              ├── DynamoDB dq_reports 写入告警记录
              │   （供 Agent Log Analyzer 工具查询）
              │
              └── CloudWatch 自定义 Metric +1
                  （触发 CloudWatch Alarm → SNS → Email）
```

### 6.3 死信重灌运维 Runbook

当数据工程师审查 dead letter 数据后，确认问题已修复（例如 DQ 规则已放宽、上游 Schema 问题已解决），可按以下步骤将死信数据重灌回 Bronze 层。

#### 完整流程

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 1: DLQ Replay Lambda — 将死信文件复制到 replay/ 目录            │
│                                                                      │
│ aws lambda invoke \                                                  │
│   --function-name iodp-dlq-replay-{env} \                           │
│   --payload '{                                                       │
│     "table_name": "bronze_app_logs",                                │
│     "batch_date": "2026-04-06",                                     │
│     "dry_run": true                                                  │
│   }' \                                                               │
│   response.json                                                      │
│                                                                      │
│ 先 dry_run=true 确认文件数量和大小，再改为 dry_run=false 执行复制。  │
│                                                                      │
│ 结果：                                                               │
│   dead_letter/table=bronze_app_logs/batch_id=*/date=2026-04-06/     │
│     → replay/bronze_app_logs/2026-04-06/                            │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Step 2: Replay Glue Job — 从 replay/ 写回 Bronze Iceberg 表         │
│                                                                      │
│ # app_logs 重灌                                                      │
│ aws glue start-job-run \                                             │
│   --job-name iodp-replay-app-logs-to-bronze-{env} \                 │
│   --arguments '{                                                     │
│     "--TABLE_NAME": "bronze_app_logs",                              │
│     "--BATCH_DATE": "2026-04-06"                                    │
│   }'                                                                 │
│                                                                      │
│ # clickstream 重灌                                                   │
│ aws glue start-job-run \                                             │
│   --job-name iodp-replay-clickstream-to-bronze-{env} \              │
│   --arguments '{                                                     │
│     "--TABLE_NAME": "bronze_clickstream",                           │
│     "--BATCH_DATE": "2026-04-06"                                    │
│   }'                                                                 │
│                                                                      │
│ 操作：读取 replay/ parquet → 去除 _dq_error_type 列 → append Bronze │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Step 3: 等待下游自动消费                                              │
│                                                                      │
│ • app_logs → silver_parse_logs（每小时自动运行，MERGE 去重幂等）     │
│ • clickstream → silver_enrich_clicks（每小时自动运行）               │
│                                                                      │
│ 无需手动触发 Silver/Gold Job，Iceberg 表更新后下游自动可见。         │
└──────────────────────────────────────────────────────────────────────┘
```

#### 注意事项

| 项目 | 说明 |
|------|------|
| **幂等性** | Silver 层使用 `iceberg_merge_dedup(merge_keys=["log_id"])` / `["event_id"]`，重复执行不会产生重复数据 |
| **dead letter 保留期** | 30 天（S3 Lifecycle 自动删除），需在过期前完成重灌 |
| **replay/ 目录清理** | 重灌完成后可手动删除 `s3://{bucket}/replay/{table}/{date}/`，非必须 |
| **并发保护** | Glue Job 设置 `max_concurrent_runs = 1`，Lambda 设置 `reserved_concurrent_executions = 1` |
| **监控** | 查看 Glue Job CloudWatch Logs 确认写入行数，或查 DynamoDB lineage_events 表 |

---

## 7. Medallion 三层架构规格

### 7.0 概念解释：这些东西到底是什么？

#### 7.0.1 普通 S3 Bucket 与"数据湖分层 Bucket"的区别

**普通 S3 Bucket** 就是一个对象存储，你往里面扔文件，它不关心文件内容是什么、有没有 Schema、数据是否重复、能不能被 SQL 查询——它只是一个"网络硬盘"。

本项目里的 Bronze/Silver/Gold **不是特殊的 S3 Bucket 类型**，S3 本身没有这个区分。它们其实是**三个普通 S3 Bucket**，只是我们在命名和使用规范上做了区分，配合 Glue + Athena + Iceberg 技术栈，使其具备"数据仓库"的能力。区别对比如下：

| 对比维度 | 普通 S3 Bucket（随便用） | 本项目的分层 Bucket（数据湖规范） |
|---------|----------------------|-------------------------------|
| **内容** | 任意文件（图片、日志、zip） | 严格的 Parquet/Iceberg 列式数据文件 |
| **Schema** | 无，S3 不感知格式 | 由 Glue Data Catalog 统一管理，有明确表结构 |
| **查询能力** | 不能直接 SQL 查询 | 可以用 Athena 直接 SQL 查询，秒级返回 |
| **数据质量** | 存什么是什么 | 有 DQ 规则过滤，保证数据符合预期 |
| **ACID 事务** | 无 | Iceberg 格式提供 ACID，支持并发写入安全 |
| **时间旅行** | 不支持 | Iceberg 支持查询"3天前的快照数据" |
| **分区裁剪** | 扫全量文件 | Iceberg 元数据驱动，只扫相关分区，节省 Athena 费用 |
| **演化 Schema** | 无法做到 | Iceberg 支持 Add/Rename/Drop 列而不重写数据 |

**总结一句话**：Bronze/Silver/Gold 是"普通 S3 + Iceberg 表格式 + Glue Catalog"三者叠加后，让 S3 升级成具备数仓能力的数据湖分层。

---

#### 7.0.2 Apache Iceberg 是什么？

Iceberg 是一种**开放的表格式（Table Format）**，运行在 S3 之上，解决了"怎么在对象存储里像数据库一样管理数据"的问题。

**理解 Iceberg 的最简单类比**：

> 想象你在 S3 里存了 10 亿行日志，分散在 5 万个 Parquet 文件里。  
> 如果没有 Iceberg：Athena 查询时要先扫一遍所有 5 万个文件的元数据，慢且贵。  
> 有了 Iceberg：Iceberg 维护了一份精确的"目录"（元数据树），Athena 直接定位到相关的 200 个文件，99.96% 的数据被跳过，查询快 100 倍，费用降 100 倍。

**Iceberg 在文件系统里的实际样子**：

```
s3://iodp-bronze-prod/app_logs/
│
├── metadata/             ← Iceberg 元数据层（对用户透明）
│   ├── v1.metadata.json  ← 表的快照历史、Schema 版本、分区规格
│   ├── v2.metadata.json
│   ├── snap-001.avro     ← 快照清单（记录哪些文件属于快照 001）
│   └── snap-002.avro
│
└── data/                 ← 实际数据文件（Parquet 列式格式）
    ├── service_name=payment-service/
    │   ├── log_level=ERROR/
    │   │   ├── 00001.parquet  (压缩后约 128MB/文件)
    │   │   └── 00002.parquet
    │   └── log_level=INFO/
    │       └── 00003.parquet
    └── service_name=checkout-service/
        └── log_level=ERROR/
            └── 00004.parquet
```

**Iceberg 的核心能力**：

| 能力 | 解释 | 本项目用到的场景 |
|------|------|----------------|
| **ACID 事务** | 多个 Glue Job 并发写入同一张表不会数据错乱 | Streaming Job 60s 一批并发写 Bronze |
| **时间旅行** | `SELECT * FROM table FOR SYSTEM_TIME AS OF '2026-04-01'` | 数据回溯、审计 |
| **Schema 演化** | 加列不需要重写历史数据 | 业务字段扩展时无需重建表 |
| **分区演化** | 改分区策略不需要重写数据 | 初期按天，后期改为按小时 |
| **Snapshot 隔离** | 读写互不阻塞 | Batch Job 读 Silver 时 Streaming 继续写 Bronze |
| **小文件合并（Compaction）** | 自动将大量小 Parquet 文件合并成大文件 | Streaming 60s 写的小文件定期合并，提升查询性能 |

---

#### 7.0.3 Bronze / Silver / Gold 三层各自是什么？

这三层来自 **Medallion Architecture（奖牌架构）**，是 Databricks 推广的数据湖最佳实践，用金属价值（铜 < 银 < 金）比喻数据的价值密度和质量等级。

```
原始事件流
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│  🥉 Bronze Layer（铜层）— 原始数据保险箱                          │
│                                                                  │
│  • 从 MSK Kafka 消费到的原始 JSON，几乎不做任何转换              │
│  • 只做最基本的 Schema 解析 + 死信隔离                           │
│  • 保留全量原始数据，出了问题可以从这里重新处理（Replayable）      │
│  • 相当于"原材料仓库"——矿石，有用无用全部先存下来                │
│                                                                  │
│  本项目举例：                                                    │
│    s3://iodp-bronze-prod/app_logs/                              │
│    字段：log_id, service_name, log_level, event_timestamp,      │
│          error_code, stack_trace, user_id ... （原始展开字段）   │
└─────────────────┬───────────────────────────────────────────────┘
                  │ Glue Batch Job（每小时）：去重 + 补维度 + 格式标准化
                  ▼
┌─────────────────────────────────────────────────────────────────┐
│  🥈 Silver Layer（银层）— 干净的、可信赖的单实体表               │
│                                                                  │
│  • 去重（针对 Kafka At-Least-Once 可能导致的重复消息）           │
│  • 类型标准化（string → timestamp，string → double 等）         │
│  • 填充维度（如根据 IP 补充地理位置信息）                        │
│  • 可以被多个下游聚合任务复用                                    │
│  • 相当于"精加工原料"——冶炼后的金属棒，可以直接用于生产          │
│                                                                  │
│  本项目举例：                                                    │
│    s3://iodp-silver-prod/parsed_logs/                           │
│    字段：log_id（去重后唯一）, service_name, log_level,         │
│          event_timestamp（类型正确）, error_code,               │
│          req_duration_ms（double 类型）...                      │
└─────────────────┬───────────────────────────────────────────────┘
                  │ Glue Batch Job（每小时/每天）：聚合 + 宽表生成
                  ▼
┌─────────────────────────────────────────────────────────────────┐
│  🥇 Gold Layer（金层）— 业务级聚合宽表，直接供消费               │
│                                                                  │
│  • 面向具体业务问题的聚合结果（不再是原始行级数据）              │
│  • 字段都是业务语言：error_rate（错误率）、unique_users（用户数） │
│  • Agent 的 Log Analyzer 直接查这层，不用自己做聚合              │
│  • BI 报表也直接连这层，查询快（数据量少，聚合已完成）           │
│  • 相当于"成品"——组装好的产品，可以直接交付给客户               │
│                                                                  │
│  本项目举例：                                                    │
│    s3://iodp-gold-prod/api_error_stats/                         │
│    字段：stat_hour, service_name, error_code,                   │
│          error_rate（0.34 = 34%）, p99_duration_ms,            │
│          unique_users, sample_trace_ids...                      │
└─────────────────────────────────────────────────────────────────┘
```

**为什么要三层，不直接从 Kafka 写 Gold Layer？**

| 如果只有一层（直接 Kafka → Gold） | 三层的优势 |
|-------------------------------|-----------|
| 出错了无法重新处理（原始数据没存） | Bronze 层是原始备份，随时可以重跑 |
| 不同业务需求要重复写相似的复杂逻辑 | Silver 层是公共"干净数据"，多个 Gold Job 复用 |
| 一个 Schema 变更就要重写整个 ETL | 变更影响范围可控，只需更新对应层的 Job |
| 查询性能差（Gold 查原始 Kafka 级别数据） | Gold 层数据量小（聚合后），查询极快 |
| 调试困难（不知道哪步引入了错误） | 可以按层追查：Bronze OK → Silver 有问题 → 定位 Silver ETL |

---

#### 7.0.4 Glue Data Catalog 的角色

光有 S3 + Iceberg 文件还不够，需要有人告诉 Athena "这个路径下的数据是什么表、有哪些列、是什么类型"。这就是 **Glue Data Catalog** 的作用——它是整个数据湖的**元数据目录**，相当于图书馆的索引系统。

```
Athena 收到 SQL:
  SELECT error_rate FROM iodp_gold_prod.api_error_stats WHERE stat_date = '2026-04-06'

      │
      ▼  查询 Glue Catalog
  "iodp_gold_prod.api_error_stats" 对应的 S3 路径是:
    s3://iodp-gold-prod/api_error_stats/
  字段有: stat_hour(timestamp), service_name(string), error_rate(double)...
  分区键是: stat_date, service_name
  格式是: Iceberg

      │                         │
      ▼                         ▼
  Iceberg 元数据定位         只扫 stat_date=2026-04-06 的分区文件
  相关文件 3 个 (共 128MB)   跳过其余 99% 的数据

      │
      ▼
  返回结果（通常 < 3秒）
```

---

### 7.1 三层规格汇总表

| 层 | Bucket | 格式 | 写入方式 | 保留期 | 消费方 |
|----|--------|------|---------|--------|--------|
| **Bronze** | `iodp-bronze-{env}` | Iceberg (Parquet) | 流式，60s micro-batch | 1年 | Silver Glue Job |
| **Silver** | `iodp-silver-{env}` | Iceberg (Parquet) | 批式，每小时 | 2年 | Gold Glue Job |
| **Gold** | `iodp-gold-{env}` | Iceberg (Parquet) | 批式，每小时/每天 | 3年 | Athena（Agent 查询）、BI |
| **Dead Letter** | `iodp-bronze-{env}/dead_letter/` | Parquet（不含 Iceberg 元数据） | 流式，随DQ触发 | 30天 | 数据工程团队人工审查 |

---

## 8. 血缘追踪设计

```
血缘链路示例:

MSK::user_clickstream
    │
    │ [stream_clickstream.py] 2026-04-06T14:00Z | 100万条 → 98万条(死信2万)
    ▼
S3::bronze/clickstream/2026-04-06/hour=14/
    │
    │ [silver_enrich_clicks.py] 2026-04-06T15:05Z | 98万条 → 97万条(去重1万)
    ▼
S3::silver/enriched_clicks/2026-04-06/
    │
    │ [gold_api_error_stats.py] 2026-04-06T15:10Z | 聚合 → 1200行统计数据
    ▼
S3::gold/api_error_stats/2026-04-06/hour=14/
    │
    │ [Athena View: v_error_log_enriched]
    ▼
〔Agent Log Analyzer 查询〕
```

血缘数据存储在 `DynamoDB::lineage_events`，支持按 `source_table` 或 `event_time` 反向追溯。

---

## 9. 监控与告警规格

| 告警名 | 触发条件 | 严重级别 | 处理建议 |
|--------|---------|---------|---------|
| `MSK Consumer Lag` | 积压 > 5 分钟 | High | 检查 Glue Streaming 是否挂起，增加 Worker |
| `Glue Job Failure` | 任意 Job 运行失败 | High | 查看 CloudWatch Logs，重跑 Job |
| `DQ Failure Rate` | 失败率 > 5% (自定义指标) | Medium | 上游 Schema 是否变更，检查死信区数据 |
| `S3 Dead Letter Growth` | 死信区 30 天内超 1GB | Low | 数据工程师审查，更新 Schema 或规则 |
| `Glue Job Duration P99` | 超过基准的 2x | Medium | 检查数据倾斜或资源瓶颈 |

---

## 10. FinOps 成本控制规格

| 控制项 | 实现方式 | 预期节省 |
|--------|---------|---------|
| 强制 Environment + CostCenter 标签 | Terraform `default_tags` | 成本归因到团队 |
| S3 生命周期（30/90/365天） | `aws_s3_bucket_lifecycle_configuration` | 存储成本降低 ~60% |
| KMS Bucket Key | `bucket_key_enabled = true` | KMS API 调用费用 ~99% |
| Glue Auto Scaling | `--enable-auto-scaling = true` | 低峰期资源节省 ~40% |
| DynamoDB TTL（90/180天） | `TTL` 属性 | 表大小控制在合理范围 |
| MSK Serverless | 无需预置容量，按使用量计费 | 相比 MSK Provisioned 节省 ~50% |
| Glue G.1X 最小规格起步 | `worker_type = "G.1X"` | 控制初始运行成本 |
| Athena 按扫描量计费优化 | Iceberg 分区裁剪 + 列式存储 | 查询费用降低 ~70% |

---

## 11. CI/CD 流水线

### `.github/workflows/terraform-plan.yml`

```yaml
name: Terraform Plan

on:
  pull_request:
    paths: ["terraform/**"]

jobs:
  plan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "1.6.0"

      - name: Configure AWS Credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: us-east-1

      - name: Terraform Init
        run: terraform init -backend-config="environments/prod.tfvars"
        working-directory: terraform/

      - name: Terraform Validate
        run: terraform validate
        working-directory: terraform/

      - name: Terraform Plan
        run: |
          terraform plan \
            -var-file="environments/prod.tfvars" \
            -out=tfplan \
            -detailed-exitcode
        working-directory: terraform/

      - name: Post Plan to PR
        uses: actions/github-script@v7
        # ... 将 plan 输出作为 PR 评论
```

---

## 12. 与 Agent 层的数据接口契约

以下是本项目（前处理层）向 [项目二 Agent](./Agent.md) 暴露的**稳定数据接口**，Agent 的 `Log Analyzer` 工具依赖这些接口。

### 12.0 Athena DDL — 建表语句

#### Bronze `clickstream` (`athena/ddl/bronze_clickstream.sql`)

```sql
-- athena/ddl/bronze_clickstream.sql
-- Bronze 层 clickstream Iceberg 表（Glue Streaming Job 写入前需先建表）

CREATE TABLE IF NOT EXISTS iodp_bronze_prod.clickstream (
    event_id             STRING        COMMENT 'Kafka 消息唯一 ID',
    user_id              STRING        COMMENT '用户 ID',
    session_id           STRING        COMMENT '会话 ID',
    event_type           STRING        COMMENT 'click|view|scroll|purchase|add_to_cart|checkout',
    event_timestamp      TIMESTAMP     COMMENT '事件发生时间（UTC）',
    page_url             STRING        COMMENT '当前页面 URL',
    referrer_url         STRING        COMMENT '来源页面 URL',
    device_type          STRING        COMMENT 'mobile|desktop|tablet',
    os                   STRING        COMMENT '操作系统',
    browser              STRING        COMMENT '浏览器',
    country_code         STRING        COMMENT '国家代码 ISO 3166-1 alpha-2',
    city                 STRING        COMMENT '城市',
    ip_hash              STRING        COMMENT 'IP 地址哈希（脱敏）',
    product_id           STRING        COMMENT '商品 ID（如有）',
    amount               DOUBLE        COMMENT '金额（如有）',
    environment          STRING        COMMENT 'prod|staging|dev',
    ingest_timestamp     TIMESTAMP     COMMENT 'Kafka 消费时间',
    processing_timestamp TIMESTAMP     COMMENT 'Glue Job 处理时间'
)
PARTITIONED BY (event_type)
LOCATION 's3://iodp-bronze-prod/clickstream/'
TBLPROPERTIES (
    'table_type'                        = 'ICEBERG',
    'format'                            = 'parquet',
    'write.parquet.compression-codec'   = 'snappy',
    'write.metadata.delete-after-commit.enabled' = 'true',
    'write.metadata.previous-versions-max'       = '10'
);
```

#### Silver `enriched_clicks` (`athena/ddl/silver_enriched_clicks.sql`)

```sql
-- athena/ddl/silver_enriched_clicks.sql
-- Silver 层去重后的点击流（结构与 Bronze 一致，但 event_id 保证唯一）

CREATE TABLE IF NOT EXISTS iodp_silver_prod.enriched_clicks (
    event_id             STRING        COMMENT '去重后唯一事件 ID',
    user_id              STRING        COMMENT '用户 ID（非空）',
    session_id           STRING,
    event_type           STRING,
    event_timestamp      TIMESTAMP     COMMENT '类型正确的事件时间',
    page_url             STRING,
    referrer_url         STRING,
    device_type          STRING,
    os                   STRING,
    browser              STRING,
    country_code         STRING,
    city                 STRING,
    ip_hash              STRING,
    product_id           STRING,
    amount               DOUBLE,
    environment          STRING,
    event_date           DATE          COMMENT '分区键 = event_timestamp::date',
    ingest_timestamp     TIMESTAMP,
    processing_timestamp TIMESTAMP
)
PARTITIONED BY (event_date, event_type)
LOCATION 's3://iodp-silver-prod/enriched_clicks/'
TBLPROPERTIES (
    'table_type'                        = 'ICEBERG',
    'format'                            = 'parquet',
    'write.parquet.compression-codec'   = 'snappy',
    'write.delete.mode'                 = 'merge-on-read',
    'write.update.mode'                 = 'merge-on-read'
);
```

#### Gold `api_error_stats` (`athena/ddl/gold_api_error_stats.sql`)

```sql
-- athena/ddl/gold_api_error_stats.sql
-- Gold 层 API 错误率聚合表

CREATE TABLE IF NOT EXISTS iodp_gold_prod.api_error_stats (
    stat_hour         TIMESTAMP    COMMENT '统计小时（整点，UTC）',
    service_name      STRING       COMMENT '服务名称',
    error_code        STRING       COMMENT '错误码',
    total_requests    BIGINT       COMMENT '该小时该服务总请求数',
    error_count       BIGINT       COMMENT '该错误码出现次数',
    error_rate        DOUBLE       COMMENT '错误率 = error_count / total_requests',
    p99_duration_ms   DOUBLE       COMMENT 'P99 响应时间（毫秒）',
    unique_users      BIGINT       COMMENT '受影响的去重用户数',
    sample_trace_ids  ARRAY<STRING> COMMENT '最多 5 个 trace_id 样本',
    environment       STRING,
    stat_date         DATE         COMMENT '分区键'
)
PARTITIONED BY (stat_date, service_name)
LOCATION 's3://iodp-gold-prod/api_error_stats/'
TBLPROPERTIES (
    'table_type'                        = 'ICEBERG',
    'format'                            = 'parquet',
    'write.parquet.compression-codec'   = 'snappy'
);
```

---

### 12.1 Athena 视图 — 用户会话聚合 (`athena/views/v_user_session.sql`)

```sql
-- athena/views/v_user_session.sql
-- 用户会话视图：把同一 session_id 的行为序列聚合成一行
-- 供 BI 工具分析用户路径和会话时长

CREATE OR REPLACE VIEW iodp_silver_prod.v_user_session AS
SELECT
    session_id,
    user_id,
    MIN(event_timestamp)                                        AS session_start,
    MAX(event_timestamp)                                        AS session_end,
    -- 会话时长（秒）
    DATE_DIFF('second', MIN(event_timestamp), MAX(event_timestamp)) AS session_duration_sec,
    COUNT(*)                                                    AS total_events,
    -- 漏斗标记：该 session 是否到达过各阶段
    MAX(CASE WHEN event_type = 'view'         THEN 1 ELSE 0 END) AS has_view,
    MAX(CASE WHEN event_type = 'add_to_cart'  THEN 1 ELSE 0 END) AS has_add_to_cart,
    MAX(CASE WHEN event_type = 'checkout'     THEN 1 ELSE 0 END) AS has_checkout,
    MAX(CASE WHEN event_type = 'purchase'     THEN 1 ELSE 0 END) AS has_purchase,
    -- 设备和地理信息（取 session 内第一条记录）
    MIN_BY(device_type,  event_timestamp)                      AS device_type,
    MIN_BY(country_code, event_timestamp)                      AS country_code,
    MIN_BY(city,         event_timestamp)                      AS city,
    -- 购买总金额
    SUM(CASE WHEN event_type = 'purchase' THEN amount ELSE 0 END) AS total_purchase_amount,
    CAST(event_timestamp AS DATE)                               AS event_date
FROM iodp_silver_prod.enriched_clicks
GROUP BY session_id, user_id, CAST(event_timestamp AS DATE)
;
```

---

### 12.1 Athena 视图：`v_error_log_enriched`

```sql
-- athena/views/v_error_log_enriched.sql
-- 供 Agent Log Analyzer 工具调用
-- 输入参数：user_id (可选), time_start, time_end, service_name (可选)

CREATE OR REPLACE VIEW iodp_gold_prod.v_error_log_enriched AS
SELECT
    aes.stat_hour,
    aes.service_name,
    aes.error_code,
    aes.error_rate,
    aes.error_count,
    aes.total_requests,
    aes.p99_duration_ms,
    aes.unique_users,
    aes.sample_trace_ids,
    -- 关联 Silver 层获取具体用户受影响情况
    sl.user_id,
    sl.req_path,
    sl.req_method,
    sl.http_status,
    sl.message         AS error_message,
    sl.stack_trace,
    sl.trace_id,
    sl.event_timestamp
FROM iodp_gold_prod.api_error_stats   aes
JOIN iodp_silver_prod.parsed_logs     sl
  ON  aes.service_name = sl.service_name
  AND aes.stat_hour    = DATE_TRUNC('hour', sl.event_timestamp)
  AND aes.error_code   = sl.error_code
WHERE sl.log_level IN ('ERROR', 'FATAL')
;
```

### 12.2 Agent 调用示例（伪 SQL）

```sql
-- Log Analyzer Agent 收到用户投诉："我昨晚 11 点支付失败"
-- 生成的 Athena 查询：

SELECT
    error_code,
    error_message,
    error_rate,
    sample_trace_ids,
    p99_duration_ms
FROM iodp_gold_prod.v_error_log_enriched
WHERE user_id       = 'usr_12345678'
  AND event_timestamp BETWEEN TIMESTAMP '2026-04-05 22:00:00'
                          AND TIMESTAMP '2026-04-06 00:00:00'
  AND service_name  = 'payment-service'
ORDER BY error_rate DESC
LIMIT 10;
```

### 12.3 DynamoDB 查询接口

Agent Log Analyzer 还可以查询 `dq_reports` 表以了解该时段数据质量：

```python
# Agent 工具代码片段（在 Agent.md 中详细展开）
dynamodb.query(
    TableName="iodp_dq_reports_prod",
    KeyConditionExpression="table_name = :t AND report_timestamp BETWEEN :s AND :e",
    ExpressionAttributeValues={
        ":t": "bronze_app_logs",
        ":s": "2026-04-05T22:00:00Z",
        ":e": "2026-04-06T00:00:00Z",
    }
)
```

---

> **下游文档**：详细阅读 [Agent.md](./Agent.md) 了解本数据层如何被多智能体系统消费，以及端到端诊断工作流的完整实现。

---

## 13. v2 架构改进（2026-04-06）

本节记录相对于初始设计的所有改进项及对应实现文件。

### 13.1 Bronze 分区键修正

**问题**：`stream_app_logs.py` 原来使用 `partitionedBy("service_name", "log_level")`。低流量服务（如告警服务）每 60 秒一个 mini-batch，会产生大量小文件（每 partition 可能 <1MB），导致 Athena 扫描文件数爆炸和 Glue Catalog 元数据膨胀。

**修正**：改为 `partitionedBy("event_date", "log_level")`，`service_name` 作为普通字段通过 Iceberg metadata filtering 过滤，而非分区键。

```python
# 修正前（产生小文件问题）
.partitionedBy("service_name", "log_level")

# 修正后（日期粒度分区）
valid_df = valid_df.withColumn("event_date", date_format(col("event_timestamp"), "yyyy-MM-dd"))
.partitionedBy("event_date", "log_level")
```

**实现文件**：[`glue_jobs/streaming/stream_app_logs.py`](../iodp-bigdata/glue_jobs/streaming/stream_app_logs.py)

---

### 13.2 DQ 阈值从全局统一改为按表可配置

**问题**：`DataQualityChecker` 的 `failure_threshold` 原来硬编码为 `0.05`，对所有表统一适用。不同表数据来源和质量要求差异很大，例如：
- `bronze_clickstream`：客户端事件，网络异常频繁，5% 容忍度合理
- `bronze_app_logs`：系统日志，直接影响 Agent 诊断准确性，应用更严格的 2% 阈值

**修正**：新增 `dq_threshold_config` DynamoDB 表，`DataQualityChecker` 构造时从该表按 `table_name` 加载阈值，失败时回退到构造参数的默认值。

```python
# 修正前（硬编码）
checker = DataQualityChecker(table_name="bronze_app_logs", failure_threshold=0.05, ...)

# 修正后（动态加载）
checker = DataQualityChecker(
    table_name="bronze_app_logs",
    failure_threshold=0.05,              # 兜底默认值
    threshold_config_table="iodp-dq-threshold-config-prod",  # 新增：从 DynamoDB 读取
)
```

| 表名 | 默认阈值 | 说明 |
|------|--------|------|
| `bronze_clickstream` | 5% | 客户端事件，容忍度较高 |
| `bronze_app_logs` | 2% | 系统日志，直接影响 Agent |
| `silver_parsed_logs` | 1% | 已过滤过一次，应更严格 |

**实现文件**：
- [`glue_jobs/lib/data_quality.py`](../iodp-bigdata/glue_jobs/lib/data_quality.py) — `load_threshold()` 静态方法
- [`terraform/modules/dynamodb/main.tf`](../iodp-bigdata/terraform/modules/dynamodb/main.tf) — `dq_threshold_config` 表 + 初始配置项

---

### 13.3 DLQ 死信数据重处理机制

**问题**：原设计只写死信（`s3://bronze/dead_letter/`），没有重处理路径。数据修复后只能手动复制，容易出错。

**修正**：新增 DLQ Replay Lambda + EventBridge 触发器（默认 DISABLED）。运维人员在修复数据问题后可手动启用，支持 `dry_run` 模式先预览再执行。

```bash
# 使用示例：dry-run 预览
aws lambda invoke \
  --function-name iodp-dlq-replay-prod \
  --payload '{"table_name":"bronze_app_logs","batch_date":"2026-04-06","dry_run":true}' \
  response.json

# 确认后实际执行
aws lambda invoke \
  --function-name iodp-dlq-replay-prod \
  --payload '{"table_name":"bronze_app_logs","batch_date":"2026-04-06","dry_run":false}' \
  response.json
```

**实现文件**：
- [`lambda/dlq_replay/handler.py`](../iodp-bigdata/lambda/dlq_replay/handler.py)
- [`terraform/modules/dlq_replay/main.tf`](../iodp-bigdata/terraform/modules/dlq_replay/main.tf)

---

### 13.4 事件驱动 OpenSearch 索引器

**问题**：原 `scripts/index_knowledge_base.py` 是每天凌晨 1 点的离线脚本。Gold 层新增 `incident_summary` 数据后最长需要等到次日凌晨才能被 RAG Agent 检索到（最多 24 小时延迟）。

**修正**：在 Gold S3 Bucket 上添加 `s3:ObjectCreated:*` 事件通知，filter prefix=`incident_summary/`，触发新的 OpenSearch Indexer Lambda，将 Parquet 文件实时向量化并写入 `incident_solutions` 索引。延迟从 24 小时降至 5 分钟以内。

```
Gold Job 写入 incident_summary/*.parquet
                    │
                    │ S3 Event Notification
                    ▼
         Lambda: opensearch_indexer
                    │
                    ▼
         ① 读取 Parquet (awswrangler)
         ② Bedrock Titan Embeddings V2
         ③ OpenSearch bulk index
                    │
                    ▼
         incident_solutions 索引更新（< 5 分钟）
```

**实现文件**：
- [`lambda/opensearch_indexer/handler.py`](../iodp-bigdata/lambda/opensearch_indexer/handler.py)
- [`terraform/modules/opensearch_indexer/main.tf`](../iodp-bigdata/terraform/modules/opensearch_indexer/main.tf)

---

### 13.5 新增单元测试

```
tests/unit/
├── conftest.py              # 公共 fixtures（mock DynamoDB、mock Spark DataFrame）
└── test_data_quality.py     # DataQualityChecker 完整测试集
    ├── TestLoadThreshold    # 5 个测试：DynamoDB 成功/缺项/网络故障/ClientError/兜底
    ├── TestRuleFactories    # 6 个测试：各规则工厂函数
    ├── TestWriteDqReport    # 2 个测试：DynamoDB 写入成功/失败静默
    └── TestThresholdBreach  # 4 个测试：阈值触发逻辑
```

运行：
```bash
cd iodp-bigdata
pytest tests/unit/test_data_quality.py -v
```

---

> **下游文档**：详细阅读 [Agent.md](./Agent.md) 了解本数据层如何被多智能体系统消费，以及端到端诊断工作流的完整实现。
