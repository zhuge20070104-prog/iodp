# terraform/modules/compute/main.tf
# Glue Jobs（Batch only）、Glue Catalog、IAM Roles
#
# 方案 D 调整：
#   - 删除 2 个 Glue Streaming Job（Firehose 已直接落 Bronze）
#   - 删除 MSK IAM 权限
#   - 保留 5 个 Batch Job（Silver × 2 + Gold × 3）
#   - Trigger 默认关闭（手动模式，FinOps）

# ─── Glue IAM Role ───
resource "aws_iam_role" "glue_execution" {
  name = "iodp-glue-execution-${var.environment}"

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

resource "aws_iam_role_policy" "glue_execution_policy" {
  name = "iodp-glue-policy-${var.environment}"
  role = aws_iam_role.glue_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadWrite"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [
          var.bronze_bucket_arn, "${var.bronze_bucket_arn}/*",
          var.silver_bucket_arn, "${var.silver_bucket_arn}/*",
          var.gold_bucket_arn,   "${var.gold_bucket_arn}/*",
          "arn:aws:s3:::${var.scripts_bucket_name}",
          "arn:aws:s3:::${var.scripts_bucket_name}/*"
        ]
      },
      {
        Sid    = "DynamoDBWriteDQAndLineage"
        Effect = "Allow"
        Action = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query"]
        Resource = [var.dq_reports_table_arn, var.lineage_table_arn, var.dq_threshold_config_table_arn]
      },
      {
        Sid    = "GlueCatalog"
        Effect = "Allow"
        Action = [
          "glue:GetDatabase", "glue:GetTable", "glue:CreateTable",
          "glue:UpdateTable", "glue:GetPartitions", "glue:BatchCreatePartition"
        ]
        Resource = [
          "arn:aws:glue:*:${var.glue_catalog_id}:catalog",
          "arn:aws:glue:*:${var.glue_catalog_id}:database/*",
          "arn:aws:glue:*:${var.glue_catalog_id}:table/*/*"
        ]
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:log-group:/aws-glue/*"
      }
    ]
  })
}

# ─── Glue Data Catalog Databases ───
# 显式传 tags 绕开 AWS provider v5 + default_tags 在 Glue Catalog Database 上的
# "Provider produced inconsistent final plan / tags_all" bug。
resource "aws_glue_catalog_database" "bronze" {
  name        = "iodp_bronze_${var.environment}"
  description = "IODP Bronze Layer - Raw ingested data (Firehose GZip JSON)"
  tags        = var.tags
}

resource "aws_glue_catalog_database" "silver" {
  name        = "iodp_silver_${var.environment}"
  description = "IODP Silver Layer - Cleaned and enriched data"
  tags        = var.tags
}

resource "aws_glue_catalog_database" "gold" {
  name        = "iodp_gold_${var.environment}"
  description = "IODP Gold Layer - Business aggregates, consumed by Agent"
  tags        = var.tags
}

# ════════════════════════════════════════════════════════════════
#  Batch Jobs — Silver 层（Bronze → Silver）
# ════════════════════════════════════════════════════════════════

resource "aws_glue_job" "silver_enrich_clicks" {
  name     = "iodp-silver-enrich-clicks-${var.environment}"
  role_arn = aws_iam_role.glue_execution.arn

  command {
    name            = "glueetl"
    script_location = "s3://${var.scripts_bucket_name}/batch/silver_enrich_clicks.py"
    python_version  = "3"
  }

  glue_version      = "4.0"
  number_of_workers = 2
  worker_type       = "G.1X"

  default_arguments = {
    "--enable-auto-scaling"              = "true"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--job-bookmark-option"              = "job-bookmark-enable"
    "--extra-py-files"                   = "s3://${var.scripts_bucket_name}/lib.zip"
    "--BRONZE_BUCKET"                    = "s3://${var.bronze_bucket_name}/"
    "--BRONZE_PREFIX"                    = "clickstream/"
    "--SILVER_BUCKET"                    = "s3://${var.silver_bucket_name}/"
    "--LINEAGE_TABLE"                    = var.lineage_table_name
    "--DQ_TABLE"                         = var.dq_reports_table_name
    "--DQ_THRESHOLD_TABLE"               = var.dq_threshold_config_table_name
    "--GLUE_DATABASE_BRONZE"             = aws_glue_catalog_database.bronze.name
    "--GLUE_DATABASE_SILVER"             = aws_glue_catalog_database.silver.name
    "--ENVIRONMENT"                      = var.environment
  }

  tags = var.tags
}

resource "aws_glue_job" "silver_parse_logs" {
  name     = "iodp-silver-parse-logs-${var.environment}"
  role_arn = aws_iam_role.glue_execution.arn

  command {
    name            = "glueetl"
    script_location = "s3://${var.scripts_bucket_name}/batch/silver_parse_logs.py"
    python_version  = "3"
  }

  glue_version      = "4.0"
  number_of_workers = 2
  worker_type       = "G.1X"

  default_arguments = {
    "--enable-auto-scaling"              = "true"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--job-bookmark-option"              = "job-bookmark-enable"
    "--extra-py-files"                   = "s3://${var.scripts_bucket_name}/lib.zip"
    "--BRONZE_BUCKET"                    = "s3://${var.bronze_bucket_name}/"
    "--BRONZE_PREFIX"                    = "app_logs/"
    "--SILVER_BUCKET"                    = "s3://${var.silver_bucket_name}/"
    "--LINEAGE_TABLE"                    = var.lineage_table_name
    "--DQ_TABLE"                         = var.dq_reports_table_name
    "--DQ_THRESHOLD_TABLE"               = var.dq_threshold_config_table_name
    "--GLUE_DATABASE_BRONZE"             = aws_glue_catalog_database.bronze.name
    "--GLUE_DATABASE_SILVER"             = aws_glue_catalog_database.silver.name
    "--ENVIRONMENT"                      = var.environment
  }

  tags = var.tags
}

# ════════════════════════════════════════════════════════════════
#  Batch Jobs — Gold 层（Silver → Gold）
# ════════════════════════════════════════════════════════════════

resource "aws_glue_job" "gold_hourly_active_users" {
  name     = "iodp-gold-hourly-active-users-${var.environment}"
  role_arn = aws_iam_role.glue_execution.arn

  command {
    name            = "glueetl"
    script_location = "s3://${var.scripts_bucket_name}/batch/gold_hourly_active_users.py"
    python_version  = "3"
  }

  glue_version      = "4.0"
  number_of_workers = 2
  worker_type       = "G.1X"

  default_arguments = {
    "--enable-auto-scaling"              = "true"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--job-bookmark-option"              = "job-bookmark-enable"
    "--extra-py-files"                   = "s3://${var.scripts_bucket_name}/lib.zip"
    "--SILVER_BUCKET"                    = "s3://${var.silver_bucket_name}/"
    "--GOLD_BUCKET"                      = "s3://${var.gold_bucket_name}/"
    "--GLUE_DATABASE_SILVER"             = aws_glue_catalog_database.silver.name
    "--GLUE_DATABASE_GOLD"               = aws_glue_catalog_database.gold.name
    "--ENVIRONMENT"                      = var.environment
  }

  tags = var.tags
}

resource "aws_glue_job" "gold_api_error_stats" {
  name     = "iodp-gold-api-error-stats-${var.environment}"
  role_arn = aws_iam_role.glue_execution.arn

  command {
    name            = "glueetl"
    script_location = "s3://${var.scripts_bucket_name}/batch/gold_api_error_stats.py"
    python_version  = "3"
  }

  glue_version      = "4.0"
  number_of_workers = 2
  worker_type       = "G.1X"

  default_arguments = {
    "--enable-auto-scaling"              = "true"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--job-bookmark-option"              = "job-bookmark-enable"
    "--extra-py-files"                   = "s3://${var.scripts_bucket_name}/lib.zip"
    "--SILVER_BUCKET"                    = "s3://${var.silver_bucket_name}/"
    "--GOLD_BUCKET"                      = "s3://${var.gold_bucket_name}/"
    "--GLUE_DATABASE_SILVER"             = aws_glue_catalog_database.silver.name
    "--GLUE_DATABASE_GOLD"               = aws_glue_catalog_database.gold.name
    "--ENVIRONMENT"                      = var.environment
  }

  tags = var.tags
}

resource "aws_glue_job" "gold_incident_summary" {
  name     = "iodp-gold-incident-summary-${var.environment}"
  role_arn = aws_iam_role.glue_execution.arn

  command {
    name            = "glueetl"
    script_location = "s3://${var.scripts_bucket_name}/batch/gold_incident_summary.py"
    python_version  = "3"
  }

  glue_version      = "4.0"
  number_of_workers = 2
  worker_type       = "G.1X"

  default_arguments = {
    "--enable-auto-scaling"              = "true"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
    "--job-bookmark-option"              = "job-bookmark-enable"
    "--extra-py-files"                   = "s3://${var.scripts_bucket_name}/lib.zip"
    "--SILVER_BUCKET"                    = "s3://${var.silver_bucket_name}/"
    "--GOLD_BUCKET"                      = "s3://${var.gold_bucket_name}/"
    "--LINEAGE_TABLE"                    = var.lineage_table_name
    "--GLUE_DATABASE_SILVER"             = aws_glue_catalog_database.silver.name
    "--GLUE_DATABASE_GOLD"               = aws_glue_catalog_database.gold.name
    "--ENVIRONMENT"                      = var.environment
  }

  tags = var.tags
}

# ════════════════════════════════════════════════════════════════
#  Glue Triggers（定时调度，默认关闭 — FinOps 手动模式）
# ════════════════════════════════════════════════════════════════

# Silver 层：每小时运行
resource "aws_glue_trigger" "silver_hourly" {
  name     = "iodp-silver-hourly-trigger-${var.environment}"
  type     = "SCHEDULED"
  schedule = "cron(5 * * * ? *)"   # 每小时第 5 分钟
  enabled  = var.triggers_enabled

  actions {
    job_name = aws_glue_job.silver_enrich_clicks.name
  }
  actions {
    job_name = aws_glue_job.silver_parse_logs.name
  }

  tags = var.tags
}

# Gold 层：每小时运行
resource "aws_glue_trigger" "gold_hourly" {
  name     = "iodp-gold-hourly-trigger-${var.environment}"
  type     = "SCHEDULED"
  schedule = "cron(15 * * * ? *)"  # 每小时第 15 分钟（等 Silver 完成）
  enabled  = var.triggers_enabled

  actions {
    job_name = aws_glue_job.gold_hourly_active_users.name
  }
  actions {
    job_name = aws_glue_job.gold_api_error_stats.name
  }

  tags = var.tags
}

# Gold incident_summary：每天凌晨运行
resource "aws_glue_trigger" "gold_daily" {
  name     = "iodp-gold-daily-trigger-${var.environment}"
  type     = "SCHEDULED"
  schedule = "cron(0 2 * * ? *)"   # UTC 02:00
  enabled  = var.triggers_enabled

  actions {
    job_name = aws_glue_job.gold_incident_summary.name
  }

  tags = var.tags
}
