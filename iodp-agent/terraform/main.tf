# terraform/main.tf
# IODP Agent 完整基础设施
#
# 架构：API Gateway HTTP API + Lambda (Container) + DynamoDB + S3 Vectors
# 前端：S3 Static Hosting + CloudFront
#
# 最低成本方案：
#   - API Gateway HTTP API: $1/百万请求（比 REST API 便宜 70%）
#   - Lambda 按请求计费，无流量时零成本
#   - DynamoDB PAY_PER_REQUEST
#   - S3 Vectors 按 PUT/存储/查询计费，本量级月费 < $1（取代旧 OpenSearch Serverless 2 OCU 方案）
#   - S3 + CloudFront 静态托管 ~$1/月
#
# 注意：S3 Vectors 资源（aws_s3vectors_vector_bucket / aws_s3vectors_index）需要 AWS provider >= 6.5。

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.5"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "iodp-agent"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

locals {
  project_name = "iodp-agent"
  name_prefix  = "${local.project_name}-${var.environment}"
  tags = {
    Project     = "iodp-agent"
    Environment = var.environment
  }
}

# ═══════════════════════════════════════════════════════════════
# DynamoDB 模块（Agent State + Tickets + Jobs）
# ═══════════════════════════════════════════════════════════════

module "dynamodb" {
  source      = "./modules/dynamodb"
  environment = var.environment
  tags        = local.tags
}

# ═══════════════════════════════════════════════════════════════
# ECR 仓库（Lambda Container Image）
# ═══════════════════════════════════════════════════════════════

resource "aws_ecr_repository" "agent" {
  name                 = local.project_name
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.tags
}

# ═══════════════════════════════════════════════════════════════
# S3 Vectors（RAG 向量搜索 — GA 2025-12，取代 OpenSearch Serverless）
# ═══════════════════════════════════════════════════════════════
#
# 模型：1 个 vector bucket → N 个 index
#   - incident_solutions  ：bigdata vector_indexer Lambda 增量写入历史故障摘要
#   - product_docs        ：人工/离线脚本写入产品文档
#
# 元数据策略：
#   filterable     ：error_codes, doc_type, source_env  （查询时可作为过滤条件）
#   non-filterable ：title, content, resolution, ...    （仅 returnMetadata 时回传，更便宜）

resource "aws_s3vectors_vector_bucket" "rag" {
  vector_bucket_name = "iodp-rag-${var.environment}"
  force_destroy      = true

  tags = local.tags
}

resource "aws_s3vectors_index" "incident_solutions" {
  vector_bucket_name = aws_s3vectors_vector_bucket.rag.vector_bucket_name
  index_name         = "incident_solutions"
  data_type          = "float32"
  dimension          = 1024
  distance_metric    = "cosine"

  metadata_configuration {
    non_filterable_metadata_keys = [
      "title",
      "content",
      "resolution",
      "affected_service",
      "severity",
      "resolved_at",
      "incident_id",
    ]
  }

  tags = local.tags
}

resource "aws_s3vectors_index" "product_docs" {
  vector_bucket_name = aws_s3vectors_vector_bucket.rag.vector_bucket_name
  index_name         = "product_docs"
  data_type          = "float32"
  dimension          = 1024
  distance_metric    = "cosine"

  metadata_configuration {
    non_filterable_metadata_keys = [
      "title",
      "content",
      "created_at",
    ]
  }

  tags = local.tags
}

# ═══════════════════════════════════════════════════════════════
# IAM Role — Lambda 执行角色
# ═══════════════════════════════════════════════════════════════

resource "aws_iam_role" "lambda" {
  name = "${local.name_prefix}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_app" {
  name = "${local.name_prefix}-lambda-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBAgentTables"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
          "dynamodb:Query", "dynamodb:Scan", "dynamodb:DeleteItem",
        ]
        Resource = [
          module.dynamodb.agent_state_table_arn,
          module.dynamodb.tickets_table_arn,
          module.dynamodb.agent_jobs_table_arn,
          "${module.dynamodb.agent_jobs_table_arn}/index/*",
          "${module.dynamodb.tickets_table_arn}/index/*",
        ]
      },
      {
        Sid      = "DynamoDBBigDataRead"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = [var.bigdata_dq_reports_table_arn]
      },
      {
        Sid    = "AthenaQuery"
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution", "athena:GetQueryExecution",
          "athena:GetQueryResults", "athena:StopQueryExecution",
        ]
        Resource = ["arn:aws:athena:${var.aws_region}:*:workgroup/${var.athena_workgroup}"]
      },
      {
        Sid    = "AthenaResultBucket"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:GetBucketLocation"]
        Resource = [
          "arn:aws:s3:::${replace(var.athena_result_bucket, "s3://", "")}",
          "arn:aws:s3:::${replace(var.athena_result_bucket, "s3://", "")}/*",
        ]
      },
      {
        Sid      = "GlueCatalogRead"
        Effect   = "Allow"
        Action   = ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables"]
        Resource = ["arn:aws:glue:${var.aws_region}:*:catalog", "arn:aws:glue:${var.aws_region}:*:database/*", "arn:aws:glue:${var.aws_region}:*:table/*/*"]
      },
      {
        Sid      = "S3GoldBucketRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.bigdata_gold_bucket_arn, "${var.bigdata_gold_bucket_arn}/*"]
      },
      {
        Sid      = "BedrockInvoke"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = ["arn:aws:bedrock:${var.aws_region}::foundation-model/*"]
      },
      {
        Sid    = "S3VectorsAccess"
        Effect = "Allow"
        Action = [
          "s3vectors:QueryVectors",
          "s3vectors:GetVectors",
          "s3vectors:PutVectors",
          "s3vectors:DeleteVectors",
          "s3vectors:ListVectors",
        ]
        Resource = [
          aws_s3vectors_vector_bucket.rag.arn,
          "${aws_s3vectors_vector_bucket.rag.arn}/index/*",
        ]
      },
    ]
  })
}

# ═══════════════════════════════════════════════════════════════
# Lambda Function（Container Image）
# ═══════════════════════════════════════════════════════════════

resource "aws_lambda_function" "agent" {
  function_name = "${local.name_prefix}"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = var.lambda_image_uri
  timeout       = 900   # 15 分钟（Lambda 最大值，LangGraph 多轮调用可能较慢）
  memory_size   = 1024

  environment {
    variables = {
      IODP_AWS_REGION           = var.aws_region
      IODP_ENVIRONMENT          = var.environment
      IODP_AGENT_STATE_TABLE    = module.dynamodb.agent_state_table_name
      IODP_AGENT_JOBS_TABLE     = module.dynamodb.agent_jobs_table_name
      IODP_BUG_TICKETS_TABLE    = module.dynamodb.tickets_table_name
      IODP_DQ_REPORTS_TABLE     = replace(element(split("/", var.bigdata_dq_reports_table_arn), length(split("/", var.bigdata_dq_reports_table_arn)) - 1), "iodp_", "iodp-")
      IODP_VECTOR_BUCKET_NAME   = aws_s3vectors_vector_bucket.rag.vector_bucket_name
      IODP_ATHENA_WORKGROUP     = var.athena_workgroup
      IODP_ATHENA_RESULT_BUCKET = var.athena_result_bucket
    }
  }

  tags = local.tags
}

# ═══════════════════════════════════════════════════════════════
# API Gateway HTTP API（比 REST API 便宜 70%）
# ═══════════════════════════════════════════════════════════════

resource "aws_apigatewayv2_api" "agent" {
  name          = "${local.name_prefix}-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization"]
    max_age       = 3600
  }

  tags = local.tags
}

# Cognito JWT Authorizer（可选，cognito_user_pool_arn 为空时跳过）
resource "aws_apigatewayv2_authorizer" "cognito" {
  count = var.cognito_user_pool_arn != "" ? 1 : 0

  api_id           = aws_apigatewayv2_api.agent.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito-jwt"

  jwt_configuration {
    audience = ["iodp-agent-client"]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${element(split("/", var.cognito_user_pool_arn), length(split("/", var.cognito_user_pool_arn)) - 1)}"
  }
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.agent.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.agent.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_diagnose" {
  api_id    = aws_apigatewayv2_api.agent.id
  route_key = "POST /diagnose"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"

  authorization_type = var.cognito_user_pool_arn != "" ? "JWT" : "NONE"
  authorizer_id      = var.cognito_user_pool_arn != "" ? aws_apigatewayv2_authorizer.cognito[0].id : null
}

resource "aws_apigatewayv2_route" "get_diagnose" {
  api_id    = aws_apigatewayv2_api.agent.id
  route_key = "GET /diagnose/{job_id}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"

  authorization_type = var.cognito_user_pool_arn != "" ? "JWT" : "NONE"
  authorizer_id      = var.cognito_user_pool_arn != "" ? aws_apigatewayv2_authorizer.cognito[0].id : null
}

resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.agent.id
  route_key = "GET /health"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  # health 端点不需要认证
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.agent.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 50
    throttling_rate_limit  = 20
  }

  tags = local.tags
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.agent.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.agent.execution_arn}/*/*"
}

# ═══════════════════════════════════════════════════════════════
# S3 + CloudFront（React 前端静态托管）
# ═══════════════════════════════════════════════════════════════

resource "aws_s3_bucket" "frontend" {
  bucket        = "${local.name_prefix}-frontend"
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_identity" "frontend" {
  comment = "OAI for ${local.name_prefix} frontend"
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_cloudfront_origin_access_identity.frontend.iam_arn }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
    }]
  })
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"   # 最便宜：只用北美和欧洲边缘节点

  origin {
    domain_name = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id   = "S3Frontend"

    s3_origin_config {
      origin_access_identity = aws_cloudfront_origin_access_identity.frontend.cloudfront_access_identity_path
    }
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "S3Frontend"
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 86400
    max_ttl     = 31536000
  }

  # SPA fallback：所有 404 返回 index.html（React Router 处理路由）
  custom_error_response {
    error_code         = 403
    response_code      = 200
    response_page_path = "/index.html"
  }
  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = local.tags
}
