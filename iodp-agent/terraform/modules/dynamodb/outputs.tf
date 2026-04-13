output "agent_state_table_name" {
  value = aws_dynamodb_table.agent_state.name
}

output "agent_state_table_arn" {
  value = aws_dynamodb_table.agent_state.arn
}

output "tickets_table_name" {
  value = aws_dynamodb_table.tickets.name
}

output "tickets_table_arn" {
  value = aws_dynamodb_table.tickets.arn
}

output "agent_jobs_table_name" {
  description = "Async job tracking table (NEW)"
  value       = aws_dynamodb_table.agent_jobs.name
}

output "agent_jobs_table_arn" {
  value = aws_dynamodb_table.agent_jobs.arn
}
