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

# ─── Networking ───

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "AZs for subnet deployment (at least 2 for MSK)"
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

# ─── Streaming ───

variable "kafka_topics" {
  description = "MSK Kafka topic definitions"
  type = list(object({
    name       = string
    partitions = number
    retention  = number  # hours
  }))
  default = [
    { name = "user_clickstream",  partitions = 6, retention = 168 },
    { name = "system_app_logs",   partitions = 6, retention = 168 },
  ]
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
  default     = "incident_solutions"
}

# ─── Deployment orchestration ───

variable "triggers_enabled" {
  description = "Whether Glue scheduled triggers are active. Set false during first deploy so cron does not fire before Athena DDL has created Iceberg tables."
  type        = bool
  default     = true
}
