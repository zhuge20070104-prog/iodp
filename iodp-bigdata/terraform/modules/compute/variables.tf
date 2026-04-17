variable "environment" {
  description = "Deployment environment (prod / staging / dev)"
  type        = string
}

# ─── S3 ───

variable "bronze_bucket_arn" {
  description = "Bronze S3 bucket ARN"
  type        = string
}

variable "bronze_bucket_name" {
  description = "Bronze S3 bucket name (without s3:// prefix)"
  type        = string
}

variable "silver_bucket_arn" {
  description = "Silver S3 bucket ARN"
  type        = string
}

variable "silver_bucket_name" {
  description = "Silver S3 bucket name (without s3:// prefix)"
  type        = string
}

variable "gold_bucket_arn" {
  description = "Gold S3 bucket ARN"
  type        = string
}

variable "gold_bucket_name" {
  description = "Gold S3 bucket name (without s3:// prefix)"
  type        = string
}

variable "scripts_bucket_name" {
  description = "S3 bucket name for Glue job scripts"
  type        = string
}

# ─── MSK ───

variable "msk_cluster_arn" {
  description = "MSK Serverless cluster ARN"
  type        = string
}

variable "msk_bootstrap_brokers" {
  description = "MSK bootstrap brokers (IAM auth)"
  type        = string
  sensitive   = true
}

# ─── DynamoDB ───

variable "dq_reports_table_arn" {
  description = "DQ reports DynamoDB table ARN"
  type        = string
}

variable "dq_reports_table_name" {
  description = "DQ reports DynamoDB table name"
  type        = string
}

variable "lineage_table_arn" {
  description = "Lineage events DynamoDB table ARN"
  type        = string
}

variable "lineage_table_name" {
  description = "Lineage events DynamoDB table name"
  type        = string
}

variable "dq_threshold_config_table_arn" {
  description = "Per-table DQ threshold config DynamoDB table ARN"
  type        = string
}

variable "dq_threshold_config_table_name" {
  description = "Per-table DQ threshold config DynamoDB table name"
  type        = string
}

# ─── Glue Catalog ───

variable "glue_catalog_id" {
  description = "AWS account ID for Glue Catalog"
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ─── Triggers ───

variable "triggers_enabled" {
  description = "Whether Glue scheduled triggers are active. Keep false on first deploy until Athena DDL has created the Iceberg tables; flip to true afterwards."
  type        = bool
  default     = true
}
