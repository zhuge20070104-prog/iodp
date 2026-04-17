output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "msk_sg_id" {
  description = "Security group ID for MSK Serverless cluster"
  value       = aws_security_group.msk.id
}

output "glue_sg_id" {
  description = "Security group ID for Glue Jobs"
  value       = aws_security_group.glue.id
}
