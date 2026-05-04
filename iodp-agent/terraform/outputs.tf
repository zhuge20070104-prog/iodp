output "api_endpoint" {
  description = "API Gateway HTTP API endpoint URL"
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "lambda_function_name" {
  value = aws_lambda_function.agent.function_name
}

output "vector_bucket_name" {
  description = "S3 Vectors bucket name (used by RAG agent + bigdata indexer)"
  value       = aws_s3vectors_vector_bucket.rag.vector_bucket_name
}

output "vector_bucket_arn" {
  description = "S3 Vectors bucket ARN (paste into iodp-bigdata tfvars as vector_bucket_arn)"
  value       = aws_s3vectors_vector_bucket.rag.arn
}

output "vector_index_incident_solutions" {
  description = "Vector index for incident solutions (written by bigdata vector_indexer Lambda)"
  value       = aws_s3vectors_index.incident_solutions.index_name
}

output "vector_index_product_docs" {
  description = "Vector index for product docs (written by index_knowledge_base.py)"
  value       = aws_s3vectors_index.product_docs.index_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.agent.repository_url
}

output "frontend_bucket" {
  description = "S3 bucket for React frontend static files"
  value       = aws_s3_bucket.frontend.bucket
}

output "frontend_url" {
  description = "CloudFront URL for the frontend"
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID (for cache invalidation after deploy)"
  value       = aws_cloudfront_distribution.frontend.id
}
