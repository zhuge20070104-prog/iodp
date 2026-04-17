output "msk_cluster_arn" {
  value = aws_msk_serverless_cluster.main.arn
}

output "bootstrap_brokers_sasl_iam" {
  value     = aws_msk_serverless_cluster.main.bootstrap_brokers_sasl_iam
  sensitive = true
}

output "msk_cluster_name" {
  value = aws_msk_serverless_cluster.main.cluster_name
}
