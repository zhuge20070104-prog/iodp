variable "environment" {
  description = "Deployment environment (prod / staging / dev)"
  type        = string
  validation {
    condition     = contains(["prod", "staging", "dev"], var.environment)
    error_message = "environment must be prod, staging, or dev."
  }
}

variable "tags" {
  description = "Mandatory FinOps tags applied to all resources"
  type        = map(string)
  default     = {}
}
