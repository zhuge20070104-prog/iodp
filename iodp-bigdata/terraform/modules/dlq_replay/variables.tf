variable "environment" {
  type = string
}

variable "bronze_bucket_name" {
  description = "Name of the Bronze S3 bucket (without s3:// prefix)"
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
