# IODP Agent —— LangGraph 多 Agent 故障诊断系统

基于 LangGraph 的多 Agent 状态机：用户描述故障 → Router 分类意图 → Log Analyzer 查 Athena → RAG 检索 S3 Vectors → 并行生成用户回复 + 结构化 Bug 报告。FastAPI on Lambda Container + API Gateway HTTP API + **异步 Job 模式**绕开 29 秒超时。

> 配套项目 [`../iodp-bigdata/`](../iodp-bigdata/) 提供 Athena 视图、DynamoDB DQ 报告、S3 Vectors 知识库。架构深入和运行时机制见 [AGENT.md](./AGENT.md)。

---

## 架构总览

```
React SPA (S3 + CloudFront + OAI)
    │  POST /diagnose   →  202 + job_id
    │  GET  /diagnose/{job_id}   →  result | running | queued
    ▼
API Gateway HTTP API (throttle 20 RPS / burst 50)
    │
    ▼
Lambda Container (Mangum → FastAPI → LangGraph)
    │
    ├──→ Qwen (dashscope OpenAI 兼容)   qwen-max 推理 + qwen-turbo 路由
    ├──→ Athena                         v_error_log_enriched 视图
    ├──→ DynamoDB × 3                   Jobs / Checkpointer / Tickets
    └──→ S3 Vectors                     incident_solutions + product_docs
```

**异步 Job 模式**：POST 立即返回 202 + job_id，LangGraph 在 FastAPI `BackgroundTasks` 后台跑，客户端 GET 轮询结果——为绕开 API Gateway 的 29 秒硬超时。

完整 LangGraph 状态机和深入机制见 [AGENT.md](./AGENT.md)。

---

## 部署 —— 4 步走

### 0. 前置条件

```bash
# 工具
aws --version          # AWS CLI v2
terraform -version     # >= 1.6.0
docker --version       # daemon 必须运行（Lambda Container 镜像构建）
python3 --version      # >= 3.10

# AWS 凭证 + region
export AWS_ACCESS_KEY_ID="AKIAXXXXXXXXXXXXXXXX"
export AWS_SECRET_ACCESS_KEY="xxxxxxxx"
export AWS_REGION="ap-southeast-1"

# LLM API key（qwen / deepseek / glm / openai 任选；默认走 dashscope）
export IODP_LLM_API_KEY="sk-..."

# 可选：跨项目 ARN（不设则 Makefile 用默认拼接，bigdata 没部署时会指向不存在资源）
# 通常由根目录 Makefile 从 bigdata 的 terraform output 自动注入
export BIGDATA_DQ_TABLE_ARN="arn:aws:dynamodb:ap-southeast-1:${AWS_ACCOUNT_ID}:table/iodp_dq_reports_dev"
export BIGDATA_GOLD_BUCKET_ARN="arn:aws:s3:::iodp-gold-dev"
```

### 1. 首次部署

```bash
make init ENV=dev
```

`make init` 内置 7 步（见 [Makefile:84-138](Makefile#L84-L138)）：
1. `terraform init`
2. 创建 ECR 仓库
3. 构建 Docker 镜像（Lambda Container）
4. 推送到 ECR
5. `terraform apply` 全量基础设施（DynamoDB × 3 + S3 Vectors + Lambda + API Gateway + S3/CloudFront）
6. `make seed-data` 注入 Demo 数据到 Athena + DynamoDB
7. `make index-kb` 索引 RAG 知识库到 S3 Vectors

### 2. 部署前端（可选）

```bash
make deploy-frontend ENV=dev
```

Vite build React → 注入 API endpoint → S3 sync → CloudFront invalidation。

### 3. 端到端测试

```bash
make test-api ENV=dev
```

发一个 `POST /diagnose`，自动解析结果。

### 4. 销毁（演示完）

```bash
make destroy ENV=dev
```

---

## 常用命令

| 命令 | 作用 |
|---|---|
| `make help`            | 显示完整命令清单 |
| `make init`            | 首次一键部署（基础设施 + 镜像 + 测试数据 + RAG 索引） |
| `make deploy`          | 日常更新：重建镜像 + 更新 Lambda（不动 infra） |
| `make deploy-frontend` | build React + 上传 S3 + 刷新 CloudFront |
| `make deploy-all`      | 后端 + 前端 |
| `make seed-data`       | 注入 Demo 数据到 Athena/DynamoDB |
| `make index-kb`        | 重新索引 RAG 到 S3 Vectors |
| `make test-api`        | 发送端到端测试请求 |
| `make status`          | 查看 Lambda / S3 Vectors / Terraform 状态 |
| `make destroy`         | 销毁所有资源（停止计费） |
| `make clean`           | 清理本地 Docker 镜像和 Python 缓存 |

---

## 请求示例

```bash
# 第一轮对话
curl -X POST https://{api-endpoint}/diagnose \
  -H "Content-Type: application/json" \
  -d '{"message": "我昨晚 11 点支付一直失败，页面卡在加载中"}'
# 返回 202: {"job_id": "xxx", "status": "queued", "thread_id": "thread_xxx"}

# 轮询结果（建议每 2-3 秒一次，最多 120 秒）
curl https://{api-endpoint}/diagnose/xxx

# 第二轮对话（传入相同 thread_id 即可继续）
curl -X POST https://{api-endpoint}/diagnose \
  -H "Content-Type: application/json" \
  -d '{"message": "我的用户 ID 是 u_12345", "thread_id": "thread_xxx"}'
```

---

## 项目结构

```
iodp-agent/
├── README.md                ← 你在这里（项目入口 + 部署）
├── AGENT.md                 ← 架构深入 + 运行时机制 + 设计细节
├── Makefile
├── Dockerfile               ← Lambda Container 镜像
├── lambda_handler.py        ← Mangum 适配 FastAPI
├── requirements.txt
│
├── src/
│   ├── main.py              ← FastAPI 应用 + 异步 Job 端点
│   ├── config.py            ← Pydantic Settings（IODP_ 前缀环境变量）
│   ├── graph/
│   │   ├── state.py         ← AgentState + Reducer 定义
│   │   ├── graph_builder.py ← LangGraph 图构建 + 路由逻辑
│   │   ├── checkpointer.py  ← DynamoDB 多轮对话状态持久化
│   │   └── nodes/
│   │       ├── router_agent.py
│   │       ├── log_analyzer_agent.py
│   │       ├── rag_agent.py
│   │       ├── reply_agent.py
│   │       └── bug_report_agent.py
│   └── tools/
│       ├── athena_tool.py
│       ├── dynamodb_tool.py
│       └── s3_vectors_tool.py
│
├── frontend/                ← Vite + React 18 聊天界面
│
├── terraform/
│   ├── main.tf              ← Lambda + API Gateway + ECR + S3 Vectors + S3/CloudFront
│   └── modules/dynamodb/    ← Agent State + Tickets + Jobs 三张表
│
├── scripts/
│   ├── seed_test_data.py
│   └── index_knowledge_base.py
│
└── tests/unit/
```

---

## 关键设计决策（一句话版）

| 决策 | 选择 | 为什么 |
|---|---|---|
| HTTP 接入 | **API Gateway HTTP API**（不是 REST API） | 比 REST API 便宜 70%，原生支持 JWT |
| 长时任务 | **异步 Job + BackgroundTasks** | 绕开 API Gateway 29 秒硬超时；POST 202 + GET 轮询 |
| 多轮对话状态 | **DynamoDB Checkpointer + `add_messages` reducer** | thread_id 串联多轮消息，每轮一个 job_id |
| LLM provider | **Qwen (dashscope OpenAI 兼容)** | 中国大陆 AWS 账号过不了 Bedrock allowlisting；单 key 同时支持 chat + embedding |
| 双模型路由 | **qwen-max 推理 / qwen-turbo 路由** | router/rag/reply 用便宜模型，省 ~5x |
| RAG 存储 | **S3 Vectors（不是 OpenSearch）** | 替换 OpenSearch Serverless（Dec 2025），省 ~90% |
| 工单查询 | **DynamoDB GSI `severity-service-index`** | 按 severity + service 查询不用全表扫描 |
| 前端托管 | **S3（全 block_public）+ CloudFront + OAI** | 强制走 CDN，禁止裸 S3 访问 |
| 限流 | **API Gateway throttle（令牌桶 20 RPS / burst 50）** | 防恶意刷接口 / 前端 bug 无限轮询打爆 Lambda |
| 配置管理 | **Pydantic Settings + `IODP_` 前缀环境变量** | 本地用 `.env`，Lambda 用 Terraform 注入 |

---

## 跨项目契约（依赖 iodp-bigdata）

Agent 不 import bigdata 代码，**契约是 AWS 资源 ARN + Athena 视图/表名**：

| Agent 端读取 | bigdata 端提供 |
|---|---|
| `dynamodb_tool.py` → `iodp-dq-reports-{env}` | DQ 校验结果（Silver Job 写入） |
| `athena_tool.py` → view `v_error_log_enriched` | 错误日志 + Gold 统计 join |
| `s3_vectors_tool.py` → index `incident_solutions` | bigdata `vector_indexer` Lambda 自动灌入 |
| `s3_vectors_tool.py` → index `product_docs` | 运维手册（agent 端 `make index-kb` 手动灌入） |

ARN 由根 [Makefile](../Makefile) 从 bigdata 的 `terraform output` 抽出来注入到 agent 的 TF vars——见 root `make init-agent` target。

---

## 周费估算（dev 环境，闲置）

```
Lambda + API Gateway + DynamoDB + S3 Vectors    ≈ $0.5 / 周（基本只算存储）
被调用时：Lambda 调用 + LLM API + Athena 扫描     按使用量
```

主要成本是 **LLM API**（qwen-max ≈ ¥0.04 / 1K tokens）和 **Athena 扫描**。Lambda + API Gateway 在闲置状态成本几乎为零。这是和 bigdata 项目"演示完立刻 destroy"理念一致的 FinOps 设计。

---

## 相关文档

- [AGENT.md](./AGENT.md) —— 架构深入：LangGraph 节点 / 异步 Job 内部机制 / Checkpointer + Reducer / DynamoDB GSI / API Gateway 限流 / S3+CloudFront 安全模型 / 配置管理
- [`../iodp-bigdata/`](../iodp-bigdata/) —— 配套数据湖项目（Firehose → S3 Iceberg → Athena）
- [`../INTERVIEW.md`](../INTERVIEW.md) —— 简历讲述用的 STAR 故事
- [`../CLAUDE.md`](../CLAUDE.md) —— Claude Code 工作指引（两个项目协作方式）
