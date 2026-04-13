output "dq_reports_table_name" {
  value = aws_dynamodb_table.dq_reports.name
}

output "dq_reports_table_arn" {
  value = aws_dynamodb_table.dq_reports.arn
}

output "lineage_events_table_name" {
  value = aws_dynamodb_table.lineage_events.name
}

output "lineage_events_table_arn" {
  value = aws_dynamodb_table.lineage_events.arn
}

output "dq_threshold_config_table_name" {
  description = "Per-table DQ threshold configuration table (NEW)"
  value       = aws_dynamodb_table.dq_threshold_config.name
}

output "dq_threshold_config_table_arn" {
  description = "ARN for per-table DQ threshold configuration table"
  value       = aws_dynamodb_table.dq_threshold_config.arn
}
