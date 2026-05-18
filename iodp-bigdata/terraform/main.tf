# terraform/main.tf
# 根模块，调用各子模块
#
# 架构（方案 D — Firehose 替代 MSK + Glue Streaming）：
#   ingestion (Firehose) → storage (Bronze) → compute (Glue Batch, hourly) → Silver → Gold
#   dynamodb → compute (DQ/Lineage 写入)
#   compute → observability (Glue Job 指标)
#   observability → vector_indexer (SNS topic 触发)
#   storage + dynamodb → dlq_replay / replay_jobs
#
# 核心删减：
#   - 删 networking 模块：无 VPC、无 NAT、无 IGW（Firehose 是托管公网服务）
#   - 删 streaming 模块：无 MSK Serverless（Firehose Direct PUT 取代）

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.mandatory_tags
  }
}

data "aws_caller_identity" "current" {}

# ════════════════════════════════════════════════════════════════
#  存储 + DynamoDB
# ════════════════════════════════════════════════════════════════

module "storage" {
  source = "./modules/storage"

  environment         = var.environment
  bronze_bucket_name  = "iodp-bronze-${var.environment}-${var.aws_account_id}"
  silver_bucket_name  = "iodp-silver-${var.environment}-${var.aws_account_id}"
  gold_bucket_name    = "iodp-gold-${var.environment}-${var.aws_account_id}"
  scripts_bucket_name = "iodp-glue-scripts-${var.environment}-${var.aws_account_id}"
  dead_letter_prefix  = "dead_letter/"
  # FinOps 生命周期
  ia_transition_days      = 30
  glacier_transition_days = 90
  expiration_days         = 365
  tags                    = local.mandatory_tags
}

module "dynamodb" {
  source = "./modules/dynamodb"

  environment = var.environment
  tags        = local.mandatory_tags
}

# ════════════════════════════════════════════════════════════════
#  数据入口：Kinesis Data Firehose（Direct PUT → S3 Bronze）
# ════════════════════════════════════════════════════════════════

module "ingestion" {
  source = "./modules/ingestion"

  environment         = var.environment
  bronze_bucket_arn   = module.storage.bronze_bucket_arn
  streams             = var.firehose_streams
  buffer_size_mb      = var.firehose_buffer_size_mb
  buffer_interval_sec = var.firehose_buffer_interval_sec
  tags                = local.mandatory_tags
}

# ════════════════════════════════════════════════════════════════
#  核心计算（Glue Batch — Silver/Gold 转换）
# ════════════════════════════════════════════════════════════════

module "compute" {
  source = "./modules/compute"

  environment                    = var.environment
  bronze_bucket_arn              = module.storage.bronze_bucket_arn
  bronze_bucket_name             = module.storage.bronze_bucket_name
  silver_bucket_arn              = module.storage.silver_bucket_arn
  silver_bucket_name             = module.storage.silver_bucket_name
  gold_bucket_arn                = module.storage.gold_bucket_arn
  gold_bucket_name               = module.storage.gold_bucket_name
  scripts_bucket_name            = module.storage.scripts_bucket_name
  dq_reports_table_arn           = module.dynamodb.dq_reports_table_arn
  dq_reports_table_name          = module.dynamodb.dq_reports_table_name
  lineage_table_arn              = module.dynamodb.lineage_events_table_arn
  lineage_table_name             = module.dynamodb.lineage_events_table_name
  dq_threshold_config_table_arn  = module.dynamodb.dq_threshold_config_table_arn
  dq_threshold_config_table_name = module.dynamodb.dq_threshold_config_table_name
  glue_catalog_id                = data.aws_caller_identity.current.account_id
  triggers_enabled               = var.triggers_enabled
  tags                           = local.mandatory_tags
}

# ════════════════════════════════════════════════════════════════
#  可观测性（Glue Job 指标 + Firehose 指标）
# ════════════════════════════════════════════════════════════════

module "observability" {
  source = "./modules/observability"

  environment              = var.environment
  aws_region               = var.aws_region
  glue_job_names           = module.compute.glue_job_names
  firehose_stream_names    = module.ingestion.delivery_stream_names
  dq_reports_table_name    = module.dynamodb.dq_reports_table_name
  alarm_email              = var.alarm_email
  tags                     = local.mandatory_tags
}

# ════════════════════════════════════════════════════════════════
#  增值功能模块（DLQ 重放、S3 Vectors 索引、Replay Jobs）
# ════════════════════════════════════════════════════════════════

module "dlq_replay" {
  source = "./modules/dlq_replay"

  environment        = var.environment
  bronze_bucket_name = module.storage.bronze_bucket_name
  tags               = local.mandatory_tags
}

module "replay_jobs" {
  source = "./modules/replay_jobs"

  environment        = var.environment
  bronze_bucket_name = module.storage.bronze_bucket_name
  scripts_bucket_name = module.storage.scripts_bucket_name
  lineage_table_name = module.dynamodb.lineage_events_table_name
  tags               = local.mandatory_tags
}

# vector_indexer 依赖 iodp-agent 创建的 S3 Vectors bucket。
# 部署顺序：
#   1) 首次部署 bigdata 时 vector_bucket_arn 为空，本模块跳过（count = 0）
#   2) 部署 iodp-agent 拿到 S3 Vectors bucket ARN
#   3) 回填 dev.tfvars 的 vector_bucket_arn / vector_bucket_name，重 apply bigdata 即创建本模块
module "vector_indexer" {
  source = "./modules/vector_indexer"
  count  = var.vector_bucket_arn != "" ? 1 : 0

  environment         = var.environment
  aws_region          = var.aws_region
  gold_bucket_name    = module.storage.gold_bucket_name
  vector_bucket_name  = var.vector_bucket_name
  vector_bucket_arn   = var.vector_bucket_arn
  vector_index_name   = var.vector_index_name
  sns_alert_topic_arn = module.observability.sns_alert_topic_arn
  llm_api_key         = var.llm_api_key
  tags                = local.mandatory_tags
}
