variable "environment" {
  description = "Deployment environment (prod / staging / dev)"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "List of AZs (at least 2 for MSK Serverless)"
  type        = list(string)
}

variable "tags" {
  description = "Mandatory FinOps tags"
  type        = map(string)
  default     = {}
}
