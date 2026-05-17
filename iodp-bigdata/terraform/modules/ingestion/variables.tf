variable "environment" {
  description = "Deployment environment (prod / staging / dev)"
  type        = string
}

variable "bronze_bucket_arn" {
  description = "Bronze S3 bucket ARN — Firehose writes GZip JSON here"
  type        = string
}

variable "streams" {
  description = "Logical stream names — one Firehose delivery stream per name. Use lowercase a-z0-9_-, e.g. clickstream / app_logs."
  type        = list(string)
  default     = ["clickstream", "app_logs"]
}

variable "buffer_size_mb" {
  description = "Firehose buffering size in MB (1–128). Flushes when EITHER size or interval is reached."
  type        = number
  default     = 5
}

variable "buffer_interval_sec" {
  description = "Firehose buffering interval in seconds (60–900)."
  type        = number
  default     = 60
}

variable "tags" {
  type    = map(string)
  default = {}
}
