# terraform/modules/storage/main.tf
# S3 存储桶 + 生命周期策略
#
# 设计：
#   - Bronze / Silver / Gold 三层桶，各有独立生命周期（FinOps）
#   - Scripts 桶存放 Glue PySpark 脚本，无生命周期
#   - 所有桶：KMS 加密 + Bucket Key（降低 API 成本）、禁止公开访问、开启版本控制

# ─── Bronze Bucket ───
resource "aws_s3_bucket" "bronze" {
  bucket = var.bronze_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "bronze" {
  bucket = aws_s3_bucket.bronze.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "bronze" {
  bucket = aws_s3_bucket.bronze.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true   # FinOps: 降低 KMS API 调用成本
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "bronze" {
  bucket = aws_s3_bucket.bronze.id

  # FinOps: 普通对象生命周期
  rule {
    id     = "bronze-standard-lifecycle"
    status = "Enabled"
    filter { prefix = "" }

    transition {
      days          = var.ia_transition_days      # 30 天 → Standard-IA
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = var.glacier_transition_days  # 90 天 → Glacier Instant Retrieval
      storage_class = "GLACIER_IR"
    }
    expiration {
      days = var.expiration_days                  # 365 天后删除
    }
    noncurrent_version_expiration {
      noncurrent_days = 30                        # 非当前版本 30 天后删除
    }
  }

  # FinOps: 死信区数据保留时间更短
  rule {
    id     = "dead-letter-lifecycle"
    status = "Enabled"
    filter { prefix = var.dead_letter_prefix }
    expiration { days = 30 }
  }
}

resource "aws_s3_bucket_public_access_block" "bronze" {
  bucket                  = aws_s3_bucket.bronze.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ─── Silver Bucket ───
resource "aws_s3_bucket" "silver" {
  bucket = var.silver_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "silver" {
  bucket = aws_s3_bucket.silver.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "silver" {
  bucket = aws_s3_bucket.silver.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "silver" {
  bucket = aws_s3_bucket.silver.id
  rule {
    id     = "silver-lifecycle"
    status = "Enabled"
    filter { prefix = "" }
    transition { days = 60;  storage_class = "STANDARD_IA" }
    transition { days = 180; storage_class = "GLACIER_IR"  }
    expiration { days = 730 }
  }
}

resource "aws_s3_bucket_public_access_block" "silver" {
  bucket                  = aws_s3_bucket.silver.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ─── Gold Bucket ───
resource "aws_s3_bucket" "gold" {
  bucket = var.gold_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "gold" {
  bucket = aws_s3_bucket.gold.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "gold" {
  bucket = aws_s3_bucket.gold.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "gold" {
  bucket = aws_s3_bucket.gold.id
  rule {
    id     = "gold-lifecycle"
    status = "Enabled"
    filter { prefix = "" }
    # Gold 层数据访问频率较高，延迟 IA 转换
    transition { days = 90;  storage_class = "STANDARD_IA" }
    transition { days = 270; storage_class = "GLACIER_IR"  }
    expiration { days = 1095 }   # 3 年（业务合规要求）
  }
}

resource "aws_s3_bucket_public_access_block" "gold" {
  bucket                  = aws_s3_bucket.gold.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ─── Glue Scripts Bucket（不需要生命周期，只需小存储）───
resource "aws_s3_bucket" "scripts" {
  bucket = var.scripts_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "scripts" {
  bucket = aws_s3_bucket.scripts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "scripts" {
  bucket                  = aws_s3_bucket.scripts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
