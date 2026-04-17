variable "environment" {
  description = "Deployment environment (prod / staging / dev)"
  type        = string
}

variable "cluster_name" {
  description = "MSK Serverless cluster name"
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for MSK (at least 2 AZs)"
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security group IDs for MSK cluster"
  type        = list(string)
}

variable "kafka_topics" {
  description = "List of Kafka topics to create via bootstrap script"
  type = list(object({
    name       = string
    partitions = number
    retention  = number  # hours
  }))
}

variable "tags" {
  type    = map(string)
  default = {}
}
