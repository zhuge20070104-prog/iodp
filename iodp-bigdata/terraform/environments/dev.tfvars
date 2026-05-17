# environments/dev.tfvars
# 开发环境变量
#   - 由 Makefile 的 TF_VARS 自动通过 -var-file=environments/dev.tfvars 加载
#   - aws_region 和 environment 由 Makefile 用 -var 覆盖（命令行优先级最高），这里写啥都行
#   - 改完无需重 init，下次 apply 自动生效

environment    = "dev"
aws_region     = "ap-southeast-1"
aws_account_id = "165518479671"

# FinOps
cost_center = "engineering-data-platform"
team_owner  = "data-engineering@company.com"

# Ingestion (Firehose Direct PUT)
firehose_streams             = ["clickstream", "app_logs"]
firehose_buffer_size_mb      = 5
firehose_buffer_interval_sec = 60

# Observability
alarm_email = "zhuge20070104@gmail.com"

# S3 Vectors（dev 环境可选，留空则 indexer 模块创建但不连接真实 bucket）
# 部署完 iodp-agent terraform 后，从其 outputs 复制对应值。
vector_bucket_name = ""
vector_bucket_arn  = ""
vector_index_name  = "incident-solutions"
