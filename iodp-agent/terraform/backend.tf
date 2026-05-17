# terraform/backend.tf
# 显式声明 local backend：state 在 iodp-agent/terraform/terraform.tfstate
# 单人 dev 场景，简化运维，避免 S3 backend 的 lock / digest / migrate 各种坑。
# 生产环境再考虑切回 S3 + DynamoDB lock。

terraform {
  backend "local" {}
}
