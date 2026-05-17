# terraform/modules/dlq_replay/main.tf
# DLQ 死信数据重处理模块
#
# 设计：
#   - Lambda 手动触发（或 EventBridge 一次性开启），读取 dead_letter/ 下的数据
#   - 支持 dry_run 模式，不写入数据只统计文件数
#   - EventBridge 规则默认 DISABLED，运维人员在重处理前手动 enable
#
# Lambda ZIP 由 terraform 用 archive_file data source 自动打包
# handler.py 无外部依赖（只用 boto3，Lambda runtime 自带）

locals {
  function_name = "iodp-dlq-replay-${var.environment}"
}

data "archive_file" "dlq_replay" {
  type        = "zip"
  source_file = "${path.module}/../../../lambda/dlq_replay/handler.py"
  output_path = "${path.module}/../../../lambda/dlq_replay.zip"
}

# ─── IAM Role ───
resource "aws_iam_role" "dlq_replay" {
  name = "iodp-dlq-replay-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "dlq_replay" {
  name = "iodp-dlq-replay-policy-${var.environment}"
  role = aws_iam_role.dlq_replay.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadDeadLetter"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket", "s3:HeadObject"]
        Resource = [
          "arn:aws:s3:::${var.bronze_bucket_name}",
          "arn:aws:s3:::${var.bronze_bucket_name}/dead_letter/*",
        ]
      },
      {
        Sid    = "S3WriteReplay"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "arn:aws:s3:::${var.bronze_bucket_name}/replay/*",
        ]
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:log-group:/aws/lambda/${local.function_name}:*"
      },
    ]
  })
}

# ─── Lambda Function ───
resource "aws_lambda_function" "dlq_replay" {
  function_name = local.function_name
  role          = aws_iam_role.dlq_replay.arn
  runtime       = "python3.12"
  handler       = "handler.handler"
  timeout       = 300   # 5 minutes — replay may need to copy many files
  memory_size   = 512

  filename         = data.archive_file.dlq_replay.output_path
  source_code_hash = data.archive_file.dlq_replay.output_base64sha256

  environment {
    variables = {
      BRONZE_BUCKET = var.bronze_bucket_name
      DLQ_PREFIX    = "dead_letter/"
      REPLAY_PREFIX = "replay/"
      ENVIRONMENT   = var.environment
    }
  }

  # 不设 reserved_concurrent_executions：新 AWS 账户的 Lambda unreserved 配额默认只有 10，
  # 任何 reserved 占用都会让剩余配额低于 AWS 的最低 10 限制。
  # 并发去重通过 EventBridge 默认 DISABLED + 手动单次触发实现，不依赖 reserved concurrency。

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "dlq_replay" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 14
  tags              = var.tags
}

# ─── EventBridge Rule（默认 DISABLED，运维手动启用进行一次性重放）───
resource "aws_cloudwatch_event_rule" "dlq_replay_trigger" {
  name                = "iodp-dlq-replay-trigger-${var.environment}"
  description         = "One-shot trigger for DLQ replay. Enable manually before use, disable after."
  schedule_expression = "rate(1 day)"   # 仅作占位，实际按需手动触发
  state               = "DISABLED"      # 默认关闭，避免意外重放

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "dlq_replay" {
  rule      = aws_cloudwatch_event_rule.dlq_replay_trigger.name
  target_id = "DlqReplayLambda"
  arn       = aws_lambda_function.dlq_replay.arn

  # 默认 payload：dry_run=true，运维启用时可覆盖
  input = jsonencode({
    table_name = "bronze_app_logs"
    batch_date = "YYYY-MM-DD"   # 运维需要在启用前修改此值
    dry_run    = true
  })
}

resource "aws_lambda_permission" "eventbridge_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dlq_replay.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.dlq_replay_trigger.arn
}
