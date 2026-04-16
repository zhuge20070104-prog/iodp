output "api_endpoint" {
  description = "API Gateway HTTP API endpoint URL"
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "lambda_function_name" {
  value = aws_lambda_function.agent.function_name
}

output "opensearch_endpoint" {
  value = aws_opensearchserverless_collection.rag.collection_endpoint
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
