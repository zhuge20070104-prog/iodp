.PHONY: init deploy destroy help check-aws check-tools \
       init-bigdata init-agent deploy-bigdata deploy-agent \
       destroy-agent destroy-bigdata \
       test-api status status-bigdata status-agent clean help

# ──────────────────────────────────────────────────────────────
# 环境变量
# ──────────────────────────────────────────────────────────────
AWS_REGION     ?= ap-southeast-1
ENV            ?= dev
AWS_ACCOUNT_ID := $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")

BIGDATA_DIR := iodp-bigdata
AGENT_DIR   := iodp-agent

# ──────────────────────────────────────────────────────────────
# 检查必要工具
# ──────────────────────────────────────────────────────────────
check-tools:
	@echo "🔍 [IODP] 检查必要工具..."
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

# ══════════════════════════════════════════════════════════════
#  一键全部署：BigData 先部署 → 取输出 → 喂给 Agent
# ══════════════════════════════════════════════════════════════
init: check-aws
	@echo ""
	@echo "╔══════════════════════════════════════════════════════════╗"
	@echo "║     IODP 统一平台 一键部署 ($(ENV))                      ║"
	@echo "║                                                          ║"
	@echo "║     Phase 1: BigData 数据层 (MSK + Glue + S3 + Athena)  ║"
	@echo "║     Phase 2: Agent 智能层  (LangGraph + OpenSearch)     ║"
	@echo "╚══════════════════════════════════════════════════════════╝"
	@echo ""
	@$(MAKE) init-bigdata
	@echo ""
	@echo "──────────────────────────────────────────────────────────"
	@echo " Phase 1 完成，自动提取 BigData 输出，传递给 Agent..."
	@echo "──────────────────────────────────────────────────────────"
	@echo ""
	@$(MAKE) init-agent
	@echo ""
	@echo "╔══════════════════════════════════════════════════════════╗"
	@echo "║  ✅ IODP 统一平台部署完成！                               ║"
	@echo "╚══════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "快速命令:"
	@echo "  make test-api       发送端到端测试请求"
	@echo "  make status         查看两个项目部署状态"
	@echo "  make destroy        销毁全部资源（省钱！）"

# ──────────────────────────────────────────────────────────────
# Phase 1: 部署 BigData 数据层
# ──────────────────────────────────────────────────────────────
init-bigdata: check-aws
	@echo ""
	@echo "━━━ Phase 1: BigData 数据层 ━━━"
	@echo ""
	$(MAKE) -C $(BIGDATA_DIR) init \
		AWS_REGION=$(AWS_REGION) \
		ENV=$(ENV) \
		AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
		AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY)

# ──────────────────────────────────────────────────────────────
# Phase 2: 部署 Agent 智能层（自动从 BigData 取输出注入）
# ──────────────────────────────────────────────────────────────
init-agent: check-aws
	@echo ""
	@echo "━━━ Phase 2: Agent 智能层 ━━━"
	@echo ""
	$(eval DQ_TABLE_ARN := $(shell cd $(BIGDATA_DIR)/terraform && terraform output -raw dq_reports_table_arn 2>/dev/null || echo "arn:aws:dynamodb:$(AWS_REGION):$(AWS_ACCOUNT_ID):table/iodp_dq_reports_$(ENV)"))
	$(eval GOLD_BUCKET_ARN := $(shell cd $(BIGDATA_DIR)/terraform && terraform output -raw gold_bucket_arn 2>/dev/null || echo "arn:aws:s3:::iodp-gold-$(ENV)"))
	$(eval ATHENA_WG := $(shell cd $(BIGDATA_DIR)/terraform && terraform output -raw athena_workgroup 2>/dev/null || echo "primary"))
	$(eval ATHENA_BUCKET := $(shell cd $(BIGDATA_DIR)/terraform && terraform output -raw athena_result_bucket 2>/dev/null || echo "s3://iodp-athena-results-$(ENV)/"))
	@echo "📎 注入 BigData 输出:"
	@echo "   DQ Table ARN:     $(DQ_TABLE_ARN)"
	@echo "   Gold Bucket ARN:  $(GOLD_BUCKET_ARN)"
	@echo "   Athena Workgroup: $(ATHENA_WG)"
	@echo ""
	$(MAKE) -C $(AGENT_DIR) init \
		AWS_REGION=$(AWS_REGION) \
		ENV=$(ENV) \
		AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
		AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY) \
		BIGDATA_DQ_TABLE_ARN=$(DQ_TABLE_ARN) \
		BIGDATA_GOLD_BUCKET_ARN=$(GOLD_BUCKET_ARN) \
		ATHENA_WORKGROUP=$(ATHENA_WG) \
		ATHENA_RESULT_BUCKET=$(ATHENA_BUCKET)

# ══════════════════════════════════════════════════════════════
#  日常更新
# ══════════════════════════════════════════════════════════════
deploy: deploy-bigdata deploy-agent

deploy-bigdata: check-aws
	$(MAKE) -C $(BIGDATA_DIR) deploy \
		AWS_REGION=$(AWS_REGION) ENV=$(ENV) \
		AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
		AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY)

deploy-agent: check-aws
	$(MAKE) -C $(AGENT_DIR) deploy \
		AWS_REGION=$(AWS_REGION) ENV=$(ENV) \
		AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
		AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY)

# ══════════════════════════════════════════════════════════════
#  销毁：先拆 Agent（依赖方），再拆 BigData（被依赖方）
# ══════════════════════════════════════════════════════════════
destroy: check-aws
	@echo ""
	@echo "╔══════════════════════════════════════════════════════════╗"
	@echo "║  ⚠️  即将销毁 IODP $(ENV) 环境的全部资源                  ║"
	@echo "║                                                          ║"
	@echo "║  Phase 1: 先销毁 Agent（OpenSearch + Lambda + DynamoDB） ║"
	@echo "║  Phase 2: 再销毁 BigData（MSK + Glue + S3 数据湖）       ║"
	@echo "║                                                          ║"
	@echo "║  ⚠️  S3 数据湖中的数据将永久丢失！                         ║"
	@echo "╚══════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "按 Enter 继续, 或 Ctrl+C 取消"
	@read -p ""
	@echo ""
	@echo "━━━ Phase 1: 销毁 Agent 层 ━━━"
	$(MAKE) -C $(AGENT_DIR) do-destroy \
		AWS_REGION=$(AWS_REGION) ENV=$(ENV) \
		AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
		AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY)
	@echo ""
	@echo "━━━ Phase 2: 销毁 BigData 层 ━━━"
	$(MAKE) -C $(BIGDATA_DIR) do-destroy \
		AWS_REGION=$(AWS_REGION) ENV=$(ENV) \
		AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
		AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY)
	@echo ""
	@echo "✅ IODP 全部资源已销毁，计费已停止"

destroy-agent: check-aws
	$(MAKE) -C $(AGENT_DIR) destroy \
		AWS_REGION=$(AWS_REGION) ENV=$(ENV) \
		AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
		AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY)

destroy-bigdata: check-aws
	$(MAKE) -C $(BIGDATA_DIR) destroy \
		AWS_REGION=$(AWS_REGION) ENV=$(ENV) \
		AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
		AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY)

# ══════════════════════════════════════════════════════════════
#  测试 / 状态 / 清理
# ══════════════════════════════════════════════════════════════
test-api:
	$(MAKE) -C $(AGENT_DIR) test-api \
		AWS_REGION=$(AWS_REGION) ENV=$(ENV) \
		AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
		AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY)

status: status-bigdata status-agent

status-bigdata:
	@echo ""
	@echo "━━━ BigData 数据层 ━━━"
	@$(MAKE) -C $(BIGDATA_DIR) status \
		AWS_REGION=$(AWS_REGION) ENV=$(ENV) \
		AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
		AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY) 2>/dev/null || echo "(未部署)"

status-agent:
	@echo ""
	@echo "━━━ Agent 智能层 ━━━"
	@$(MAKE) -C $(AGENT_DIR) status \
		AWS_REGION=$(AWS_REGION) ENV=$(ENV) \
		AWS_ACCESS_KEY_ID=$(AWS_ACCESS_KEY_ID) \
		AWS_SECRET_ACCESS_KEY=$(AWS_SECRET_ACCESS_KEY) 2>/dev/null || echo "(未部署)"

clean:
	$(MAKE) -C $(BIGDATA_DIR) clean
	$(MAKE) -C $(AGENT_DIR) clean
	@echo "✅ 全部清理完成"

# ══════════════════════════════════════════════════════════════
#  帮助信息
# ══════════════════════════════════════════════════════════════
help:
	@echo "IODP 统一平台部署 Makefile"
	@echo ""
	@echo "  项目结构:"
	@echo "  ├── Makefile           ← 你在这里（根编排器）"
	@echo "  ├── iodp-bigdata/     ← 项目一：数据层"
	@echo "  │   └── Makefile"
	@echo "  └── iodp-agent/       ← 项目二：Agent 层"
	@echo "      └── Makefile"
	@echo ""
	@echo "  部署顺序: BigData 先 → Agent 后（自动传递 ARN）"
	@echo "  销毁顺序: Agent 先 → BigData 后（先拆依赖方）"
	@echo ""
	@echo "全局命令:"
	@echo ""
	@echo "  make init              一键部署整个平台（BigData → Agent）"
	@echo "  make deploy            更新整个平台"
	@echo "  make destroy           销毁整个平台（Agent → BigData）"
	@echo "  make test-api          端到端测试"
	@echo "  make status            查看全局部署状态"
	@echo "  make clean             清理本地资源"
	@echo ""
	@echo "单项目命令:"
	@echo ""
	@echo "  make init-bigdata      只部署 BigData 数据层"
	@echo "  make init-agent        只部署 Agent 层（需先部署 BigData）"
	@echo "  make deploy-bigdata    只更新 BigData"
	@echo "  make deploy-agent      只更新 Agent"
	@echo "  make destroy-agent     只销毁 Agent"
	@echo "  make destroy-bigdata   只销毁 BigData"
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
	@echo "  # 一键部署整个平台"
	@echo "  export AWS_ACCESS_KEY_ID=AKIA..."
	@echo "  export AWS_SECRET_ACCESS_KEY=..."
	@echo "  make init"
	@echo ""
	@echo "  # 测完立刻销毁（省钱）"
	@echo "  make destroy"
	@echo ""
	@echo "  # 只部署 Agent 层（BigData 已存在）"
	@echo "  make init-agent"
	@echo ""
	@echo "💰 成本提醒: OpenSearch Serverless 每小时 ~SGD 0.73，测完记得 make destroy"
