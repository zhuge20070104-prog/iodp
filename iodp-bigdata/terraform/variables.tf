# terraform/variables.tf
# 全局变量，由 environments/*.tfvars 注入

variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "aws_account_id" {
  description = "AWS account ID (used in S3 bucket naming to ensure uniqueness)"
  type        = string
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  validation {
    condition     = contains(["prod", "staging", "dev"], var.environment)
    error_message = "environment must be prod, staging, or dev."
  }
}

variable "cost_center" {
  description = "FinOps cost center tag"
  type        = string
  default     = "engineering-data-platform"
}

variable "team_owner" {
  description = "Team owner email for tagging"
  type        = string
  default     = "data-engineering@company.com"
}

# ─── Ingestion (Kinesis Data Firehose) ───

variable "firehose_streams" {
  description = "Logical stream names — one Firehose delivery stream per name."
  type        = list(string)
  default     = ["clickstream", "app_logs"]
}

variable "firehose_buffer_size_mb" {
  description = "Firehose buffering size in MB before flushing to S3."
  type        = number
  default     = 5
}

variable "firehose_buffer_interval_sec" {
  description = "Firehose buffering interval in seconds before flushing to S3."
  type        = number
  default     = 60
}

# ─── Observability ───

variable "alarm_email" {
  description = "Email address for CloudWatch alarm notifications"
  type        = string
}

# ─── S3 Vectors (for vector_indexer module) ───
# 物理资源（vector bucket + indexes）由 iodp-agent 项目的 Terraform 创建并输出，
# 这里通过 tfvars 接收名称/ARN 引用。

variable "vector_bucket_name" {
  description = "S3 Vectors bucket name (created by iodp-agent terraform)"
  type        = string
  default     = ""
}

variable "vector_bucket_arn" {
  description = "S3 Vectors bucket ARN (created by iodp-agent terraform)"
  type        = string
  default     = ""
}

variable "vector_index_name" {
  description = "S3 Vectors index name written by the indexer Lambda"
  type        = string
  default     = "incident-solutions"
}

# ─── Deployment orchestration ───

variable "triggers_enabled" {
  description = <<-EOT
    Whether Glue scheduled triggers are active.

    Default is FALSE (manual-trigger mode) for FinOps reasons — auto-running
    Silver hourly + Gold hourly + Gold daily costs ~$35/week even when idle.
    In demo/dev usage, prefer manual invocation via `make demo-pipeline`.

    Set to true only when you need 24x7 fresh data (true production usage).
  EOT
  type        = bool
  default     = false
}
