.PHONY: init deploy destroy destroy-confirm help check-aws check-tools build-image push-image \
       seed-data index-kb test-api status clean output

# ──────────────────────────────────────────────────────────────
# 环境变量
# ──────────────────────────────────────────────────────────────
AWS_REGION       ?= ap-southeast-1
ENV              ?= dev
AWS_ACCOUNT_ID   := $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
ECR_REGISTRY     := $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com
IMAGE_NAME       := iodp-agent
IMAGE_TAG        ?= latest
IMAGE_URI        := $(ECR_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
PROJECT_NAME     := iodp-agent
TF_DIR           := terraform

# 跨项目引用（项目一 BigData 的资源 ARN，部署前必须填写）
BIGDATA_DQ_TABLE_ARN    ?= arn:aws:dynamodb:$(AWS_REGION):$(AWS_ACCOUNT_ID):table/iodp_dq_reports_$(ENV)
BIGDATA_GOLD_BUCKET_ARN ?= arn:aws:s3:::iodp-gold-$(ENV)
COGNITO_USER_POOL_ARN   ?= ""
ATHENA_WORKGROUP        ?= primary
ATHENA_RESULT_BUCKET    ?= s3://iodp-athena-results-$(ENV)/

# Terraform 公共变量
TF_VARS := -var='aws_region=$(AWS_REGION)' \
           -var='environment=$(ENV)' \
           -var='bigdata_dq_reports_table_arn=$(BIGDATA_DQ_TABLE_ARN)' \
           -var='bigdata_gold_bucket_arn=$(BIGDATA_GOLD_BUCKET_ARN)' \
           -var='cognito_user_pool_arn=$(COGNITO_USER_POOL_ARN)' \
           -var='athena_workgroup=$(ATHENA_WORKGROUP)' \
           -var='athena_result_bucket=$(ATHENA_RESULT_BUCKET)'

# ──────────────────────────────────────────────────────────────
# 检查必要工具
# ──────────────────────────────────────────────────────────────
check-tools:
	@echo "🔍 检查必要工具..."
	@command -v docker >/dev/null 2>&1 || { echo "❌ 缺少 docker"; exit 1; }
	@command -v aws >/dev/null 2>&1 || { echo "❌ 缺少 aws cli"; exit 1; }
	@command -v terraform >/dev/null 2>&1 || { echo "❌ 缺少 terraform"; exit 1; }
	@command -v python3 >/dev/null 2>&1 || { echo "❌ 缺少 python3"; exit 1; }
	@docker info > /dev/null 2>&1 || { echo "❌ Docker daemon 未运行"; exit 1; }
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
# 构建 Docker 镜像（FastAPI + LangGraph）
# ──────────────────────────────────────────────────────────────
build-image: check-aws
	@echo "🔨 构建 Agent Docker 镜像..."
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .
	@echo "✅ 镜像已构建: $(IMAGE_NAME):$(IMAGE_TAG)"

# ──────────────────────────────────────────────────────────────
# 推送镜像到 ECR
# ──────────────────────────────────────────────────────────────
push-image: check-aws
	@echo "📤 推送镜像到 ECR..."
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $(ECR_REGISTRY)
	docker tag $(IMAGE_NAME):$(IMAGE_TAG) $(IMAGE_URI)
	docker push $(IMAGE_URI)
	@echo "✅ 镜像已推送: $(IMAGE_URI)"

# ──────────────────────────────────────────────────────────────
# 一次性初始化（首次部署用）
# ──────────────────────────────────────────────────────────────
init: check-aws
	@echo ""
	@echo "=========================================="
	@echo " IODP Agent 一键初始化部署 ($(ENV))"
	@echo "=========================================="
	@echo ""
	@echo "🏗️  步骤 1/7: 初始化 Terraform..."
	cd $(TF_DIR) && terraform init
	@echo ""
	@echo "🏗️  步骤 2/7: 创建 ECR 仓库..."
	cd $(TF_DIR) && terraform apply \
		-target=aws_ecr_repository.agent \
		$(TF_VARS) \
		-var='lambda_image_uri=dummy' \
		-auto-approve
	@echo "✅ ECR 仓库已创建"
	@echo ""
	@echo "🔨 步骤 3/7: 构建 Docker 镜像..."
	@$(MAKE) build-image
	@echo ""
	@echo "📤 步骤 4/7: 推送镜像到 ECR..."
	@$(MAKE) push-image
	@echo ""
	@echo "🏗️  步骤 5/7: 部署完整基础设施 (DynamoDB + OpenSearch + Lambda + API Gateway)..."
	cd $(TF_DIR) && terraform apply \
		$(TF_VARS) \
		-var='lambda_image_uri=$(IMAGE_URI)' \
		-auto-approve
	@echo "✅ 基础设施已创建"
	@echo ""
	@echo "📊 步骤 6/7: 注入 Demo 测试数据 (Athena + DynamoDB)..."
	@$(MAKE) seed-data
	@echo ""
	@echo "📚 步骤 7/7: 索引 RAG 知识库 (OpenSearch)..."
	@$(MAKE) index-kb
	@echo ""
	@echo "=========================================="
	@echo " ✅ 全部部署完成！"
	@echo "=========================================="
	@echo ""
	@cd $(TF_DIR) && terraform output
	@echo ""
	@echo "💡 快速测试: make test-api"
	@echo "💡 用完销毁: make destroy（省钱！）"

# ──────────────────────────────────────────────────────────────
# 注入 Demo 测试数据
# ──────────────────────────────────────────────────────────────
seed-data: check-aws
	@echo "📊 注入测试数据..."
	cd scripts && python3 seed_test_data.py \
		--region $(AWS_REGION) \
		--env $(ENV)
	@echo "✅ 测试数据注入完成"

# ──────────────────────────────────────────────────────────────
# 索引 RAG 知识库到 OpenSearch
# ──────────────────────────────────────────────────────────────
index-kb: check-aws
	@echo "📚 索引 RAG 知识库到 OpenSearch..."
	$(eval OS_ENDPOINT := $(shell cd $(TF_DIR) && terraform output -raw opensearch_endpoint 2>/dev/null))
	cd scripts && python3 index_knowledge_base.py \
		--region $(AWS_REGION) \
		--env $(ENV) \
		--opensearch-endpoint $(OS_ENDPOINT)
	@echo "✅ 知识库索引完成"

# ──────────────────────────────────────────────────────────────
# 日常部署（更新代码）
# ──────────────────────────────────────────────────────────────
deploy: check-aws build-image push-image
	@echo ""
	@echo "📦 更新 Lambda 函数..."
	aws lambda update-function-code \
		--function-name $(PROJECT_NAME)-$(ENV) \
		--image-uri $(IMAGE_URI) \
		--region $(AWS_REGION) > /dev/null
	@echo "✅ Lambda 函数已更新"
	@echo ""
	@echo "🎉 部署完成！"
	@cd $(TF_DIR) && terraform output

# ──────────────────────────────────────────────────────────────
# 端到端测试（发一个 Demo 请求）
# ──────────────────────────────────────────────────────────────
test-api: check-aws
	@echo "🧪 发送测试请求..."
	$(eval API_URL := $(shell cd $(TF_DIR) && terraform output -raw api_endpoint 2>/dev/null))
	@echo "API Endpoint: $(API_URL)"
	@curl -s -X POST $(API_URL)/diagnose \
		-H "Content-Type: application/json" \
		-d '{ \
			"user_input": "我昨晚11点支付一直失败，页面卡在加载中", \
			"user_id": "u_test_001" \
		}' | python3 -m json.tool
	@echo ""
	@echo "✅ 测试完成"

# ──────────────────────────────────────────────────────────────
# 查看部署状态
# ──────────────────────────────────────────────────────────────
status: check-aws
	@echo "📋 部署状态:"
	@echo ""
	@echo "--- Terraform 输出 ---"
	@cd $(TF_DIR) && terraform output 2>/dev/null || echo "(Terraform 未初始化)"
	@echo ""
	@echo "--- Lambda 函数 ---"
	@aws lambda get-function --function-name $(PROJECT_NAME)-$(ENV) --region $(AWS_REGION) \
		--query 'Configuration.{State:State,Runtime:Runtime,Memory:MemorySize,Timeout:Timeout,LastModified:LastModified}' \
		--output table 2>/dev/null || echo "(Lambda 未部署)"
	@echo ""
	@echo "--- OpenSearch Collection ---"
	@aws opensearchserverless list-collections \
		--query "collectionSummaries[?name=='iodp-rag-$(ENV)']" \
		--region $(AWS_REGION) --output table 2>/dev/null || echo "(OpenSearch 未部署)"

# ──────────────────────────────────────────────────────────────
# 销毁所有资源（省钱！）
# ──────────────────────────────────────────────────────────────

# 带确认提示（直接 make destroy 时）
destroy: check-aws
	@echo ""
	@echo "⚠️  即将销毁 Agent $(ENV) 环境的所有资源:"
	@echo "   - API Gateway + Lambda"
	@echo "   - OpenSearch Serverless Collection（停止 OCU 计费）"
	@echo "   - DynamoDB 表（Agent State + Tickets）"
	@echo "   - ECR 仓库 + 镜像"
	@echo "   - IAM Role + Policy"
	@echo ""
	@echo "⏱️  OpenSearch Serverless 每小时烧 SGD 0.73，确认要销毁？"
	@echo "按 Enter 继续, 或 Ctrl+C 取消"
	@read -p ""
	@$(MAKE) do-destroy

# 不带确认（被根 Makefile 调用时可用 CONFIRM=skip make destroy 跳过）
do-destroy: check-aws
	cd $(TF_DIR) && terraform destroy \
		$(TF_VARS) \
		-var='lambda_image_uri=$(IMAGE_URI)' \
		-auto-approve
	@echo ""
	@echo "✅ Agent 资源已销毁，计费已停止"

# ──────────────────────────────────────────────────────────────
# 清理本地资源
# ──────────────────────────────────────────────────────────────
clean:
	docker rmi $(IMAGE_NAME):$(IMAGE_TAG) 2>/dev/null || true
	docker rmi $(IMAGE_URI) 2>/dev/null || true
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache 2>/dev/null || true
	@echo "✅ 本地资源已清理"

# ──────────────────────────────────────────────────────────────
# 显示输出
# ──────────────────────────────────────────────────────────────
output:
	@cd $(TF_DIR) && terraform output

# ──────────────────────────────────────────────────────────────
# 帮助信息
# ──────────────────────────────────────────────────────────────
help:
	@echo "IODP Agent 部署 Makefile"
	@echo ""
	@echo "部署命令:"
	@echo ""
	@echo "  make init              首次一键部署（基础设施 + 镜像 + 测试数据 + RAG 索引）"
	@echo "  make deploy            日常更新（重建镜像 + 更新 Lambda）"
	@echo "  make destroy           销毁所有资源（停止计费）"
	@echo ""
	@echo "数据命令:"
	@echo ""
	@echo "  make seed-data         注入 Demo 测试数据到 Athena/DynamoDB"
	@echo "  make index-kb          索引 RAG 知识库到 OpenSearch"
	@echo ""
	@echo "调试命令:"
	@echo ""
	@echo "  make test-api          发送一个端到端测试请求"
	@echo "  make status            查看部署状态"
	@echo "  make output            显示 Terraform 输出"
	@echo "  make clean             清理本地 Docker 镜像和缓存"
	@echo ""
	@echo "环境变量:"
	@echo ""
	@echo "  AWS_ACCESS_KEY_ID      AWS 访问密钥（必需）"
	@echo "  AWS_SECRET_ACCESS_KEY  AWS 密钥（必需）"
	@echo "  AWS_REGION             AWS 区域（默认 ap-southeast-1）"
	@echo "  ENV                    环境名称（默认 dev）"
	@echo ""
	@echo "示例:"
	@echo ""
	@echo "  # 首次部署"
	@echo "  export AWS_ACCESS_KEY_ID=AKIA..."
	@echo "  export AWS_SECRET_ACCESS_KEY=..."
	@echo "  make init"
	@echo ""
	@echo "  # 测试完毕，立即销毁（省钱）"
	@echo "  make destroy"
	@echo ""
	@echo "  # 指定环境"
	@echo "  make init ENV=staging"
	@echo ""
	@echo "💰 成本提醒: OpenSearch Serverless 最低 ~SGD 0.73/小时，测完记得 make destroy"
