# environments/prod.tfvars
# 生产环境变量 — terraform plan -var-file=environments/prod.tfvars

environment    = "prod"
aws_region     = "us-east-1"
aws_account_id = "987654321098"   # 替换为实际 Account ID

# FinOps
cost_center = "engineering-data-platform"
team_owner  = "data-engineering@company.com"

# Ingestion (Firehose Direct PUT)
firehose_streams             = ["clickstream", "app_logs"]
firehose_buffer_size_mb      = 5
firehose_buffer_interval_sec = 60

# Observability
alarm_email = "data-engineering-oncall@company.com"

# S3 Vectors（vector bucket + indexes 由 iodp-agent 项目创建）
# 部署完 iodp-agent terraform 后，从其 outputs 复制以下两个值：
vector_bucket_name = "iodp-rag-prod"
vector_bucket_arn  = "arn:aws:s3vectors:us-east-1:987654321098:bucket/iodp-rag-prod"
vector_index_name  = "incident-solutions"
