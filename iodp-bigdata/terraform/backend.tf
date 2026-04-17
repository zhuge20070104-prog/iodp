# terraform/backend.tf
# S3 远程 State + DynamoDB 锁，确保多人协作时状态一致

terraform {
  backend "s3" {
    bucket         = "iodp-terraform-state-prod"
    key            = "bigdata/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "iodp-terraform-locks"
    encrypt        = true
  }
}
