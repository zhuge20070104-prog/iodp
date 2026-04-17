output "glue_execution_role_arn" {
  value = aws_iam_role.glue_execution.arn
}

output "glue_job_names" {
  description = "All Glue job names (for observability module alarm setup)"
  value = [
    aws_glue_job.stream_clickstream.name,
    aws_glue_job.stream_app_logs.name,
    aws_glue_job.silver_enrich_clicks.name,
    aws_glue_job.silver_parse_logs.name,
    aws_glue_job.gold_hourly_active_users.name,
    aws_glue_job.gold_api_error_stats.name,
    aws_glue_job.gold_incident_summary.name,
  ]
}

output "bronze_database_name" {
  value = aws_glue_catalog_database.bronze.name
}

output "silver_database_name" {
  value = aws_glue_catalog_database.silver.name
}

output "gold_database_name" {
  value = aws_glue_catalog_database.gold.name
}
