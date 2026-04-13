# terraform/modules/dynamodb/main.tf (Agent 层)
# 包含：agent_state（LangGraph Checkpointer）、tickets（Bug 报告）、agent_jobs（异步 Job 跟踪）

# ─── 1. Agent 状态持久化表（LangGraph Checkpointer）───
resource "aws_dynamodb_table" "agent_state" {
  name         = "iodp-agent-state-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "thread_id"
  range_key    = "checkpoint_ns"

  attribute {
    name = "thread_id"
    type = "S"
  }
  attribute {
    name = "checkpoint_ns"
    type = "S"
  }

  ttl {
    attribute_name = "TTL"
    enabled        = true   # 7天后自动清理过期对话，FinOps
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption  { enabled = true }
  tags = var.tags
}

# ─── 2. Bug 报告工单表（Synthesizer 输出）───
resource "aws_dynamodb_table" "tickets" {
  name         = "iodp-bug-tickets-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "report_id"
  range_key    = "generated_at"

  attribute {
    name = "report_id"
    type = "S"
  }
  attribute {
    name = "generated_at"
    type = "S"
  }
  attribute {
    name = "severity"
    type = "S"
  }
  attribute {
    name = "affected_service"
    type = "S"
  }

  global_secondary_index {
    name               = "severity-service-index"
    hash_key           = "severity"
    range_key          = "affected_service"
    projection_type    = "ALL"
  }

  ttl {
    attribute_name = "TTL"
    enabled        = true
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption  { enabled = true }
  tags = var.tags
}

# ─── 3. 异步 Job 跟踪表（NEW — 支持异步 /diagnose 模式）───
# 客户端提交 POST /diagnose 后立即返回 job_id，
# 通过 GET /diagnose/{job_id} 轮询此表获取状态和结果。
resource "aws_dynamodb_table" "agent_jobs" {
  name         = "iodp-agent-jobs-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  # GSI：按 thread_id 查询历史 Job（多轮对话场景）
  attribute {
    name = "thread_id"
    type = "S"
  }

  global_secondary_index {
    name            = "thread-id-index"
    hash_key        = "thread_id"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "TTL"
    enabled        = true   # 1 小时后 Job 记录自动过期（由应用层设置 TTL）
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption  { enabled = true }

  tags = merge(var.tags, {
    Purpose = "Async diagnosis job tracking (POST /diagnose → GET /diagnose/{job_id})"
  })
}
