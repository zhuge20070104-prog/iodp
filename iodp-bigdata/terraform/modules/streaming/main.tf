# terraform/modules/streaming/main.tf
# MSK Serverless 集群
#
# 设计：
#   - MSK Serverless：免运维，按使用量计费，无需管理 broker
#   - IAM 认证（端口 9098），无需管理 SASL/SCRAM 密码
#   - Topic 通过 bootstrap 脚本创建（MSK Serverless 不支持 aws_msk_topic 资源）

resource "aws_msk_serverless_cluster" "main" {
  cluster_name = var.cluster_name

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

  client_authentication {
    sasl {
      iam { enabled = true }   # 使用 IAM 认证，无需管理密码
    }
  }

  tags = var.tags
}

# ─── Kafka Topics（通过 bootstrap 脚本创建）───
# MSK Serverless 不直接支持 aws_msk_topic 资源，使用 null_resource + bootstrap 脚本
resource "null_resource" "create_topics" {
  depends_on = [aws_msk_serverless_cluster.main]

  triggers = {
    topics = join(",", [for t in var.kafka_topics : t.name])
  }

  provisioner "local-exec" {
    command = <<-EOT
      bash ${path.module}/../../scripts/bootstrap_kafka_topics.sh \
        --bootstrap-servers "${aws_msk_serverless_cluster.main.bootstrap_brokers_sasl_iam}" \
        --topics '${jsonencode(var.kafka_topics)}'
    EOT
  }
}
