output "lambda_arn" {
  value = aws_lambda_function.dlq_replay.arn
}

output "lambda_function_name" {
  value = aws_lambda_function.dlq_replay.function_name
}

output "eventbridge_rule_name" {
  description = "EventBridge rule name. Enable this rule to trigger replay."
  value       = aws_cloudwatch_event_rule.dlq_replay_trigger.name
}
