# environments/dev.tfvars
# 开发环境变量 — terraform plan -var-file=environments/dev.tfvars

environment    = "dev"
aws_region     = "us-east-1"
aws_account_id = "123456789012"   # 替换为实际 Account ID

# FinOps
cost_center = "engineering-data-platform"
team_owner  = "data-engineering@company.com"

# Networking
vpc_cidr           = "10.0.0.0/16"
availability_zones = ["us-east-1a", "us-east-1b"]

# Streaming
kafka_topics = [
  { name = "user_clickstream", partitions = 2, retention = 72 },
  { name = "system_app_logs",  partitions = 2, retention = 72 },
]

# Observability
alarm_email = "data-engineering-dev@company.com"

# S3 Vectors（dev 环境可选，留空则 indexer 模块创建但不连接真实 bucket）
# 部署完 iodp-agent terraform 后，从其 outputs 复制对应值。
vector_bucket_name = ""
vector_bucket_arn  = ""
vector_index_name  = "incident_solutions"
