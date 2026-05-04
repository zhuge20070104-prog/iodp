# IODP Agent 使用说明

## 异步 Job 模式

Agent 采用异步 Job 模式处理诊断请求，避免 API Gateway 29 秒超时问题。

### 交互流程

```
前端                         API Gateway + Lambda                  DynamoDB
 │                                  │                                │
 │ POST /diagnose                   │                                │
 │ { message, thread_id? }          │                                │
 │ ─────────────────────────────►   │                                │
 │                                  │  put_item(status="queued")     │
 │                                  │ ──────────────────────────────►│
 │   202 { job_id, status="queued"} │                                │
 │ ◄─────────────────────────────── │                                │
 │                                  │  [后台] LangGraph 执行         │
 │                                  │  update(status="running")      │
 │  GET /diagnose/{job_id}          │  ...                           │
 │ ─────────────────────────────►   │  update(status="completed",    │
 │   { status: "running" }          │         result_json=...)       │
 │ ◄─────────────────────────────── │                                │
 │                                  │                                │
 │  GET /diagnose/{job_id}          │                                │
 │ ─────────────────────────────►   │  get_item(job_id)              │
 │   { status: "completed",         │ ◄────────────────────────────  │
 │     result: { user_reply,        │                                │
 │               bug_report } }     │                                │
 │ ◄─────────────────────────────── │                                │
```

建议客户端每 2-3 秒轮询一次 `GET /diagnose/{job_id}`，最多等待 120 秒。

### 为什么用异步模式：BackgroundTasks

`BackgroundTasks` 是 FastAPI 内置的机制，在当前请求返回响应**之后**异步执行耗时任务：

```python
@app.post("/diagnose", status_code=202)
async def submit_diagnosis(request, background_tasks: BackgroundTasks):
    _create_job_record(job_id, ...)                            # 1. 写 DynamoDB
    background_tasks.add_task(run_graph_job, job_id, ...)      # 2. 丢到后台
    return JobResponse(job_id=job_id, status="queued")         # 3. 立即返回 202
```

```
时间线：
  ──────────────────────────────────────────────────────→
  │ POST 进来 │ 返回 202 │ 后台跑 LangGraph（30 秒~几分钟）│
                ▲                       ▲
                │                       │
           用户拿到 job_id         跑完写 DynamoDB
           开始轮询 GET            status → "completed"
```

如果不用 `BackgroundTasks`，LangGraph 执行期间请求一直挂着，API Gateway 29 秒就超时断开。这是 v1 → v2 的核心改进（见 `main.py` 注释）。

### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/diagnose` | 提交诊断请求，返回 202 + job_id |
| GET | `/diagnose/{job_id}` | 轮询 Job 状态和结果 |
| GET | `/health` | 健康检查（无需认证） |

### 请求示例

```bash
# 第一轮对话
curl -X POST https://{api-endpoint}/diagnose \
  -H "Content-Type: application/json" \
  -d '{"message": "我昨晚11点支付一直失败，页面卡在加载中"}'

# 返回：{"job_id": "xxx", "status": "queued", "thread_id": "thread_xxx"}

# 轮询结果
curl https://{api-endpoint}/diagnose/xxx

# 第二轮对话（传入相同 thread_id）
curl -X POST https://{api-endpoint}/diagnose \
  -H "Content-Type: application/json" \
  -d '{"message": "我的用户 ID 是 u_12345", "thread_id": "thread_xxx"}'
```

---

## DynamoDB job_id vs thread_id

`iodp-agent-jobs-{env}` 表用 `job_id` 做主键，`thread_id` 做 GSI（Global Secondary Index）。

**一个 thread 会产生多个 job**，因为多轮对话中每次用户发消息都是一个独立的 job：

```
用户第一轮："我支付失败了"
  → job_id: job_001, thread_id: thread_abc
  → Agent："请问您的用户 ID 是多少？"

用户第二轮："我的 ID 是 u_12345"
  → job_id: job_002, thread_id: thread_abc（同一个 thread）
  → Agent："查到了，昨晚 payment-service 有故障..."

用户第三轮："帮我看一下具体的错误码"
  → job_id: job_003, thread_id: thread_abc（还是同一个 thread）
```

DynamoDB 里三条记录：

| job_id (PK) | thread_id (GSI) | status | result_json |
|---|---|---|---|
| job_001 | thread_abc | completed | "请问您的用户 ID..." |
| job_002 | thread_abc | completed | "查到了，昨晚..." |
| job_003 | thread_abc | running | "" |

两种查询方式：

```
按 job_id 查（主键）：
  GET /diagnose/job_003  →  "这个 job 跑到哪了？"

按 thread_id 查（GSI）：
  query(thread_id = "thread_abc")  →  "这个对话的所有 job 历史"
```

GSI 让 DynamoDB 在后台维护一份按 `thread_id` 排列的索引。没有 GSI，想查某个 thread 的所有 job 就只能全表扫描。

---

## DynamoDB GSI 详解：severity-service-index

`iodp-bug-tickets-{env}` 表的 GSI `severity-service-index` 提供按严重级别和服务维度查询工单的能力。

### 主表 vs GSI 对比

| | 主表 | GSI `severity-service-index` |
|---|---|---|
| **Hash Key** | `report_id`（工单 ID） | `severity`（严重级别） |
| **Range Key** | `generated_at`（生成时间） | `affected_service`（受影响服务） |
| **用途** | 按工单 ID 查单条记录 | 按严重级别 + 服务名筛选 |

### 为什么需要这个 GSI

DynamoDB 主表只能按主键（`report_id`）高效查询。但实际运维场景中，更常见的查询是：

> "所有 **Critical** 级别的、影响 **payment-service** 的工单有哪些？"

没有 GSI，这种查询只能全表扫描（Scan），慢且贵。有了这个 GSI，就变成一次精确的 Query：

```python
table.query(
    IndexName="severity-service-index",
    KeyConditionExpression="severity = :s AND affected_service = :svc",
    ExpressionAttributeValues={
        ":s": "Critical",
        ":svc": "payment-service"
    }
)
```

### projection_type = "ALL" 的含义

GSI 会复制主表的数据到索引中。`ALL` 表示**复制全部字段**，查询时不需要回主表取数据（无额外读开销），代价是存储翻倍。

其他选项：
- `KEYS_ONLY` — 只复制主键，省存储但查完还要回表取其他字段
- `INCLUDE` — 指定复制哪些字段，折中方案

### 典型查询场景

| 查询 | 方式 |
|---|---|
| 查某个工单详情 | 主表 Query `report_id = X` |
| 某天所有工单 | 主表 Query `report_id = X` + `generated_at` 范围 |
| 所有 Critical 工单 | GSI Query `severity = "Critical"` |
| Critical 且影响 auth-service | GSI Query `severity = "Critical" AND affected_service = "auth-service"` |

简单说：**主键决定数据怎么存，GSI 决定数据还能怎么查**。这个 GSI 让 bug ticket 系统可以按严重级别和服务维度高效检索。

---

## 多轮对话：Checkpointer + Reducer 机制

### 每轮对话是一次独立的 graph 执行

信息不足时 Router 返回 END，**graph 直接结束**，不是在同一个 graph 里循环。用户下一条消息会触发一次全新的 graph 执行（新 job），但带同一个 `thread_id`：

```
第一轮：POST /diagnose { message: "我支付失败了" }
  → job_001, graph 执行 #1
  → router: intent="tech_issue", user_id=None → END
  → result: "请问您的用户 ID？"
  → checkpointer 把 final_state 存入 DynamoDB (thread_id="thread_abc")

第二轮：POST /diagnose { message: "我的 ID 是 u_12345", thread_id: "thread_abc" }
  → job_002, graph 执行 #2（全新的 graph，不是上一个在继续）
  → checkpointer 从 DynamoDB 加载 thread_abc 的历史 state
  → router: intent="tech_issue"（从 checkpoint 恢复）, user_id="u_12345"（从新消息提取）
  → 有 user_id 了 → log_analyzer → rag → reply + bug_report → END
```

### 消息追加：add_messages reducer

`main.py` 每次只传入当前这条新消息：

```python
initial_state = {
    "messages": [HumanMessage(content=request.message)],  # 只有 "我的 ID 是 u_12345"
    ...
}
```

但 `state.py` 里 `messages` 字段定义了 `add_messages` reducer：

```python
messages: Annotated[List[BaseMessage], add_messages]
```

LangGraph 加载 checkpoint 后，用 reducer 合并新旧消息：

```
checkpoint 里的 messages:  [HumanMessage("我支付失败了"), AIMessage("请问用户ID？")]
+ initial_state 的 messages: [HumanMessage("我的 ID 是 u_12345")]
= 合并后:                    [HumanMessage("我支付失败了"),
                              AIMessage("请问用户ID？"),
                              HumanMessage("我的 ID 是 u_12345")]
```

**是追加，不是替换**。Router 第二轮看到完整的 3 条消息历史。

### Reducer 机制：Annotated 类型注解

`state.py` 里有两个 reducer，控制字段的更新方式：

```python
# 没有 reducer 的字段：新值直接覆盖旧值
raw_user_input: str
# state["raw_user_input"] = "旧" → "新"  → 结果: "新"

# add_messages reducer：新消息追加到历史后面
messages: Annotated[List[BaseMessage], add_messages]
# state["messages"] = [msg1] → [msg2]  → 结果: [msg1, msg2]

# merge_synthesizer reducer：合并并行节点的输出
synthesizer: Annotated[Optional[SynthesizerOutput], merge_synthesizer]
# reply_agent 写 { user_reply: "很抱歉...", bug_report: None }
# bug_report_agent 写 { user_reply: None, bug_report: {...} }
# → 合并结果: { user_reply: "很抱歉...", bug_report: {...} }
```

### initial_state 与 checkpoint 的覆盖关系

每轮 graph 执行时，`main.py` 传入的 `initial_state` 如下：

```python
initial_state = {
    "messages":     [HumanMessage(content=request.message)],  # 有 reducer → 追加
    "raw_user_input": request.message,                         # 无 reducer → 覆盖
    "iteration_count": 0,                                      # 无 reducer → 覆盖
    "router":       None,                                      # 无 reducer → 覆盖
    "log_analyzer": None,                                      # 无 reducer → 覆盖
    "rag":          None,                                      # 无 reducer → 覆盖
    "synthesizer":  None,                                      # 有 reducer，但 None 不影响合并
}
```

对于没有 reducer 的字段，**initial_state 的值优先于 checkpoint 恢复的值**。所以：

- `router: None` 会覆盖掉上一轮 checkpoint 里保存的 `{intent: "tech_issue", user_id: None}`。这不是问题，因为 Router Agent 会重新跑一遍，从完整的 messages 历史里重新提取 intent 和 user_id。
- `synthesizer: None` 会清掉上一轮的回复和报告。这是期望行为，用户只关心当前这轮的结果。
- `messages` 有 `add_messages` reducer，所以新消息追加到 checkpoint 历史后面，不会被 None 覆盖。

特殊情况：如果前端传了 `user_id`，会预填充 `router` 子结构，跳过 Router 自己提取的过程：

```python
if request.user_id:
    initial_state["router"] = {
        "intent": None,
        "user_id": request.user_id,   # 前端已知，直接注入
        ...
    }
```

### max_clarification_iterations 保险机制

如果追问了 3 轮用户还没提供 user_id，`iteration_count >= 3` 触发，强制跳过追问进入 `log_analyzer_agent`，用有限信息尽力诊断，避免无限循环。

---

## 配置管理

配置通过 `src/config.py` 的 Pydantic Settings 管理，读取优先级：**环境变量 > .env 文件 > 代码默认值**。

所有环境变量需要加 `IODP_` 前缀（避免和 boto3 等框架的变量冲突）：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `IODP_AWS_REGION` | us-east-1 | AWS 区域 |
| `IODP_ENVIRONMENT` | prod | 环境：prod/staging/dev |
| `IODP_AGENT_STATE_TABLE` | iodp-agent-state-prod | LangGraph Checkpointer 表 |
| `IODP_AGENT_JOBS_TABLE` | iodp-agent-jobs-prod | 异步 Job 跟踪表 |
| `IODP_DQ_REPORTS_TABLE` | iodp-dq-reports-prod | BigData DQ 报告表（跨项目读取） |
| `IODP_VECTOR_BUCKET_NAME` | （必填） | S3 Vectors bucket 名称（terraform output `vector_bucket_name`） |
| `IODP_BEDROCK_MODEL_ID` | anthropic.claude-3-5-sonnet-... | LLM 模型 |
| `IODP_ATHENA_RESULT_BUCKET` | iodp-athena-results-prod | Athena 查询结果 S3 |
| `IODP_ATHENA_MAX_ROWS` | 50 | Athena 结果截断行数 |
| `IODP_MAX_CLARIFICATION_ITERATIONS` | 3 | 最大追问轮数 |
| `IODP_ASYNC_JOB_TTL_SECONDS` | 3600 | Job 记录过期时间（秒） |

本地开发时可以在 `iodp-agent/.env` 文件中设置（不提交到 git）：

```bash
IODP_AWS_REGION=ap-southeast-1
IODP_ENVIRONMENT=dev
IODP_VECTOR_BUCKET_NAME=iodp-rag-dev
```

Lambda 部署时由 Terraform 通过 Lambda 环境变量注入，不需要 `.env` 文件。

---

## S3 Vectors RAG 知识库：两个索引

RAG Agent 同时搜索同一个 vector bucket 下的两个 index，结果按相关度分数混排返回 top 5：

```python
raw_hits = vector_search(
    index_names=["product_docs", "incident_solutions"],   # 同时搜两个
    top_k=5,
    ...
)
```

### 两个索引的区别

```
incident_solutions（自动）                product_docs（人工）
─────────────────────                    ─────────────────
来源：Gold incident_summary              来源：运维手册、API 文档、FAQ
灌入：S3 Event → Lambda 自动触发         灌入：离线脚本手动执行（make index-kb）
内容：系统检测到的历史故障摘要            内容：人工编写的排查指南
更新：每天随 incident_summary 自动更新    更新：文档更新后手动重新索引
回答："上次出现类似问题是怎么回事？"      回答："这个错误码应该怎么排查？"
```

两者互补：比如 payment-service 报 E2001 时，
- `incident_solutions` 返回："上周也出现过 E2001，当时是数据库连接池满了"
- `product_docs` 返回："E2001 排查步骤：1. 检查连接池配置 2. 检查慢查询..."

### incident_solutions 的完整数据链路

```
gold_incident_summary.py 生成 incident record
    → Gold S3 parquet（stat_date 分区）
    → S3 Event 自动触发 vector_indexer Lambda
    → handler.py _build_vector_record() 构造记录（key + float32 + metadata）
    → Bedrock Titan Embedding 生成 1024 维向量
    → S3 Vectors put_vectors → incident_solutions index
    → RAG Agent vector_search() 检索 (query_vectors)
    → RAGDocument 各字段
```

### incident_solutions 数据量估算

`incident_summary` 不是流水日志，而是高度聚合后的故障事件摘要。产出规则：同一 service + error_code 连续 >=2 小时 error_rate > 5% 才算一条 incident。

```
典型系统（10 个微服务，每月 2-3 次故障/服务）：
  每月：~30-50 条 incident
  每年：~400-600 条
  5 年：~2000-3000 条

不稳定系统（故障频繁）：
  5 年：~12000 条（上限估算）
```

全量重建索引耗时（瓶颈仍是 Bedrock Embedding API，~200ms/条）：

| 数据量 | 索引时间 | S3 Vectors 存储 |
|--------|---------|----------------|
| 3000 条（正常 5 年） | ~10 分钟 | ~12 MB（含 metadata） |
| 12000 条（极端 5 年） | ~40 分钟 | ~50 MB |

数据量很小，`make index-kb` 全量重建没有性能压力。日常增量由 bigdata 端的 `vector_indexer` Lambda 自动处理，每天只新增几条。

### product_docs 的导入方式

当前代码库没有自动化管道，需要运维团队：
1. 人工编写产品文档（运维手册、FAQ、排查指南）
2. 通过 `make index-kb` 调用 `scripts/index_knowledge_base.py` 离线导入

**注意**：S3 Vectors index 由 Terraform 创建，部署后即存在；空 index 查询返回 0 命中而非异常。代码层面单 index 失败也不会中断流程，仅日志告警。

### 搜索结果字段映射

两个索引都有相同的字段结构，RAGDocument 不需要区分来源：

| RAGDocument 字段 | S3 Vectors 来源 | 说明 |
|---|---|---|
| `doc_id` | `vec["key"]` | 向量唯一 ID（incident_id 或文档 key） |
| `title` | `vec["metadata"]["title"]` | 文档标题（non-filterable） |
| `content` | `vec["metadata"]["content"]` | 文档正文（non-filterable） |
| `doc_type` | `vec["metadata"]["doc_type"]` | `"incident_solution"` 或 `"product_doc"`，filterable |
| `relevance_score` | `1 - vec["distance"]` | 由 cosine distance 转换得到（越大越相似） |
| `error_codes` | `vec["metadata"]["error_codes"]` | 关联错误码列表，filterable（支持 `$in` 过滤） |

---

## API Gateway 限流机制（Throttling）

`terraform/main.tf` 中 API Gateway stage 配置了两个限流参数：

```hcl
default_route_settings {
  throttling_rate_limit  = 20    # 稳态：每秒最多 20 个请求
  throttling_burst_limit = 50    # 突发：瞬间最多 50 个请求
}
```

### 令牌桶算法

限流基于令牌桶（Token Bucket）：
- 桶容量 = `burst_limit`（50 个令牌）
- 补充速度 = `rate_limit`（每秒 20 个）
- 每个请求消耗 1 个令牌
- 桶空了 → 返回 `429 Too Many Requests`

### 具体场景：每秒 30 个请求持续 10 秒

```
第 1 秒：进来 30，桶里 50 令牌
  消耗 30，补充 20 → 桶剩 40
  通过 30，拒绝 0

第 2 秒：进来 30，桶里 40
  消耗 30，补充 20 → 桶剩 30
  通过 30，拒绝 0

第 3 秒：进来 30，桶里 30
  消耗 30，补充 20 → 桶剩 20
  通过 30，拒绝 0

第 4 秒：进来 30，桶里 20
  消耗 20 → 桶空了，剩 10 个请求没令牌 → 429
  补充 20 → 桶回 20，但这 10 个已经被拒
  通过 20，拒绝 10

第 5-10 秒：同第 4 秒，每秒通过 20，拒绝 10

汇总：通过 230，拒绝 70，429 率 ~23%
```

### 429 的处理

- 被 429 拒掉的请求，API Gateway **直接返回响应，不调用 Lambda**，不产生 Lambda 费用
- 前端收到 429 后应做退避重试（等 1-2 秒再发）
- 这是防止恶意刷接口或前端 bug 导致无限轮询把 Lambda 打爆的保护机制

---

## API Gateway 与 Lambda 的集成方式

```hcl
resource "aws_apigatewayv2_integration" "lambda" {
  integration_type       = "AWS_PROXY"
  integration_method     = "POST"      # 这不是用户的 HTTP 方法
  payload_format_version = "2.0"
}
```

`integration_method = "POST"` 是 **API Gateway 调用 Lambda 时的内部通信方法**，不是用户的请求方法。用户的 GET/POST 保存在 event 里由 Mangum 还原：

```
用户 GET /diagnose/job_001
    ↓
API Gateway 匹配路由 "GET /diagnose/{job_id}"
    ↓
以 POST 方式调用 Lambda，传入 event JSON：
{
  "requestContext": { "http": { "method": "GET" } },
  "rawPath": "/diagnose/job_001",
  "pathParameters": { "job_id": "job_001" }
}
    ↓
Mangum 解析 event → 还原为 GET /diagnose/job_001
    ↓
FastAPI 路由到 get_diagnosis_result(job_id="job_001")
```

这就是 `AWS_PROXY` 模式：API Gateway 把整个请求原样打包成 JSON 扔给 Lambda，Lambda 自己解析路由。

---

## S3 + CloudFront 前端托管安全模型

### S3 全部 block，前端怎么访问

S3 桶对公网完全封闭（`block_public_access` 全部 true），但通过 OAI（Origin Access Identity）给 CloudFront 开了专属通道：

```
公网用户 → S3 直接访问       ✗ 被 block_public_access 拦截
公网用户 → CloudFront → S3   ✓ CloudFront 用 OAI 身份读取 S3
```

实现方式：

```hcl
# 1. 创建 CloudFront 专属身份（OAI）
resource "aws_cloudfront_origin_access_identity" "frontend" { ... }

# 2. S3 Bucket Policy 只允许这个身份读取
resource "aws_s3_bucket_policy" "frontend" {
  policy = {
    Principal = { AWS = oai.iam_arn }   # 只有 CloudFront 能读
    Action    = "s3:GetObject"          # 只读，不能写
  }
}
```

强制所有流量走 CDN，不让人绕过去直接读 S3。这是 AWS 前端托管的标准安全实践。

### CloudFront 缓存 TTL

三个值控制 CloudFront 边缘节点缓存静态文件多久：

```
min_ttl     = 0        # 最短缓存：0 秒（S3 说不缓存就不缓存）
default_ttl = 86400    # 默认缓存：1 天（S3 没指定 Cache-Control 时用这个）
max_ttl     = 31536000 # 最长缓存：365 天（S3 说缓存再久也不超过这个上限）
```

实际缓存时间取决于 S3 文件的 `Cache-Control` 响应头：

| S3 文件的 Cache-Control | CloudFront 实际缓存 |
|---|---|
| 没设置 | `default_ttl` = 1 天 |
| `max-age=3600`（1 小时） | 1 小时（在 min 和 max 之间，尊重 S3） |
| `max-age=0`（不缓存） | `min_ttl` = 0 秒（尊重 S3） |
| `max-age=99999999`（3 年） | `max_ttl` = 365 天（封顶） |

前端部署更新时，`make deploy-frontend` 会执行 `cloudfront create-invalidation --paths "/*"` 强制清除边缘缓存，用户立刻看到新版本。平时靠 `default_ttl = 1 天` 减少回源次数，省 S3 请求费用。

---

## 前端消息流：临时占位消息的 filter 机制

`App.jsx` 的消息列表 `messages` 是一直往里 push 的，但 "正在分析中..." 是个**临时占位消息**，轮询完成后要替换掉，不能保留：

```
用户点发送 → messages 状态变化：

1. push 用户消息
   [{ role: "user", content: "我支付失败了" }]

2. push 占位消息
   [{ role: "user", ... }, { role: "system", content: "正在分析中..." }]

3. 轮询完成，filter 掉占位 → push 真实结果
   [{ role: "user", ... },
    { role: "assistant", content: "很抱歉..." },     ← 替换了 "正在分析中..."
    { role: "report", content: "{...}" }]

如果出错，同样 filter 掉占位 → push 错误消息
   [{ role: "user", ... },
    { role: "error", content: "诊断超时" }]          ← 替换了 "正在分析中..."
```

代码实现：

```javascript
// 轮询成功
setMessages(prev => {
  const filtered = prev.filter(m => m.content !== '正在分析中...')  // 移除占位
  const msgs = [...filtered]
  if (result.user_reply) msgs.push({ role: 'assistant', ... })     // 加真实回复
  if (result.bug_report) msgs.push({ role: 'report', ... })        // 加 Bug 报告
  return msgs
})

// 轮询失败
setMessages(prev => {
  const filtered = prev.filter(m => m.content !== '正在分析中...')  // 同样移除占位
  return [...filtered, { role: 'error', content: err.message }]    // 加错误消息
})
```

不 filter 的话，"正在分析中..." 会永远留在聊天记录里，和真实结果同时显示。

---

## 部署命令

| 命令 | 用途 |
|------|------|
| `make init` | 首次一键部署：Terraform + ECR + Docker + Lambda + 测试数据 + RAG 索引 |
| `make deploy` | 日常更新：重建镜像 + 更新 Lambda |
| `make deploy-frontend` | 只更新前端：build React + 上传 S3 + 刷新 CloudFront |
| `make deploy-all` | 全量更新：后端 + 前端 |
| `make destroy` | 销毁全部资源（停止计费） |
| `make test-api` | 发送端到端测试请求 |
| `make status` | 查看部署状态 |
| `make seed-data` | 注入测试数据 |
| `make index-kb` | 索引 RAG 知识库到 S3 Vectors |
