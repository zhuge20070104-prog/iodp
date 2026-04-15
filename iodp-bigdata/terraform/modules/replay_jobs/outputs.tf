output "replay_app_logs_job_name" {
  description = "Glue job name for replaying app_logs dead letter data to Bronze"
  value       = aws_glue_job.replay_app_logs.name
}

output "replay_clickstream_job_name" {
  description = "Glue job name for replaying clickstream dead letter data to Bronze"
  value       = aws_glue_job.replay_clickstream.name
}
