# terraform/modules/networking/main.tf
# VPC、子网、安全组
#
# 设计：
#   - 2 个 AZ，各 1 个 Public + 1 个 Private 子网
#   - MSK Serverless 和 Glue Job 运行在 Private 子网
#   - NAT Gateway 供 Private 子网访问外网（拉取依赖、调用 AWS API）
#   - 安全组按最小权限原则：MSK 仅开放 9098（IAM Auth），Glue 仅出站

# ─── VPC ───
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "iodp-vpc-${var.environment}" })
}

# ─── Public Subnets（NAT Gateway 所在子网）───
resource "aws_subnet" "public" {
  count = length(var.availability_zones)

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)  # /24
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = true

  tags = merge(var.tags, {
    Name = "iodp-public-${var.availability_zones[count.index]}-${var.environment}"
    Tier = "public"
  })
}

# ─── Private Subnets（MSK / Glue 运行区）───
resource "aws_subnet" "private" {
  count = length(var.availability_zones)

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 100)  # /24, offset 避免冲突
  availability_zone = var.availability_zones[count.index]

  tags = merge(var.tags, {
    Name = "iodp-private-${var.availability_zones[count.index]}-${var.environment}"
    Tier = "private"
  })
}

# ─── Internet Gateway ───
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge(var.tags, { Name = "iodp-igw-${var.environment}" })
}

# ─── Elastic IP for NAT Gateway ───
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = merge(var.tags, { Name = "iodp-nat-eip-${var.environment}" })
}

# ─── NAT Gateway（单 AZ，FinOps: 省成本；prod 可扩展为每 AZ 一个）───
resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id

  tags = merge(var.tags, { Name = "iodp-nat-${var.environment}" })

  depends_on = [aws_internet_gateway.main]
}

# ─── Route Tables ───
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(var.tags, { Name = "iodp-rt-public-${var.environment}" })
}

resource "aws_route_table_association" "public" {
  count          = length(var.availability_zones)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = merge(var.tags, { Name = "iodp-rt-private-${var.environment}" })
}

resource "aws_route_table_association" "private" {
  count          = length(var.availability_zones)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ─── Security Group: MSK ───
resource "aws_security_group" "msk" {
  name_prefix = "iodp-msk-"
  description = "MSK Serverless - allow IAM auth port from Glue"
  vpc_id      = aws_vpc.main.id

  # Kafka IAM auth 端口（9098）仅允许来自 Glue SG
  ingress {
    description     = "Kafka IAM Auth from Glue"
    from_port       = 9098
    to_port         = 9098
    protocol        = "tcp"
    security_groups = [aws_security_group.glue.id]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "iodp-msk-sg-${var.environment}" })

  lifecycle {
    create_before_destroy = true
  }
}

# ─── Security Group: Glue ───
resource "aws_security_group" "glue" {
  name_prefix = "iodp-glue-"
  description = "Glue Jobs - self-referencing for Spark + outbound for AWS APIs"
  vpc_id      = aws_vpc.main.id

  # Glue Spark worker 之间需要互通
  ingress {
    description = "Self-referencing for Spark shuffle"
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    self        = true
  }

  egress {
    description = "All outbound (AWS APIs, S3, MSK)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "iodp-glue-sg-${var.environment}" })

  lifecycle {
    create_before_destroy = true
  }
}
