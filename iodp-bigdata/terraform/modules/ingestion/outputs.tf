output "delivery_stream_names" {
  description = "All Firehose delivery stream names (for producer scripts)"
  value       = [for s in aws_kinesis_firehose_delivery_stream.stream : s.name]
}

output "delivery_stream_arns" {
  description = "All Firehose delivery stream ARNs (for IAM policies of producers)"
  value       = [for s in aws_kinesis_firehose_delivery_stream.stream : s.arn]
}

output "firehose_role_arn" {
  value = aws_iam_role.firehose.arn
}
