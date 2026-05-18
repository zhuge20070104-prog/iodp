# IODP Agent 架构说明

## 项目定位

多 Agent 智能故障诊断系统 v2.0。用户描述故障 → Agent 自动查日志、搜知识库 → 生成诊断回复 + 结构化 Bug 报告。

---

## 部署架构

```
用户浏览器
    │
    ▼
CloudFront + S3 (React SPA 静态托管)
    │
    │ POST /diagnose, GET /diagnose/{job_id}, GET /health
    ▼
API Gateway HTTP API ($1/百万请求)
    │
    │ Cognito JWT 认证（可选）
    ▼
Lambda Container (Mangum → FastAPI → LangGraph)
    │
    ├──→ Qwen via dashscope (OpenAI 兼容) — qwen-max 推理 + qwen-turbo 路由
    ├──→ Athena (查 v_error_log_enriched 视图)
    ├──→ DynamoDB (Job 状态 + Checkpointer + DQ 报告)
    └──→ S3 Vectors (RAG 向量搜索 — GA 2025-12)
```

---

## 目录结构

```
iodp-agent/
├── lambda_handler.py           # Lambda 入口：Mangum 适配 FastAPI
├── Dockerfile                  # Lambda 容器镜像
├── requirements.txt            # Python 依赖
├── Makefile                    # 一键部署/销毁
│
├── src/
│   ├── main.py                 # FastAPI 应用（异步 Job 模式）
│   ├── config.py               # Pydantic Settings 配置管理
│   │
│   ├── graph/
│   │   ├── state.py            # AgentState + Reducer 定义
│   │   ├── graph_builder.py    # LangGraph 图构建 + 路由逻辑
│   │   ├── checkpointer.py     # DynamoDB Checkpointer（多轮对话）
│   │   └── nodes/
│   │       ├── router_agent.py       # 意图分类 + 信息提取
│   │       ├── log_analyzer_agent.py # Athena SQL 生成 + 执行
│   │       ├── rag_agent.py          # S3 Vectors 向量检索
│   │       ├── reply_agent.py        # 用户友好回复生成
│   │       └── bug_report_agent.py   # 结构化 Bug 报告生成
│   │
│   └── tools/
│       ├── athena_tool.py      # Athena 查询封装（SQL 安全校验）
│       ├── dynamodb_tool.py    # DQ 报告查询（跨项目读 bigdata）
│       └── s3_vectors_tool.py  # DashScope text-embedding-v3 + S3 Vectors query_vectors
│
├── frontend/
│   ├── package.json            # Vite + React 18
│   ├── index.html
│   └── src/App.jsx             # 聊天界面
│
├── terraform/
│   ├── main.tf                 # Lambda + API Gateway + ECR + S3 Vectors + S3/CloudFront
│   ├── variables.tf
│   ├── outputs.tf
│   └── modules/dynamodb/       # Agent State + Tickets + Jobs 三张 DynamoDB 表
│
├── scripts/
│   ├── seed_test_data.py       # 注入测试数据
│   └── index_knowledge_base.py # 离线索引 RAG 知识库
│
└── tests/unit/                 # 单元测试
```

---

## LangGraph 状态机

```
START
  │
  ▼
[router_agent] 意图分类 + 信息提取
  │
  ├── tech_issue（有 user_id）→ [log_analyzer] → [rag_agent]
  │                                                   │
  │                                    ┌──────────────┤
  │                                    ▼              ▼
  │                              [reply_agent]  [bug_report_agent]  ← 并行
  │                                    │              │
  │                                    └──────┬───────┘
  │                                           │ merge_synthesizer
  │                                          END
  │
  ├── tech_issue（无 user_id）→ END（返回追问，等下一轮）
  ├── inquiry                 → [rag_agent] → [reply_agent] → END
  ├── refund / unknown        → [reply_agent] → END
  ├── security_violation      → [reply_agent] → END（拒绝回复）
  └── need_more_info          → END（追问）
```

---

## 数据流：Agent 如何使用 BigData 数据

```
bigdata 端（数据生产）                 agent 端（数据消费）
──────────────────                     ──────────────────

Athena 视图                            log_analyzer_agent.py
v_error_log_enriched         ←───────  athena_tool.py 发 SQL 查询
(Gold 错误统计 JOIN Silver 日志详情)    获取 error_rate, trace_id, stack_trace

DynamoDB 表                            log_analyzer_agent.py
iodp-dq-reports-{env}       ←───────  dynamodb_tool.py 查 DQ 报告
(Streaming Job DQ 校验结果)            判断是否为数据质量问题，排除假告警

S3 Vectors 索引                        rag_agent.py
incident_solutions           ←───────  s3_vectors_tool.py 向量搜索
(Gold incident_summary 自动灌入)       "历史上有没有类似故障？"

product_docs                 ←───────  s3_vectors_tool.py 向量搜索
(运维手册，人工离线导入)               "这个错误码怎么排查？"
```

---

## AWS 服务清单

| 服务 | 用途 | 选型依据 |
|------|------|---------|
| **API Gateway HTTP API** | HTTP 接入 + 路由 | 比 REST API 便宜 70% |
| **Lambda (Container)** | 运行 FastAPI + LangGraph | 按请求计费，零流量零成本 |
| **ECR** | 存储 Lambda 容器镜像 | 与 Lambda Container 配套 |
| **DynamoDB** | Job 状态 + Checkpointer + Tickets | PAY_PER_REQUEST，serverless |
| **Qwen / dashscope (OpenAI 兼容)** | qwen-max 推理 + qwen-turbo 路由 + text-embedding-v3 | 中国大陆 AWS 账号过不了 Bedrock allowlisting；单个 API key 同时支持 chat + embedding |
| **Athena** | 查询 BigData Gold/Silver 层 | 按扫描量计费，Serverless |
| **S3 Vectors** | RAG 向量搜索（GA 2025-12） | 存算分离 / 按 PUT+存储+查询计费，比 OpenSearch Serverless 便宜 ~90% |
| **S3** | 前端静态文件 + Athena 结果 | 标准对象存储 |
| **CloudFront** | 前端 CDN + HTTPS | 边缘缓存，全球加速 |
| **Cognito** | JWT 认证（可选） | 免费档 5 万 MAU |

---

## DynamoDB 表（Agent 端）

| 表名 | 主键 | 用途 | TTL |
|------|------|------|-----|
| `iodp-agent-state-{env}` | thread_id + checkpoint_ns | LangGraph Checkpointer，多轮对话状态 | 7 天 |
| `iodp-bug-tickets-{env}` | report_id + generated_at | Bug 报告存档，GSI 按 severity 查询 | 可配置 |
| `iodp-agent-jobs-{env}` | job_id，GSI: thread_id | 异步 Job 状态跟踪（queued/running/completed/failed） | 1 小时 |

---

---

# 深入：运行时机制

## 异步 Job 模式（BackgroundTasks）

API Gateway 有 29 秒硬超时，LangGraph 跑一遍轻松 30 秒+。解法是 POST 立即返回 202 + job_id，把 graph 执行扔给 FastAPI 的 `BackgroundTasks`，客户端用 GET 轮询。

```
前端                       API Gateway + Lambda            DynamoDB
 │                                  │                         │
 │ POST /diagnose                   │                         │
 │ { message, thread_id? }          │                         │
 │ ─────────────────────────────►   │ put_item(status=queued) │
 │                                  │ ───────────────────────►│
 │  202 { job_id, status=queued }   │                         │
 │ ◄─────────────────────────────── │ [后台] LangGraph 执行    │
 │                                  │ update(status=running)  │
 │ GET /diagnose/{job_id}           │ update(status=completed)│
 │ ─────────────────────────────►   │ ◄───────────────────────│
 │ { status: completed, result: …}  │                         │
 │ ◄─────────────────────────────── │                         │
```

```python
@app.post("/diagnose", status_code=202)
async def submit_diagnosis(request, background_tasks: BackgroundTasks):
    _create_job_record(job_id, ...)                            # 1. 写 DynamoDB
    background_tasks.add_task(run_graph_job, job_id, ...)      # 2. 丢到后台
    return JobResponse(job_id=job_id, status="queued")         # 3. 立即返回 202
```

建议客户端每 2-3 秒轮询一次 `GET /diagnose/{job_id}`，最多等待 120 秒。

### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/diagnose` | 提交诊断请求，返回 202 + job_id |
| GET | `/diagnose/{job_id}` | 轮询 Job 状态和结果 |
| GET | `/health` | 健康检查（无需认证） |

---

## DynamoDB job_id vs thread_id

`iodp-agent-jobs-{env}` 用 `job_id` 做主键，`thread_id` 做 GSI。**一个 thread 多个 job**——多轮对话中每条用户消息都是一个独立的 job：

| job_id (PK) | thread_id (GSI) | status | result_json |
|---|---|---|---|
| job_001 | thread_abc | completed | "请问您的用户 ID..." |
| job_002 | thread_abc | completed | "查到了，昨晚..." |
| job_003 | thread_abc | running | "" |

两种查询方式：
- **按 job_id 查（主键）**：`GET /diagnose/job_003` — "这个 job 跑到哪了？"
- **按 thread_id 查（GSI）**：`query(thread_id="thread_abc")` — "这个对话的所有 job 历史"

没有 GSI，查某 thread 全部 job 就只能全表扫描。

---

## 多轮对话：Checkpointer + Reducer 机制

### 每轮对话是一次独立的 graph 执行

信息不足时 Router 返回 END，**graph 直接结束**——不是在同一个 graph 里循环。下一条消息触发一次全新的 graph 执行（新 job_id），但带同一个 `thread_id`：

```
第一轮：POST { message: "我支付失败了" }
  → job_001, graph 执行 #1
  → router: intent=tech_issue, user_id=None → END
  → checkpointer 把 final_state 存入 DynamoDB (thread_id=thread_abc)

第二轮：POST { message: "我的 ID 是 u_12345", thread_id: "thread_abc" }
  → job_002, graph 执行 #2（全新的 graph，不是上一个在继续）
  → checkpointer 从 DynamoDB 加载历史 state
  → router 看到完整 3 条消息历史 → log_analyzer → rag → reply + bug_report
```

### 消息追加：`add_messages` reducer

`main.py` 每次只传入当前这条新消息，但 `state.py` 给 `messages` 字段标了 reducer：

```python
messages: Annotated[List[BaseMessage], add_messages]
```

LangGraph 加载 checkpoint 后用 reducer 合并新旧消息——**追加，不是替换**。

### Reducer 一览

| 字段 | reducer | 行为 |
|------|---------|------|
| `raw_user_input` | 无 | 新值覆盖旧值 |
| `messages` | `add_messages` | 新消息追加到历史后面 |
| `synthesizer` | `merge_synthesizer` | 合并 reply_agent 和 bug_report_agent 的并行输出 |

对于没 reducer 的字段，每轮 `initial_state` 的值（通常是 `None`）会覆盖 checkpoint 恢复的值——这是期望行为，因为每轮要重新跑 router/log_analyzer/rag。

### `max_clarification_iterations` 保险

追问 3 轮用户还没给 user_id，`iteration_count >= 3` 触发，强制进 `log_analyzer_agent` 用有限信息尽力诊断，避免无限循环。

---

# 深入：基础设施

## DynamoDB GSI: severity-service-index

`iodp-bug-tickets-{env}` 主表 PK=`report_id`、SK=`generated_at`，能高效查"某工单的详情"。但运维更常问的是：

> "所有 Critical 级别的、影响 payment-service 的工单有哪些？"

没 GSI 只能 `Scan`，慢且贵。GSI `severity-service-index`：

| | 主表 | GSI `severity-service-index` |
|---|---|---|
| Hash Key | `report_id` | `severity` |
| Range Key | `generated_at` | `affected_service` |

```python
table.query(
    IndexName="severity-service-index",
    KeyConditionExpression="severity = :s AND affected_service = :svc",
    ExpressionAttributeValues={":s": "Critical", ":svc": "payment-service"},
)
```

`projection_type = "ALL"` 让 GSI 复制全部字段，查询不需要回主表，代价是存储翻倍。**主键决定数据怎么存，GSI 决定数据还能怎么查**。

---

## S3 Vectors RAG：两个 index 混合搜索

同一个 vector bucket 下挂两个 index，rag_agent 同时搜，按相关度混排返回 top 5：

```
incident_solutions（自动灌入）                product_docs（人工灌入）
─────────────────────                         ─────────────────
来源：Gold incident_summary                   来源：运维手册、API 文档、FAQ
灌入：S3 Event → vector_indexer Lambda 自动    灌入：make index-kb 离线脚本
更新：每天随 incident_summary 自动更新         更新：文档变更后手动重新索引
回答："上次出现类似问题是怎么回事？"           回答："这个错误码怎么排查？"
```

互补：payment-service 报 E2001 时，
- `incident_solutions` → "上周也出现过 E2001，当时是数据库连接池满了"
- `product_docs`       → "E2001 排查步骤：1. 检查连接池配置 2. 检查慢查询..."

### incident_solutions 数据链路

```
gold_incident_summary.py 生成 incident record
    → Gold S3 parquet（stat_date 分区）
    → S3 Event 自动触发 vector_indexer Lambda（bigdata 侧）
    → Qwen text-embedding-v3 生成 1024 维向量
    → S3 Vectors put_vectors → incident_solutions index
    → RAG Agent vector_search() 检索
```

### 数据量估算

`incident_summary` 是高度聚合的故障摘要（同 service + error_code 连续 ≥2h error_rate > 5% 才算一条）。典型 10 微服务系统 5 年 ~2000-3000 条，存储 ~12 MB。`make index-kb` 全量重建 ~10 分钟（瓶颈是 embedding API ~200ms/条）。

### RAGDocument 字段映射

| RAGDocument | S3 Vectors 来源 |
|---|---|
| `doc_id` | `vec["key"]` |
| `title` / `content` | `vec["metadata"]["title"|"content"]`（non-filterable） |
| `doc_type` | `vec["metadata"]["doc_type"]`（filterable，`incident_solution` / `product_doc`） |
| `relevance_score` | `1 - vec["distance"]`（cosine） |
| `error_codes` | `vec["metadata"]["error_codes"]`（filterable，支持 `$in`） |

---

## API Gateway 限流（令牌桶）

```hcl
default_route_settings {
  throttling_rate_limit  = 20    # 稳态：每秒最多 20 个请求
  throttling_burst_limit = 50    # 突发：瞬间最多 50 个请求
}
```

令牌桶：桶容量 = `burst_limit`（50），补充速度 = `rate_limit`（20/秒），每个请求消耗 1 令牌，桶空了返回 `429`。

**每秒 30 个请求持续 10 秒**：
```
第 1-3 秒：桶里有积累，30 全部通过
第 4 秒及之后：桶里只剩 20，通过 20、拒绝 10
汇总：通过 230，拒绝 70，429 率 ~23%
```

429 被 API Gateway 直接返回，**不调用 Lambda**——不产生 Lambda 费用。这是防恶意刷接口 / 前端 bug 无限轮询打爆 Lambda 的保护。

---

## API Gateway 与 Lambda 集成（AWS_PROXY）

```hcl
integration_type       = "AWS_PROXY"
integration_method     = "POST"        # 内部通信，不是用户的方法
payload_format_version = "2.0"
```

`integration_method = "POST"` 是 **API Gateway 调用 Lambda 时的内部通信方法**，不是用户的请求方法。用户的 GET 保存在 event 里由 Mangum 还原：

```
用户 GET /diagnose/job_001
   ↓
API Gateway 匹配路由 "GET /diagnose/{job_id}"
   ↓
以 POST 方式调用 Lambda，event JSON 里 method=GET, pathParameters={job_id: job_001}
   ↓
Mangum 解析 event → 还原为 GET /diagnose/job_001
   ↓
FastAPI 路由到 get_diagnosis_result(job_id="job_001")
```

`AWS_PROXY` 模式：API Gateway 把整个请求原样打包成 JSON 扔给 Lambda，Lambda 自己解析路由。

---

## S3 + CloudFront 前端托管安全模型

### S3 全 block_public，前端怎么访问？

S3 桶对公网完全封闭（`block_public_access` 全部 true），但通过 OAI（Origin Access Identity）给 CloudFront 开了专属通道：

```
公网用户 → S3 直接访问       ✗ 被 block_public_access 拦截
公网用户 → CloudFront → S3   ✓ CloudFront 用 OAI 身份读取 S3
```

```hcl
resource "aws_cloudfront_origin_access_identity" "frontend" { ... }

resource "aws_s3_bucket_policy" "frontend" {
  policy = {
    Principal = { AWS = oai.iam_arn }   # 只有 CloudFront 能读
    Action    = "s3:GetObject"          # 只读，不能写
  }
}
```

强制所有流量走 CDN，不让人绕过去裸读 S3。

### CloudFront 缓存 TTL

```
min_ttl     = 0        # 最短缓存：0 秒
default_ttl = 86400    # 默认缓存：1 天（S3 没指定 Cache-Control 时用这个）
max_ttl     = 31536000 # 最长缓存：365 天
```

实际缓存时间取决于 S3 文件的 `Cache-Control` 响应头，在 min 和 max 之间尊重 S3 设置。`make deploy-frontend` 会执行 `cloudfront create-invalidation --paths "/*"` 强制清边缘缓存，平时靠 `default_ttl = 1 天` 减少回源费用。

---

# 深入：前端 + 配置

## 前端消息流：临时占位 filter

`App.jsx` 的 `messages` 一直 push，但 "正在分析中..." 是临时占位，轮询完成后要替换掉：

```
1. push 用户消息            [{ user, "我支付失败了" }]
2. push 占位消息            [..., { system, "正在分析中..." }]
3. 轮询完成 → filter 掉占位  [{ user, ... },
                              { assistant, "很抱歉..." },
                              { report, "{...}" }]
```

```javascript
setMessages(prev => {
  const filtered = prev.filter(m => m.content !== '正在分析中...')
  const msgs = [...filtered]
  if (result.user_reply) msgs.push({ role: 'assistant', ... })
  if (result.bug_report) msgs.push({ role: 'report', ... })
  return msgs
})
```

不 filter 的话，"正在分析中..." 会和真实结果同时显示。

---

## 配置管理（Pydantic Settings）

`src/config.py` 用 Pydantic `BaseSettings`，读取优先级：**环境变量 > .env 文件 > 代码默认值**。所有环境变量必须加 `IODP_` 前缀（避免和 boto3 冲突）。

| 环境变量 | 默认 | 说明 |
|---------|------|------|
| `IODP_AWS_REGION` | `us-east-1`（Lambda 部署被 Makefile 覆盖为 `ap-southeast-1`） | AWS 区域 |
| `IODP_ENVIRONMENT` | `prod` | 环境：prod/staging/dev |
| `IODP_AGENT_STATE_TABLE` | `iodp-agent-state-prod` | LangGraph Checkpointer 表 |
| `IODP_AGENT_JOBS_TABLE` | `iodp-agent-jobs-prod` | 异步 Job 跟踪表 |
| `IODP_DQ_REPORTS_TABLE` | `iodp-dq-reports-prod` | BigData DQ 报告表（跨项目读取） |
| `IODP_VECTOR_BUCKET_NAME` | （必填） | S3 Vectors bucket 名（terraform output `vector_bucket_name`） |
| `IODP_LLM_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | LLM provider（OpenAI 兼容） |
| `IODP_LLM_API_KEY` | （必填） | dashscope / deepseek / glm / openai key |
| `IODP_LLM_REASONING_MODEL` | `qwen-max` | 复杂推理（log_analyzer / bug_report） |
| `IODP_LLM_ROUTER_MODEL` | `qwen-turbo` | 简单分类（router / rag / reply），便宜 ~5x |
| `IODP_EMBEDDING_MODEL` | `text-embedding-v3` | qwen v3，输出 1024 维 |
| `IODP_EMBEDDING_DIMENSIONS` | `1024` | 必须等于 S3 Vectors index dimension |
| `IODP_ATHENA_RESULT_BUCKET` | `iodp-athena-results-prod` | Athena 查询结果 S3 |
| `IODP_ATHENA_MAX_ROWS` | `50` | Athena 结果截断行数（防 Token 超限） |
| `IODP_MAX_CLARIFICATION_ITERATIONS` | `3` | 最大追问轮数 |
| `IODP_ASYNC_JOB_TTL_SECONDS` | `3600` | Job 记录过期时间（秒） |

**切换 LLM provider** 只需改这几行 + 重 deploy：
```
DeepSeek: IODP_LLM_BASE_URL=https://api.deepseek.com    IODP_LLM_REASONING_MODEL=deepseek-chat
通义千问: IODP_LLM_BASE_URL=…dashscope…                  IODP_LLM_REASONING_MODEL=qwen-max
智谱 GLM: IODP_LLM_BASE_URL=https://open.bigmodel.cn/…   IODP_LLM_REASONING_MODEL=glm-4
OpenAI:  IODP_LLM_BASE_URL=（留空）                       IODP_LLM_REASONING_MODEL=gpt-4o
```

本地开发可在 `iodp-agent/.env` 设置（不提交到 git）。Lambda 部署时由 Terraform 通过环境变量注入。

---

## 已删除的死代码

以下文件因无任何引用已移除：

| 文件 | 原因 |
|------|------|
| `src/models/request_models.py` | `DiagnoseRequest` 在 `main.py` 里重新定义了，此文件无人 import |
| `src/models/output_models.py` | `BugReportOutput`、`DiagnosisResult` 无人 import，功能已由 `state.py` 的 TypedDict 覆盖 |
| `src/tools/schema_tool.py` | 3 个函数（get_table_schema、list_available_tables、schema_summary_for_llm）无人调用，Log Analyzer 的表结构硬编码在 prompt 中 |
