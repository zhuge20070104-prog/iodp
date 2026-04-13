# terraform/modules/opensearch_indexer/main.tf
# 事件驱动 OpenSearch 索引器
#
# 改进：原来是离线脚本每天凌晨手动触发，现在改为：
#   Gold S3 incident_summary/*.parquet 文件新增时 → S3 事件 → Lambda → OpenSearch
# 这样新的故障工单数据在写入 Gold 层后几分钟内就能被 RAG Agent 检索到。
#
# 构建步骤（Lambda ZIP 需手动构建）:
#   cd lambda/opensearch_indexer && pip install -r requirements.txt -t . && zip -r ../opensearch_indexer.zip .

locals {
  function_name = "iodp-opensearch-indexer-${var.environment}"
  zip_path      = "${path.module}/../../../lambda/opensearch_indexer.zip"
}

# ─── SQS DLQ（Lambda 失败时收集，避免静默丢失）───
resource "aws_sqs_queue" "indexer_dlq" {
  name                       = "iodp-opensearch-indexer-dlq-${var.environment}"
  message_retention_seconds  = 1209600  # 14 天
  visibility_timeout_seconds = 300

  tags = merge(var.tags, {
    Purpose = "DLQ for failed OpenSearch indexing events"
  })
}

# ─── IAM Role ───
resource "aws_iam_role" "opensearch_indexer" {
  name = "iodp-opensearch-indexer-role-${var.environment}"

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

resource "aws_iam_role_policy" "opensearch_indexer" {
  name = "iodp-opensearch-indexer-policy-${var.environment}"
  role = aws_iam_role.opensearch_indexer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadGold"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:HeadObject"]
        Resource = [
          "arn:aws:s3:::${var.gold_bucket_name}",
          "arn:aws:s3:::${var.gold_bucket_name}/incident_summary/*",
        ]
      },
      {
        Sid    = "BedrockEmbeddings"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
        ]
      },
      {
        Sid    = "OpenSearchServerless"
        Effect = "Allow"
        Action = ["aoss:APIAccessAll"]
        Resource = [var.opensearch_collection_arn]
      },
      {
        Sid    = "SQSDlq"
        Effect = "Allow"
        Action = ["sqs:SendMessage"]
        Resource = [aws_sqs_queue.indexer_dlq.arn]
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
resource "aws_lambda_function" "opensearch_indexer" {
  function_name = local.function_name
  role          = aws_iam_role.opensearch_indexer.arn
  runtime       = "python3.12"
  handler       = "handler.handler"
  timeout       = 300
  memory_size   = 1024  # 需要加载 Parquet 文件到内存

  filename         = local.zip_path
  source_code_hash = fileexists(local.zip_path) ? filebase64sha256(local.zip_path) : null

  # 防止并发过高压垮 OpenSearch Serverless
  reserved_concurrent_executions = 2

  dead_letter_config {
    target_arn = aws_sqs_queue.indexer_dlq.arn
  }

  environment {
    variables = {
      OPENSEARCH_ENDPOINT = var.opensearch_endpoint
      INDEX_NAME          = "incident_solutions"
      BEDROCK_REGION      = var.aws_region
      ENVIRONMENT         = var.environment
      BATCH_SIZE          = "50"
    }
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "opensearch_indexer" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 14
  tags              = var.tags
}

# ─── S3 Event Notification → Lambda ───
resource "aws_s3_bucket_notification" "gold_incident_summary" {
  bucket = var.gold_bucket_name

  lambda_function {
    lambda_function_arn = aws_lambda_function.opensearch_indexer.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "incident_summary/"
    filter_suffix       = ".parquet"
  }

  depends_on = [aws_lambda_permission.s3_invoke]
}

resource "aws_lambda_permission" "s3_invoke" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.opensearch_indexer.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = "arn:aws:s3:::${var.gold_bucket_name}"
}

# ─── CloudWatch 告警：索引失败率 ───
resource "aws_cloudwatch_metric_alarm" "indexer_errors" {
  alarm_name          = "iodp-opensearch-indexer-errors-${var.environment}"
  alarm_description   = "OpenSearch indexer Lambda error rate high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 3

  dimensions = {
    FunctionName = aws_lambda_function.opensearch_indexer.function_name
  }

  alarm_actions = [var.sns_alert_topic_arn]
  tags          = var.tags
}
