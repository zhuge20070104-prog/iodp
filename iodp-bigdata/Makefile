.PHONY: init deploy deploy-infra deploy-ddl enable-triggers destroy do-destroy help check-aws check-tools status clean output export-outputs

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
# 首次部署：三步 bootstrap（DB → Tables → Triggers）
#   Glue Database 只能由 Terraform 建；Iceberg Table 只能由 Athena DDL 建。
#   所以必须 infra 先于 DDL；Triggers 又必须后于 DDL（否则 cron 触发的
#   Glue Job 会因表不存在而失败）。三个 target 分离这三阶段。
# ──────────────────────────────────────────────────────────────
init: deploy-infra deploy-ddl enable-triggers
	@echo ""
	@echo "=========================================="
	@echo " ✅ IODP BigData 首次部署完成（$(ENV)）"
	@echo "=========================================="
	@cd $(TF_DIR) && terraform output

# 阶段 1：部署基础设施（Triggers 保持关闭，等 DDL 执行完再开）
deploy-infra: check-aws
	@echo ""
	@echo "🏗️  [1/3] 部署基础设施（Triggers 暂时关闭）..."
	cd $(TF_DIR) && terraform init
	cd $(TF_DIR) && terraform apply $(TF_VARS) -var='triggers_enabled=false' -auto-approve
	@echo "✅ 基础设施就绪：Glue Database / Job 已创建，Triggers 暂未启用"

# 阶段 2：执行 Athena DDL（在上面建好的 Database 里建 Iceberg Table）
deploy-ddl: check-aws
	@if [ -z "$(AWS_ACCOUNT_ID)" ]; then echo "❌ 无法获取 AWS_ACCOUNT_ID"; exit 1; fi
	@echo ""
	@echo "🗂️  [2/3] 执行 Athena DDL（建 7 张 Iceberg 表）..."
	bash scripts/apply_ddl.sh $(ENV) $(AWS_ACCOUNT_ID) $(AWS_REGION)
	@echo "✅ DDL 执行完成"

# 阶段 3：启用 Glue Triggers（cron 开始正常触发批处理作业）
enable-triggers: check-aws
	@echo ""
	@echo "⏰  [3/3] 启用 Glue Triggers..."
	cd $(TF_DIR) && terraform apply $(TF_VARS) -var='triggers_enabled=true' -auto-approve
	@echo "✅ Triggers 已启用（Silver 每小时 :05 / Gold 每小时 :15 / Daily UTC 02:00）"

# ──────────────────────────────────────────────────────────────
# 日常部署（更新 Glue Job / 配置变更，Triggers 状态沿用默认 true）
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
	@echo "首次部署（严格按顺序）："
	@echo "  make init              一键 bootstrap = deploy-infra + deploy-ddl + enable-triggers"
	@echo "  make deploy-infra      [1/3] terraform apply（Triggers 关闭）"
	@echo "  make deploy-ddl        [2/3] Athena 建 7 张 Iceberg 表"
	@echo "  make enable-triggers   [3/3] 启用 Glue 定时触发器"
	@echo ""
	@echo "日常运维："
	@echo "  make deploy            更新基础设施（Triggers 沿用当前状态）"
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
