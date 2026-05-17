.PHONY: init deploy deploy-infra deploy-ddl enable-triggers disable-triggers destroy do-destroy help check-aws check-tools status clean output export-outputs demo-pipeline demo-silver demo-gold produce-sample produce-clicks produce-logs bootstrap-backend

# ──────────────────────────────────────────────────────────────
# 环境变量
# ──────────────────────────────────────────────────────────────
AWS_REGION     ?= ap-southeast-1
ENV            ?= dev
AWS_ACCOUNT_ID := $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
PROJECT_NAME   := iodp-bigdata
TF_DIR         := terraform

# Terraform 远端 state（值必须与 terraform/backend.tf 保持一致）
TF_STATE_BUCKET ?= iodp-terraform-state-prod
TF_STATE_REGION ?= us-east-1
TF_LOCK_TABLE   ?= iodp-terraform-locks

# Terraform 公共变量
#   - 自动加载 environments/$(ENV).tfvars（所有业务变量集中放那里）
#   - -var 用于覆盖 region / environment，方便 Makefile 切换
TF_VARS := -var-file=environments/$(ENV).tfvars \
           -var='aws_region=$(AWS_REGION)' \
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
# 首次部署：两步 bootstrap（Infra → DDL）
#   - Triggers 默认关闭（FinOps：手动触发模式，省 ~$35/周）
#   - 演示场景：用 `make demo-pipeline` 手动跑一次完整流水线
#   - 真正需要 24x7 自动批处理时：再 `make enable-triggers`
#
#   Glue Database 只能由 Terraform 建；Iceberg Table 只能由 Athena DDL 建。
#   所以必须 infra 先于 DDL。
# ──────────────────────────────────────────────────────────────
init: deploy-infra deploy-ddl
	@echo ""
	@echo "=========================================="
	@echo " ✅ IODP BigData 首次部署完成（$(ENV)）"
	@echo "=========================================="
	@echo " 💡 Triggers 当前为「手动模式」（默认 FinOps 配置）"
	@echo "     - 演示跑一次：make demo-pipeline"
	@echo "     - 切回自动定时：make enable-triggers"
	@echo "=========================================="
	@cd $(TF_DIR) && terraform output

# ──────────────────────────────────────────────────────────────
# 幂等 bootstrap：远端 state 依赖（S3 bucket + DynamoDB lock 表）
#   - 第一次 init 之前必须先建好这两个资源
#   - 已存在则跳过；不存在则按企业最佳实践创建（versioning + SSE + block-public）
#   - 注意：destroy 不会删这两个资源（terraform 不管理它们），可重复使用
# ──────────────────────────────────────────────────────────────
bootstrap-backend: check-aws
	@echo ""
	@echo "🔧 [Backend] 检查/创建 Terraform 远端 state 资源 (region=$(TF_STATE_REGION))..."
	@if aws s3api head-bucket --bucket $(TF_STATE_BUCKET) --region $(TF_STATE_REGION) 2>/dev/null; then \
		echo "  ✓ S3 bucket 已存在: $(TF_STATE_BUCKET)"; \
	else \
		echo "  ➕ 创建 S3 bucket: $(TF_STATE_BUCKET)..."; \
		if [ "$(TF_STATE_REGION)" = "us-east-1" ]; then \
			aws s3api create-bucket --bucket $(TF_STATE_BUCKET) --region $(TF_STATE_REGION) >/dev/null; \
		else \
			aws s3api create-bucket --bucket $(TF_STATE_BUCKET) --region $(TF_STATE_REGION) \
				--create-bucket-configuration LocationConstraint=$(TF_STATE_REGION) >/dev/null; \
		fi; \
		aws s3api put-bucket-versioning --bucket $(TF_STATE_BUCKET) --region $(TF_STATE_REGION) \
			--versioning-configuration Status=Enabled; \
		aws s3api put-public-access-block --bucket $(TF_STATE_BUCKET) --region $(TF_STATE_REGION) \
			--public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true; \
		aws s3api put-bucket-encryption --bucket $(TF_STATE_BUCKET) --region $(TF_STATE_REGION) \
			--server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'; \
		echo "  ✓ S3 bucket 已创建 (versioning + SSE + block-public)"; \
	fi
	@if aws dynamodb describe-table --table-name $(TF_LOCK_TABLE) --region $(TF_STATE_REGION) >/dev/null 2>&1; then \
		echo "  ✓ DynamoDB lock 表已存在: $(TF_LOCK_TABLE)"; \
	else \
		echo "  ➕ 创建 DynamoDB lock 表: $(TF_LOCK_TABLE)..."; \
		aws dynamodb create-table --table-name $(TF_LOCK_TABLE) --region $(TF_STATE_REGION) \
			--attribute-definitions AttributeName=LockID,AttributeType=S \
			--key-schema AttributeName=LockID,KeyType=HASH \
			--billing-mode PAY_PER_REQUEST >/dev/null; \
		aws dynamodb wait table-exists --table-name $(TF_LOCK_TABLE) --region $(TF_STATE_REGION); \
		echo "  ✓ DynamoDB lock 表已创建 (PAY_PER_REQUEST)"; \
	fi
	@echo "✅ Backend 资源就绪"

# 阶段 1：部署基础设施（Triggers 默认关闭）
deploy-infra: bootstrap-backend
	@echo ""
	@echo "🏗️  [1/2] 部署基础设施（Triggers 默认关闭，手动触发模式）..."
	cd $(TF_DIR) && terraform init -reconfigure
	cd $(TF_DIR) && terraform apply $(TF_VARS) -var='triggers_enabled=false' -auto-approve
	@echo "✅ 基础设施就绪：Glue Database / Job 已创建，Triggers 未启用"

# 阶段 2：执行 Athena DDL（在上面建好的 Database 里建 Iceberg Table）
deploy-ddl: check-aws
	@if [ -z "$(AWS_ACCOUNT_ID)" ]; then echo "❌ 无法获取 AWS_ACCOUNT_ID"; exit 1; fi
	@echo ""
	@echo "🗂️  [2/2] 执行 Athena DDL（建 7 张 Iceberg 表）..."
	bash scripts/apply_ddl.sh $(ENV) $(AWS_ACCOUNT_ID) $(AWS_REGION)
	@echo "✅ DDL 执行完成"

# 切换到自动模式（cron 定时触发，~$35/周）
enable-triggers: check-aws
	@echo ""
	@echo "⏰  启用 Glue Triggers（切换到自动定时模式）..."
	cd $(TF_DIR) && terraform apply $(TF_VARS) -var='triggers_enabled=true' -auto-approve
	@echo "✅ Triggers 已启用（Silver 每小时 :05 / Gold 每小时 :15 / Daily UTC 02:00）"
	@echo "⚠️  该模式预计周费 ~\$$35，演示完记得 make disable-triggers"

# 切换回手动模式（停止 cron 定时触发，省钱）
disable-triggers: check-aws
	@echo ""
	@echo "💤 关闭 Glue Triggers（切换到手动触发模式）..."
	cd $(TF_DIR) && terraform apply $(TF_VARS) -var='triggers_enabled=false' -auto-approve
	@echo "✅ Triggers 已关闭，Glue Job 资源保留但不会自动跑"
	@echo "💡 演示前手动触发：make demo-pipeline"

# ──────────────────────────────────────────────────────────────
# 日常部署（更新 Glue Job / 配置变更；Triggers 状态沿用 tfvars / 默认 false）
# ──────────────────────────────────────────────────────────────
deploy: check-aws
	@echo "📦 [BigData] 更新基础设施..."
	@# 幂等 init -reconfigure：处理 backend 配置历史变更，state 在远端 S3 不会丢
	cd $(TF_DIR) && terraform init -reconfigure
	cd $(TF_DIR) && terraform apply $(TF_VARS) -auto-approve
	@echo "✅ 更新完成"
	@cd $(TF_DIR) && terraform output

# ──────────────────────────────────────────────────────────────
# 演示模式：手动触发整条 Medallion 流水线（Silver → Gold）
#   - 默认 triggers_enabled=false 时使用
#   - 演示前跑一次，演示后无成本
#   - Silver 跑完等 5 min 再触发 Gold（避免 Gold 读到旧数据）
# ──────────────────────────────────────────────────────────────
demo-pipeline: demo-silver
	@echo "⏳ 等待 Silver 完成（5 分钟）..."
	@sleep 300
	@$(MAKE) demo-gold
	@echo ""
	@echo "=========================================="
	@echo " ✅ 演示流水线已触发完成"
	@echo "=========================================="
	@echo " 查看运行状态: aws glue get-job-runs --job-name <name> --region $(AWS_REGION)"

demo-silver: check-aws
	@echo ""
	@echo "▶ [Silver] 手动触发 2 个 Silver Job..."
	aws glue start-job-run --region $(AWS_REGION) --job-name iodp-silver-enrich-clicks-$(ENV)
	aws glue start-job-run --region $(AWS_REGION) --job-name iodp-silver-parse-logs-$(ENV)
	@echo "✅ Silver 已提交"

demo-gold: check-aws
	@echo ""
	@echo "▶ [Gold] 手动触发 3 个 Gold Job..."
	aws glue start-job-run --region $(AWS_REGION) --job-name iodp-gold-hourly-active-users-$(ENV)
	aws glue start-job-run --region $(AWS_REGION) --job-name iodp-gold-api-error-stats-$(ENV)
	aws glue start-job-run --region $(AWS_REGION) --job-name iodp-gold-incident-summary-$(ENV)
	@echo "✅ Gold 已提交"

# ──────────────────────────────────────────────────────────────
# 生产示例数据 → Firehose（演示数据入口）
#   - 替代旧的 Kafka producer（MSK 已移除）
#   - Direct PUT 模式：HTTPS + IAM，无 VPC、无 broker
# ──────────────────────────────────────────────────────────────
COUNT      ?= 1000
ERROR_RATE ?= 0.02

produce-sample: produce-clicks produce-logs

produce-clicks: check-aws
	@echo "▶ Pushing $(COUNT) clickstream events to Firehose..."
	python scripts/produce_sample_events.py \
		--stream clickstream --env $(ENV) --region $(AWS_REGION) --count $(COUNT)

produce-logs: check-aws
	@echo "▶ Pushing $(COUNT) app_logs events to Firehose (error-rate=$(ERROR_RATE))..."
	python scripts/produce_sample_events.py \
		--stream app_logs --env $(ENV) --region $(AWS_REGION) \
		--count $(COUNT) --error-rate $(ERROR_RATE)

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
	@echo "   - Kinesis Data Firehose Delivery Streams"
	@echo "   - S3 数据湖 (Bronze/Silver/Gold)"
	@echo "   - Glue Jobs + Data Catalog"
	@echo "   - DynamoDB 表 (DQ Reports + Lineage + Threshold Config)"
	@echo "   - CloudWatch Dashboard + Alarms + SNS Topic"
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
	@echo "首次部署（两步 bootstrap，Triggers 默认手动模式）："
	@echo "  make init              一键 bootstrap = deploy-infra + deploy-ddl"
	@echo "  make bootstrap-backend 幂等创建远端 state S3 桶 + DynamoDB 锁表（首次/排错用）"
	@echo "  make deploy-infra      [1/2] terraform apply（Triggers 关闭，自动调 bootstrap-backend）"
	@echo "  make deploy-ddl        [2/2] Athena 建 7 张 Iceberg 表"
	@echo ""
	@echo "演示 / 手动跑流水线（推荐 — FinOps 最省）："
	@echo "  make produce-sample    向 Firehose 推送示例点击流 + app_logs 事件"
	@echo "  make produce-clicks    仅推 clickstream（COUNT=1000）"
	@echo "  make produce-logs      仅推 app_logs（ERROR_RATE=0.02）"
	@echo "  make demo-pipeline     手动触发 Silver → 等 5 min → 触发 Gold"
	@echo "  make demo-silver       仅触发 Silver 层 2 个 Job"
	@echo "  make demo-gold         仅触发 Gold 层 3 个 Job"
	@echo ""
	@echo "Trigger 模式切换："
	@echo "  make enable-triggers   切到自动 cron 模式（~\$$35/周）"
	@echo "  make disable-triggers  切回手动模式（默认，省钱）"
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
