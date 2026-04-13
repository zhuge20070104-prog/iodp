.PHONY: init deploy destroy do-destroy help check-aws check-tools status clean output export-outputs

# ──────────────────────────────────────────────────────────────
# 环境变量
# ──────────────────────────────────────────────────────────────
AWS_REGION     ?= ap-southeast-1
ENV            ?= dev
AWS_ACCOUNT_ID := $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
PROJECT_NAME   := iodp-bigdata
TF_DIR         := terraform

# Terraform 公共变量
TF_VARS := -var='aws_region=$(AWS_REGION)' \
           -var='environment=$(ENV)'

# ──────────────────────────────────────────────────────────────
# 检查必要工具
# ──────────────────────────────────────────────────────────────
check-tools:
	@echo "🔍 [BigData] 检查必要工具..."
	@command -v aws >/dev/null 2>&1 || { echo "❌ 缺少 aws cli"; exit 1; }
	@command -v terraform >/dev/null 2>&1 || { echo "❌ 缺少 terraform"; exit 1; }
	@echo "✅ 工具检查通过"

# ──────────────────────────────────────────────────────────────
# 检查 AWS 凭证
# ──────────────────────────────────────────────────────────────
check-aws: check-tools
	@if [ -z "$(AWS_ACCESS_KEY_ID)" ] || [ -z "$(AWS_SECRET_ACCESS_KEY)" ]; then \
		echo "❌ 错误：缺少 AWS 凭证"; \
		echo ""; \
		echo "请先设置:"; \
		echo "  export AWS_ACCESS_KEY_ID=\"AKIAXXXXXXXXXXXXXXXX\""; \
		echo "  export AWS_SECRET_ACCESS_KEY=\"xxxxxxxx\""; \
		echo "  export AWS_DEFAULT_REGION=\"$(AWS_REGION)\""; \
		exit 1; \
	fi
	@echo "✅ AWS 凭证验证通过 (账号: $(AWS_ACCOUNT_ID), 区域: $(AWS_REGION), 环境: $(ENV))"

# ──────────────────────────────────────────────────────────────
# 一次性初始化
# ──────────────────────────────────────────────────────────────
init: check-aws
	@echo ""
	@echo "=========================================="
	@echo " IODP BigData 数据层部署 ($(ENV))"
	@echo "=========================================="
	@echo ""
	@echo "🏗️  步骤 1/3: 初始化 Terraform..."
	cd $(TF_DIR) && terraform init
	@echo ""
	@echo "🏗️  步骤 2/3: 规划基础设施..."
	cd $(TF_DIR) && terraform plan $(TF_VARS)
	@echo ""
	@echo "🏗️  步骤 3/3: 部署基础设施 (VPC + MSK + S3 + Glue + DynamoDB + Athena)..."
	cd $(TF_DIR) && terraform apply $(TF_VARS) -auto-approve
	@echo ""
	@echo "✅ BigData 数据层部署完成！"
	@echo ""
	@cd $(TF_DIR) && terraform output

# ──────────────────────────────────────────────────────────────
# 日常部署（更新 Glue Job / 配置变更）
# ──────────────────────────────────────────────────────────────
deploy: check-aws
	@echo "📦 [BigData] 更新基础设施..."
	cd $(TF_DIR) && terraform apply $(TF_VARS) -auto-approve
	@echo "✅ 更新完成"
	@cd $(TF_DIR) && terraform output

# ──────────────────────────────────────────────────────────────
# 查看部署状态
# ──────────────────────────────────────────────────────────────
status: check-aws
	@echo "📋 [BigData] 部署状态:"
	@echo ""
	@echo "--- Terraform 输出 ---"
	@cd $(TF_DIR) && terraform output 2>/dev/null || echo "(Terraform 未初始化)"
	@echo ""
	@echo "--- Glue Jobs ---"
	@aws glue get-jobs --region $(AWS_REGION) \
		--query "Jobs[?starts_with(Name,'iodp')].{Name:Name,State:LastModifiedOn}" \
		--output table 2>/dev/null || echo "(Glue Jobs 未部署)"
	@echo ""
	@echo "--- DynamoDB 表 ---"
	@aws dynamodb list-tables --region $(AWS_REGION) \
		--query "TableNames[?starts_with(@,'iodp')]" \
		--output table 2>/dev/null || echo "(DynamoDB 未部署)"

# ──────────────────────────────────────────────────────────────
# 导出输出供 Agent 层引用（被根 Makefile 调用）
# ──────────────────────────────────────────────────────────────
export-outputs: check-aws
	@cd $(TF_DIR) && terraform output -json

# ──────────────────────────────────────────────────────────────
# 销毁所有资源
# ──────────────────────────────────────────────────────────────

# 带确认提示
destroy: check-aws
	@echo ""
	@echo "⚠️  即将销毁 BigData $(ENV) 环境的所有资源:"
	@echo "   - VPC + 子网 + 安全组"
	@echo "   - MSK Serverless 集群"
	@echo "   - S3 数据湖 (Bronze/Silver/Gold)"
	@echo "   - Glue Jobs + Data Catalog"
	@echo "   - DynamoDB 表 (DQ Reports + Lineage)"
	@echo "   - Athena Workgroup"
	@echo ""
	@echo "⚠️  S3 桶中的数据将永久丢失！"
	@echo "按 Enter 继续, 或 Ctrl+C 取消"
	@read -p ""
	@$(MAKE) do-destroy

# 不带确认（被根 Makefile 调用时）
do-destroy: check-aws
	cd $(TF_DIR) && terraform destroy $(TF_VARS) -auto-approve
	@echo ""
	@echo "✅ BigData 资源已销毁"

# ──────────────────────────────────────────────────────────────
# 清理本地
# ──────────────────────────────────────────────────────────────
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache 2>/dev/null || true
	@echo "✅ 本地缓存已清理"

# ──────────────────────────────────────────────────────────────
# 显示输出
# ──────────────────────────────────────────────────────────────
output:
	@cd $(TF_DIR) && terraform output

# ──────────────────────────────────────────────────────────────
# 帮助信息
# ──────────────────────────────────────────────────────────────
help:
	@echo "IODP BigData 数据层 Makefile"
	@echo ""
	@echo "  make init              首次部署基础设施"
	@echo "  make deploy            更新基础设施"
	@echo "  make destroy           销毁所有资源"
	@echo "  make status            查看部署状态"
	@echo "  make output            显示 Terraform 输出"
	@echo "  make clean             清理本地缓存"
	@echo ""
	@echo "环境变量:"
	@echo ""
	@echo "  AWS_ACCESS_KEY_ID      AWS 访问密钥（必需）"
	@echo "  AWS_SECRET_ACCESS_KEY  AWS 密钥（必需）"
	@echo "  AWS_REGION             AWS 区域（默认 ap-southeast-1）"
	@echo "  ENV                    环境名称（默认 dev）"
