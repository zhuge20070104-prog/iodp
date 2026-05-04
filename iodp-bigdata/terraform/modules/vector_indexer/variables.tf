variable "environment" {
  type = string
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "gold_bucket_name" {
  description = "Gold S3 bucket name (event source)"
  type        = string
}

variable "vector_bucket_name" {
  description = "S3 Vectors bucket name (write target)"
  type        = string
}

variable "vector_bucket_arn" {
  description = "S3 Vectors bucket ARN (for IAM policy)"
  type        = string
}

variable "vector_index_name" {
  description = "S3 Vectors index name (default: incident_solutions)"
  type        = string
  default     = "incident_solutions"
}

variable "sns_alert_topic_arn" {
  description = "SNS topic ARN for CloudWatch alarms"
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
