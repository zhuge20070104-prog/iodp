# terraform/modules/replay_jobs/main.tf
# DLQ 死信数据重灌 Bronze 的 Glue Batch Jobs
#
# 设计：
#   - 两个独立 Glue Job：replay_app_logs / replay_clickstream
#   - 无定时 Trigger，仅支持 CLI 手动触发
#   - 读取 replay/ 目录下的 parquet，写回 Bronze Iceberg 表
#
# 手动触发示例：
#   aws glue start-job-run \
#     --job-name iodp-replay-app-logs-to-bronze-prod \
#     --arguments '{"--TABLE_NAME":"bronze_app_logs","--BATCH_DATE":"2026-04-06"}'

# ─── IAM Role（两个 Job 共用）───
resource "aws_iam_role" "replay_glue" {
  name = "iodp-replay-glue-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "replay_glue_service" {
  role       = aws_iam_role.replay_glue.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "replay_glue" {
  name = "iodp-replay-glue-policy-${var.environment}"
  role = aws_iam_role.replay_glue.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadReplay"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${var.bronze_bucket_name}",
          "arn:aws:s3:::${var.bronze_bucket_name}/replay/*",
        ]
      },
      {
        Sid    = "S3WriteBronze"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = [
          "arn:aws:s3:::${var.bronze_bucket_name}/app_logs/*",
          "arn:aws:s3:::${var.bronze_bucket_name}/clickstream/*",
        ]
      },
      {
        Sid    = "S3ReadBronzeMetadata"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${var.bronze_bucket_name}",
          "arn:aws:s3:::${var.bronze_bucket_name}/*/metadata/*",
        ]
      },
      {
        Sid    = "GlueCatalog"
        Effect = "Allow"
        Action = [
          "glue:GetDatabase", "glue:GetTable", "glue:GetPartitions",
          "glue:UpdateTable", "glue:CreateTable",
        ]
        Resource = [
          "arn:aws:glue:*:*:catalog",
          "arn:aws:glue:*:*:database/iodp_bronze_${var.environment}",
          "arn:aws:glue:*:*:table/iodp_bronze_${var.environment}/*",
        ]
      },
      {
        Sid    = "DynamoDBLineage"
        Effect = "Allow"
        Action = ["dynamodb:PutItem"]
        Resource = "arn:aws:dynamodb:*:*:table/${var.lineage_table_name}"
      },
      {
        Sid    = "S3ReadScripts"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.scripts_bucket_name}/*"
      },
    ]
  })
}

# ─── Glue Job: replay app_logs ───
resource "aws_glue_job" "replay_app_logs" {
  name     = "iodp-replay-app-logs-to-bronze-${var.environment}"
  role_arn = aws_iam_role.replay_glue.arn

  command {
    name            = "glueetl"
    script_location = "s3://${var.scripts_bucket_name}/batch/replay_app_logs_to_bronze.py"
    python_version  = "3"
  }

  glue_version      = "4.0"
  number_of_workers = 2
  worker_type       = "G.1X"

  default_arguments = {
    "--enable-auto-scaling"              = "true"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--job-bookmark-option"              = "job-bookmark-disable"
    "--extra-py-files"                   = "s3://${var.scripts_bucket_name}/lib.zip"
    "--BRONZE_BUCKET"                    = var.bronze_bucket_name
    "--LINEAGE_TABLE"                    = var.lineage_table_name
    "--ENVIRONMENT"                      = var.environment
    # TABLE_NAME 和 BATCH_DATE 由手动触发时传入
  }

  execution_property {
    max_concurrent_runs = 1
  }

  tags = var.tags
}

# ─── Glue Job: replay clickstream ───
resource "aws_glue_job" "replay_clickstream" {
  name     = "iodp-replay-clickstream-to-bronze-${var.environment}"
  role_arn = aws_iam_role.replay_glue.arn

  command {
    name            = "glueetl"
    script_location = "s3://${var.scripts_bucket_name}/batch/replay_clickstream_to_bronze.py"
    python_version  = "3"
  }

  glue_version      = "4.0"
  number_of_workers = 2
  worker_type       = "G.1X"

  default_arguments = {
    "--enable-auto-scaling"              = "true"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--job-bookmark-option"              = "job-bookmark-disable"
    "--extra-py-files"                   = "s3://${var.scripts_bucket_name}/lib.zip"
    "--BRONZE_BUCKET"                    = var.bronze_bucket_name
    "--LINEAGE_TABLE"                    = var.lineage_table_name
    "--ENVIRONMENT"                      = var.environment
  }

  execution_property {
    max_concurrent_runs = 1
  }

  tags = var.tags
}
