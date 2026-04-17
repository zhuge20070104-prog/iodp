variable "environment" {
  description = "Deployment environment (prod / staging / dev)"
  type        = string
}

variable "bronze_bucket_name" {
  description = "S3 bucket name for Bronze layer"
  type        = string
}

variable "silver_bucket_name" {
  description = "S3 bucket name for Silver layer"
  type        = string
}

variable "gold_bucket_name" {
  description = "S3 bucket name for Gold layer"
  type        = string
}

variable "scripts_bucket_name" {
  description = "S3 bucket name for Glue job scripts"
  type        = string
}

variable "dead_letter_prefix" {
  description = "S3 prefix for dead letter data (shorter retention)"
  type        = string
  default     = "dead_letter/"
}

# ─── FinOps Lifecycle Settings ───

variable "ia_transition_days" {
  description = "Days before transitioning to Standard-IA"
  type        = number
  default     = 30
}

variable "glacier_transition_days" {
  description = "Days before transitioning to Glacier Instant Retrieval"
  type        = number
  default     = 90
}

variable "expiration_days" {
  description = "Days before object expiration (Bronze layer)"
  type        = number
  default     = 365
}

variable "tags" {
  type    = map(string)
  default = {}
}
