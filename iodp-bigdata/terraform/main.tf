# terraform/main.tf
# 根模块，调用各子模块
#
# 模块依赖图：
#   networking → streaming (需要 VPC/子网/SG)
#   storage → compute (需要 bucket ARN/名称)
#   streaming → compute (需要 MSK ARN/bootstrap)
#   dynamodb → compute (需要 DQ/Lineage 表 ARN)
#   compute + streaming → observability (需要 job 名称/集群名)
#   observability → opensearch_indexer (需要 SNS topic ARN)
#   storage + dynamodb → dlq_replay / replay_jobs

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
#  核心基础设施模块
# ════════════════════════════════════════════════════════════════

module "networking" {
  source = "./modules/networking"

  environment        = var.environment
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
  tags               = local.mandatory_tags
}

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

module "streaming" {
  source = "./modules/streaming"

  environment        = var.environment
  cluster_name       = "iodp-msk-${var.environment}"
  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = [module.networking.msk_sg_id]
  kafka_topics       = var.kafka_topics
  tags               = local.mandatory_tags
}

module "dynamodb" {
  source = "./modules/dynamodb"

  environment = var.environment
  tags        = local.mandatory_tags
}

# ════════════════════════════════════════════════════════════════
#  核心计算模块（依赖 storage + streaming + dynamodb）
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
  msk_cluster_arn                = module.streaming.msk_cluster_arn
  msk_bootstrap_brokers          = module.streaming.bootstrap_brokers_sasl_iam
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
#  监控模块（依赖 compute + streaming）
# ════════════════════════════════════════════════════════════════

module "observability" {
  source = "./modules/observability"

  environment           = var.environment
  msk_cluster_name      = module.streaming.msk_cluster_name
  glue_job_names        = module.compute.glue_job_names
  dq_reports_table_name = module.dynamodb.dq_reports_table_name
  alarm_email           = var.alarm_email
  tags                  = local.mandatory_tags
}

# ════════════════════════════════════════════════════════════════
#  增值功能模块（DLQ 重放、OpenSearch 索引、Replay Jobs）
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

module "opensearch_indexer" {
  source = "./modules/opensearch_indexer"

  environment               = var.environment
  aws_region                = var.aws_region
  gold_bucket_name          = module.storage.gold_bucket_name
  opensearch_endpoint       = var.opensearch_endpoint
  opensearch_collection_arn = var.opensearch_collection_arn
  sns_alert_topic_arn       = module.observability.sns_alert_topic_arn
  tags                      = local.mandatory_tags
}
