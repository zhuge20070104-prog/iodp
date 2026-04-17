# terraform/outputs.tf
# 关键资源输出，供 CI/CD 和 Agent 项目引用

# ─── Networking ───

output "vpc_id" {
  value = module.networking.vpc_id
}

output "private_subnet_ids" {
  value = module.networking.private_subnet_ids
}

# ─── Storage ───

output "bronze_bucket_name" {
  value = module.storage.bronze_bucket_name
}

output "silver_bucket_name" {
  value = module.storage.silver_bucket_name
}

output "gold_bucket_name" {
  value = module.storage.gold_bucket_name
}

output "scripts_bucket_name" {
  value = module.storage.scripts_bucket_name
}

# ─── Streaming ───

output "msk_cluster_arn" {
  value = module.streaming.msk_cluster_arn
}

output "msk_bootstrap_brokers" {
  value     = module.streaming.bootstrap_brokers_sasl_iam
  sensitive = true
}

# ─── DynamoDB ───

output "dq_reports_table_name" {
  value = module.dynamodb.dq_reports_table_name
}

output "lineage_events_table_name" {
  value = module.dynamodb.lineage_events_table_name
}

# ─── Compute ───

output "glue_job_names" {
  value = module.compute.glue_job_names
}

# ─── Observability ───

output "sns_alert_topic_arn" {
  value = module.observability.sns_alert_topic_arn
}
