variable "environment" {
  type = string
}

variable "bronze_bucket_name" {
  description = "Name of the Bronze S3 bucket (without s3:// prefix)"
  type        = string
}

variable "scripts_bucket_name" {
  description = "Name of the S3 bucket containing Glue job scripts"
  type        = string
}

variable "lineage_table_name" {
  description = "Name of the DynamoDB lineage events table"
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
