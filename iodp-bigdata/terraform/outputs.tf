# terraform/outputs.tf
# 关键资源输出，供 CI/CD 和 Agent 项目引用

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

# ─── Ingestion (Firehose) ───

output "firehose_delivery_streams" {
  description = "Firehose delivery stream names — producers use boto3 put_record against these"
  value       = module.ingestion.delivery_stream_names
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
