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

variable "opensearch_endpoint" {
  description = "OpenSearch Serverless collection endpoint URL"
  type        = string
}

variable "opensearch_collection_arn" {
  description = "OpenSearch Serverless collection ARN (for IAM policy)"
  type        = string
}

variable "sns_alert_topic_arn" {
  description = "SNS topic ARN for CloudWatch alarms"
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
