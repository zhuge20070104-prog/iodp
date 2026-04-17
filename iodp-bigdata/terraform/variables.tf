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

# ─── OpenSearch (for opensearch_indexer module) ───

variable "opensearch_endpoint" {
  description = "OpenSearch Serverless collection endpoint URL"
  type        = string
  default     = ""
}

variable "opensearch_collection_arn" {
  description = "OpenSearch Serverless collection ARN"
  type        = string
  default     = ""
}
