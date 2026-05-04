# terraform/modules/vector_indexer/main.tf
# 事件驱动 S3 Vectors 索引器（GA 2025-12）
#
# 架构：Gold S3 incident_summary/*.parquet 文件新增时
#       → S3 事件 → Lambda → Bedrock Embedding → S3 Vectors put_vectors
# 取代旧的 OpenSearch Serverless 方案，成本降低约 90%。
#
# 构建步骤（Lambda ZIP 需手动构建）:
#   cd lambda/vector_indexer && pip install -r requirements.txt -t . && zip -r ../vector_indexer.zip .

locals {
  function_name = "iodp-vector-indexer-${var.environment}"
  zip_path      = "${path.module}/../../../lambda/vector_indexer.zip"
}

# ─── SQS DLQ（Lambda 失败时收集，避免静默丢失）───
resource "aws_sqs_queue" "indexer_dlq" {
  name                       = "iodp-vector-indexer-dlq-${var.environment}"
  message_retention_seconds  = 1209600  # 14 天
  visibility_timeout_seconds = 300

  tags = merge(var.tags, {
    Purpose = "DLQ for failed S3 Vectors indexing events"
  })
}

# ─── IAM Role ───
resource "aws_iam_role" "vector_indexer" {
  name = "iodp-vector-indexer-role-${var.environment}"

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

resource "aws_iam_role_policy" "vector_indexer" {
  name = "iodp-vector-indexer-policy-${var.environment}"
  role = aws_iam_role.vector_indexer.id

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
        Sid    = "S3VectorsWrite"
        Effect = "Allow"
        Action = [
          "s3vectors:PutVectors",
          "s3vectors:GetVectors",
          "s3vectors:DeleteVectors",
          "s3vectors:ListVectors",
        ]
        Resource = [
          var.vector_bucket_arn,
          "${var.vector_bucket_arn}/index/*",
        ]
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
resource "aws_lambda_function" "vector_indexer" {
  function_name = local.function_name
  role          = aws_iam_role.vector_indexer.arn
  runtime       = "python3.12"
  handler       = "handler.handler"
  timeout       = 300
  memory_size   = 1024  # 需要加载 Parquet 文件到内存

  filename         = local.zip_path
  source_code_hash = fileexists(local.zip_path) ? filebase64sha256(local.zip_path) : null

  # S3 Vectors 是 storage-first 架构，写入限速远比 OpenSearch Serverless 宽松，
  # 但仍保留 reserved concurrency 以控制 Bedrock embedding 调用费率
  reserved_concurrent_executions = 2

  dead_letter_config {
    target_arn = aws_sqs_queue.indexer_dlq.arn
  }

  environment {
    variables = {
      VECTOR_BUCKET_NAME = var.vector_bucket_name
      INDEX_NAME         = var.vector_index_name
      BEDROCK_REGION     = var.aws_region
      ENVIRONMENT        = var.environment
      BATCH_SIZE         = "50"
    }
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "vector_indexer" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 14
  tags              = var.tags
}

# ─── S3 Event Notification → Lambda ───
resource "aws_s3_bucket_notification" "gold_incident_summary" {
  bucket = var.gold_bucket_name

  lambda_function {
    lambda_function_arn = aws_lambda_function.vector_indexer.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "incident_summary/"
    filter_suffix       = ".parquet"
  }

  depends_on = [aws_lambda_permission.s3_invoke]
}

resource "aws_lambda_permission" "s3_invoke" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.vector_indexer.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = "arn:aws:s3:::${var.gold_bucket_name}"
}

# ─── CloudWatch 告警：索引失败率 ───
resource "aws_cloudwatch_metric_alarm" "indexer_errors" {
  alarm_name          = "iodp-vector-indexer-errors-${var.environment}"
  alarm_description   = "S3 Vectors indexer Lambda error rate high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 3

  dimensions = {
    FunctionName = aws_lambda_function.vector_indexer.function_name
  }

  alarm_actions = [var.sns_alert_topic_arn]
  tags          = var.tags
}
