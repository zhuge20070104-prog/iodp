# terraform/locals.tf
# FinOps 强制标签：所有资源必须携带，否则 Terraform plan 会因 variable validation 失败

locals {
  mandatory_tags = {
    Environment = var.environment        # "prod" | "dev" | "staging"
    CostCenter  = var.cost_center        # 例: "engineering-data-platform"
    Project     = "IODP-BigData"
    ManagedBy   = "Terraform"
    Owner       = var.team_owner         # 例: "data-engineering@company.com"
    DataClass   = "internal"             # 数据分级合规标签
    CreatedDate = formatdate("YYYY-MM-DD", timestamp())
  }
}
