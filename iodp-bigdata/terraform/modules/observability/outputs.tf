output "sns_alert_topic_arn" {
  description = "SNS topic ARN for alarm notifications (consumed by opensearch_indexer module)"
  value       = aws_sns_topic.alerts.arn
}

output "sns_alert_topic_name" {
  value = aws_sns_topic.alerts.name
}

output "dashboard_name" {
  value = aws_cloudwatch_dashboard.iodp.dashboard_name
}
