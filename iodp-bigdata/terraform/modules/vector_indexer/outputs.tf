output "lambda_arn" {
  value = aws_lambda_function.vector_indexer.arn
}

output "lambda_function_name" {
  value = aws_lambda_function.vector_indexer.function_name
}

output "dlq_url" {
  description = "SQS DLQ URL for failed indexing events"
  value       = aws_sqs_queue.indexer_dlq.url
}

output "dlq_arn" {
  value = aws_sqs_queue.indexer_dlq.arn
}
