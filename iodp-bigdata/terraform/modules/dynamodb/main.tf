# terraform/modules/dynamodb/main.tf
# BigData 层 DynamoDB 表
# 包含：dq_reports（DQ告警）、lineage_events（数据血缘）、dq_threshold_config（每表阈值配置）

# ─── 1. DQ 报告表 ───
resource "aws_dynamodb_table" "dq_reports" {
  name         = "iodp-dq-reports-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "table_name"
  range_key    = "report_timestamp"

  attribute {
    name = "table_name"
    type = "S"
  }
  attribute {
    name = "report_timestamp"
    type = "S"
  }

  ttl {
    attribute_name = "TTL"
    enabled        = true   # 90 天后自动删除，FinOps
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption  { enabled = true }

  tags = merge(var.tags, {
    Purpose = "DQ alerting reports for Agent Log Analyzer"
  })
}

# ─── 2. 数据血缘事件表 ───
resource "aws_dynamodb_table" "lineage_events" {
  name         = "iodp-lineage-events-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "source_table"
  range_key    = "event_time"

  attribute {
    name = "source_table"
    type = "S"
  }
  attribute {
    name = "event_time"
    type = "S"
  }

  ttl {
    attribute_name = "TTL"
    enabled        = true   # 180 天后自动删除
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption  { enabled = true }

  tags = merge(var.tags, {
    Purpose = "Data lineage tracking"
  })
}

# ─── 3. 每表 DQ 阈值配置表（NEW）───
# 改进：从统一的 5% 阈值改为按表可配置阈值
# 运营同学可通过 AWS Console / CLI 修改阈值，无需重新部署 Glue Job
resource "aws_dynamodb_table" "dq_threshold_config" {
  name         = "iodp-dq-threshold-config-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "table_name"

  attribute {
    name = "table_name"
    type = "S"
  }

  # 无 TTL：配置数据需要永久保留
  point_in_time_recovery { enabled = true }
  server_side_encryption  { enabled = true }

  tags = merge(var.tags, {
    Purpose     = "Per-table DQ failure threshold configuration"
    UpdatedBy   = "data-engineering"
  })
}

# ─── 预置默认阈值配置（通过 aws_dynamodb_table_item）───
# 这些是初始值，运营可通过 Console 覆盖
resource "aws_dynamodb_table_item" "threshold_bronze_clickstream" {
  table_name = aws_dynamodb_table.dq_threshold_config.name
  hash_key   = aws_dynamodb_table.dq_threshold_config.hash_key

  item = jsonencode({
    table_name        = { S = "bronze_clickstream" }
    failure_threshold = { N = "0.05" }   # 5%：点击流数据容忍度较高
    description       = { S = "User clickstream events - 5% tolerance for device/browser anomalies" }
    updated_at        = { S = "2026-04-06T00:00:00Z" }
    updated_by        = { S = "terraform-bootstrap" }
  })
}

resource "aws_dynamodb_table_item" "threshold_bronze_app_logs" {
  table_name = aws_dynamodb_table.dq_threshold_config.name
  hash_key   = aws_dynamodb_table.dq_threshold_config.hash_key

  item = jsonencode({
    table_name        = { S = "bronze_app_logs" }
    failure_threshold = { N = "0.02" }   # 2%：系统日志要求更严格
    description       = { S = "System application logs - strict 2% tolerance, used by Agent diagnosis" }
    updated_at        = { S = "2026-04-06T00:00:00Z" }
    updated_by        = { S = "terraform-bootstrap" }
  })
}

resource "aws_dynamodb_table_item" "threshold_silver_parsed_logs" {
  table_name = aws_dynamodb_table.dq_threshold_config.name
  hash_key   = aws_dynamodb_table.dq_threshold_config.hash_key

  item = jsonencode({
    table_name        = { S = "silver_parsed_logs" }
    failure_threshold = { N = "0.01" }   # 1%：Silver 层已过滤，允许更低失败率
    description       = { S = "Silver parsed logs - 1% tolerance (already DQ-filtered at Bronze)" }
    updated_at        = { S = "2026-04-06T00:00:00Z" }
    updated_by        = { S = "terraform-bootstrap" }
  })
}
