variable "environment" {
  description = "Deployment environment (prod / staging / dev)"
  type        = string
}

variable "aws_region" {
  description = "AWS region — required by CloudWatch dashboard widgets"
  type        = string
}

variable "firehose_stream_names" {
  description = "Firehose delivery stream names to monitor (replaces MSK consumer-lag alarms)"
  type        = list(string)
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
