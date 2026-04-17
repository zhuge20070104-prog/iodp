# environments/prod.tfvars
# 生产环境变量 — terraform plan -var-file=environments/prod.tfvars

environment    = "prod"
aws_region     = "us-east-1"
aws_account_id = "987654321098"   # 替换为实际 Account ID

# FinOps
cost_center = "engineering-data-platform"
team_owner  = "data-engineering@company.com"

# Networking
vpc_cidr           = "10.1.0.0/16"
availability_zones = ["us-east-1a", "us-east-1b", "us-east-1c"]

# Streaming
kafka_topics = [
  { name = "user_clickstream", partitions = 6, retention = 168 },
  { name = "system_app_logs",  partitions = 6, retention = 168 },
]

# Observability
alarm_email = "data-engineering-oncall@company.com"

# OpenSearch
opensearch_endpoint       = "https://xxxxxxxx.us-east-1.aoss.amazonaws.com"
opensearch_collection_arn = "arn:aws:aoss:us-east-1:987654321098:collection/xxxxxxxx"
