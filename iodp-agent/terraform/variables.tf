variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "lambda_image_uri" {
  description = "ECR image URI for the agent Lambda function"
  type        = string
}

variable "bigdata_dq_reports_table_arn" {
  description = "ARN of the BigData DQ reports DynamoDB table (cross-project read)"
  type        = string
}

variable "bigdata_gold_bucket_arn" {
  description = "ARN of the BigData Gold S3 bucket (cross-project read)"
  type        = string
}

variable "cognito_user_pool_arn" {
  description = "Cognito User Pool ARN for API Gateway JWT authorizer (optional, leave empty to skip)"
  type        = string
  default     = ""
}

variable "athena_workgroup" {
  type    = string
  default = "primary"
}

variable "athena_result_bucket" {
  description = "S3 bucket for Athena query results"
  type        = string
}
