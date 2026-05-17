# terraform/modules/ingestion/main.tf
# Kinesis Data Firehose — 流式数据入口
#
# 设计：
#   - Direct PUT 模式：Producer 用 boto3.client("firehose").put_record() 直接写
#     （取代 MSK Serverless + Kafka client SDK），完全无 VPC、无 broker
#   - 2 个 delivery stream：clickstream / app_logs
#   - 缓冲 60s 或 5 MB（满任一即 flush 到 S3），延迟与 Glue Streaming 接近
#   - 格式：GZip JSON，按 year/month/day/hour 动态分区，落到 Bronze bucket
#   - 失败投递：错误前缀 errors/<stream>/ 隔离，CloudWatch Logs 留痕
#
# FinOps：Firehose 计费按 GB ingest（$0.029/GB），演示流量 ~$1/周。
#         对比 MSK Serverless $130/周 + Glue Streaming $296/周，省 ~99%。

# ─── IAM Role：Firehose 写 Bronze + Log ───
resource "aws_iam_role" "firehose" {
  name = "iodp-firehose-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "firehose" {
  name = "iodp-firehose-policy-${var.environment}"
  role = aws_iam_role.firehose.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3BronzeWrite"
        Effect = "Allow"
        Action = [
          "s3:AbortMultipartUpload", "s3:GetBucketLocation",
          "s3:GetObject", "s3:ListBucket", "s3:ListBucketMultipartUploads",
          "s3:PutObject",
        ]
        Resource = [
          var.bronze_bucket_arn,
          "${var.bronze_bucket_arn}/*",
        ]
      },
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:PutLogEvents"]
        Resource = ["arn:aws:logs:*:*:log-group:/aws/kinesisfirehose/*:log-stream:*"]
      },
    ]
  })
}

# ─── CloudWatch Log Group（Firehose 故障投递日志）───
resource "aws_cloudwatch_log_group" "firehose" {
  for_each          = toset(var.streams)
  name              = "/aws/kinesisfirehose/iodp-${each.key}-${var.environment}"
  retention_in_days = 14
  tags              = var.tags
}

resource "aws_cloudwatch_log_stream" "firehose" {
  for_each       = toset(var.streams)
  name           = "S3Delivery"
  log_group_name = aws_cloudwatch_log_group.firehose[each.key].name
}

# ─── Firehose Delivery Streams ───
resource "aws_kinesis_firehose_delivery_stream" "stream" {
  for_each    = toset(var.streams)
  name        = "iodp-${each.key}-${var.environment}"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose.arn
    bucket_arn = var.bronze_bucket_arn

    # 数据落地：bronze/<stream>/year=YYYY/month=MM/day=DD/hour=HH/
    prefix              = "${each.key}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/"
    error_output_prefix = "errors/${each.key}/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"

    # 缓冲：5 MB 或 60s 满任一即 flush（演示场景延迟低）
    buffering_size     = var.buffer_size_mb
    buffering_interval = var.buffer_interval_sec

    compression_format = "GZIP"

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose[each.key].name
      log_stream_name = aws_cloudwatch_log_stream.firehose[each.key].name
    }
  }

  tags = var.tags
}
