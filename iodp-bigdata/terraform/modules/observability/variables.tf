variable "environment" {
  description = "Deployment environment (prod / staging / dev)"
  type        = string
}

variable "msk_cluster_name" {
  description = "MSK cluster name for CloudWatch alarm dimensions"
  type        = string
}

variable "glue_job_names" {
  description = "List of Glue job names to monitor"
  type        = list(string)
}

variable "dq_reports_table_name" {
  description = "DynamoDB DQ reports table name (for dashboard reference)"
  type        = string
}

variable "alarm_email" {
  description = "Email address for alarm notifications via SNS"
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
