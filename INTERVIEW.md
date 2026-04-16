# IODP 项目面试深度问答

## 1. 架构选型

### 1.1 为什么选 Iceberg 不用 Delta Lake？

**回答要点**：

两者都是开放表格式，核心能力（ACID、时间旅行、Schema 演化）高度重叠。选 Iceberg 的原因：

| 维度 | Iceberg | Delta Lake |
|------|---------|------------|
| **AWS 原生支持** | Glue、Athena、EMR 原生支持，零配置 | 需要额外安装 delta-spark 包，Athena 支持有限 |
| **Catalog 集成** | 直接用 Glue Data Catalog 做元数据管理 | 依赖 Delta UniForm 或 Hive Metastore |
| **引擎中立性** | Spark、Flink、Trino、Presto 全支持 | Spark 优先，其他引擎支持滞后 |
| **社区趋势** | AWS、Apple、Netflix 主推 | Databricks 主推，和 Databricks 平台绑定较深 |

**面试追问：时间旅行是什么？Delta Lake 不也支持吗？**

时间旅行是查历史某个时间点的数据快照：

```sql
-- 查当前数据
SELECT * FROM app_logs WHERE service_name = 'payment-service';

-- 查 3 天前的数据
SELECT * FROM app_logs
FOR SYSTEM_TIME AS OF TIMESTAMP '2026-04-13 00:00:00';
```

原理：Iceberg 每次写入生成一个快照（snapshot），不覆盖旧数据文件。查历史就是读旧快照引用的文件集合。

Delta Lake 确实也支持时间旅行（`VERSION AS OF` / `TIMESTAMP AS OF`），这不是选 Iceberg 的理由。两者在时间旅行上的差异主要是实现细节：

| | Iceberg | Delta Lake |
|---|---|---|
| 语法 | `FOR SYSTEM_TIME AS OF` | `VERSION AS OF` / `TIMESTAMP AS OF` |
| 快照管理 | `previous-versions-max` 控制保留数 | `delta.logRetentionDuration` 控制保留时间 |
| Athena 支持 | 原生支持时间旅行查询 | Athena 对 Delta 时间旅行支持有限 |

本项目 DDL 里 `write.metadata.previous-versions-max = 10` 就是只保留 10 个历史快照，超过后旧快照和它引用的数据文件被清理，防止存储膨胀。

实际用途：数据回溯（"写错了想看之前的样子"）、审计（"字段什么时候变的"）、误操作恢复（回退到上一个快照）。

**面试追问：Iceberg 的隐藏文件过滤（Hidden Partition）怎么工作？**

传统 Hive 分区要求查询 WHERE 里写分区列，否则全表扫描。Iceberg 的元数据层记录了每个数据文件的列级统计（min/max/null count），即使不按分区列查，Iceberg 也能跳过不相关的文件。

本项目的实际例子：Bronze `app_logs` 按 `event_date + log_level` 分区，但 `service_name` 不是分区列。查询 `WHERE service_name = 'payment-service'` 时，Iceberg 通过元数据文件里的 min/max 统计自动跳过不含该 service 的 parquet 文件，效果接近分区裁剪但不需要把 service_name 设为分区键。

**面试追问：为什么 Bronze 不按 service_name 分区？**

v1 最初按 `service_name + log_level` 分区。问题是低流量服务产生大量小文件。v2 改为按 `event_date + log_level` 分区，service_name 降为普通列，靠 Iceberg metadata filtering 弥补。

**具体量化：小文件如何影响查询性能**

以 notification-service（低流量，每 batch 5 条记录）为例：

```
方案 A：按 service_name 分区 → 1440 个小文件
──────────────────────────────────────────

Athena 执行过程：
  1. S3 LIST：列出分区下所有文件 → 返回 1440 个文件路径
  2. 读取每个文件的 Parquet footer（元数据）→ 1440 次 S3 GET
  3. 所有文件都命中 → 读取数据 → 1440 次 S3 GET

  S3 API 调用：~2881 次
  数据总量：1440 × 2KB = 2.8MB（极小）
  查询耗时：3-8 秒（瓶颈在网络往返次数，不是数据量）
  S3 API 费用：$0.014


方案 B：按 event_date 分区 + Iceberg compaction → 10 个大文件
────────────────────────────────────────────────────────────

所有 service 的数据混在同一个分区，compaction 合并后只有 10 个文件：
  1. S3 LIST → 10 个文件
  2. 读取 10 个 footer → Iceberg metadata filtering 发现只有 3 个文件
     包含 notification-service 的数据 → 跳过其余 7 个
  3. 读取 3 个文件，Parquet 列式存储只扫描 service_name 列做过滤

  S3 API 调用：~14 次
  查询耗时：1-2 秒
  S3 API 费用：$0.000007


对比：
|                 | 1440 个小文件  | 10 个大文件       |
|-----------------|--------------|-------------------|
| S3 API 调用次数  | ~2881 次      | ~14 次            |
| 网络往返次数     | ~2881 次      | ~14 次            |
| 查询耗时        | 3-8 秒        | 1-2 秒            |
| S3 API 费用     | $0.014        | $0.000007         |
```

核心问题不是数据大，是**文件多**。每个文件不管多小都要一次网络往返。1440 次网络往返 vs 14 次，差 100 倍。

**面试追问：为什么不给 S3 建索引？**

S3 是对象存储，不是数据库，没有索引能力。只能按前缀列出文件然后逐个读。Iceberg 的元数据层（manifest file）就是在 S3 之上人为加的一层"文件级索引" — 能告诉你"这个文件里有没有你要的数据"，但不能告诉你"这个文件第几行是你要的"。要行级索引只能用数据库（RDS、DynamoDB、Redshift），但那就不是数据湖了，成本和架构完全不同。

---

### 1.2 为什么 Agent 用 LangGraph 不用 Claude SDK？

**回答要点**：

核心区别是**谁控制流程**。

```
LangGraph：你定义图 → 你控制路由 → LLM 只在节点内做推理
Claude SDK：你定义 tools → LLM 自己决定调什么、调几次、什么顺序
```

IODP 诊断系统的流程是固定的：

```
tech_issue → 查日志 → 搜知识库 → 回复 + 报告（永远这个顺序）
inquiry → 搜知识库 → 回复（永远这个顺序）
```

不存在"LLM 需要自己判断下一步"的场景。用 Claude SDK 等于把确定性问题变成概率性问题。

**具体差异**：

| 维度 | LangGraph | Claude SDK |
|---|---|---|
| LLM 调用次数 | 固定 5 次（router + SQL + RAG query + reply + report） | 不可预测，Claude 可能反复调 tool |
| 单测 | 每个 node 独立 mock + 测试 | 只能端到端测，LLM 决策不可 mock |
| 并行 | fan-out reply + bug_report 原生支持 | SDK 没有并行编排 |
| 业务规则 | `severity > 20% = P0` 写在代码里 | 写在 prompt 里，LLM 可能不遵守 |
| max_clarification = 3 | `iteration_count >= 3` 代码强制 | "最多追问3次" 写在 prompt 里，可能被忽略 |

**面试追问：Claude SDK 能不能写死流程？**

不能从框架层面强制。但可以变通 — 每步只给 Claude 看一个 tool，它没得选：

```python
# 变通方式：每步只暴露一个 tool
response1 = client.messages.create(tools=[classify_tool], ...)     # 只能分类
response2 = client.messages.create(tools=[query_logs_tool], ...)   # 只能查日志
response3 = client.messages.create(tools=[search_kb_tool], ...)    # 只能搜知识库
```

但这样写本质上就是你在手动编排流程 — 跟 LangGraph 做的事完全一样，只是没有 state 管理、没有 checkpointer、没有并行、没有 reducer，全部自己手搓。

**面试追问：LangGraph 能不能调度 Claude SDK？**

能，而且两者可以互补：LangGraph 管流程编排，Claude SDK 管单步推理。

```
LangGraph（流程控制层）
  ├── router_node       → Claude SDK（分类意图）
  ├── log_analyzer_node → Claude SDK + tool_use（生成 SQL，可让 Claude 自主决定是否多次查询）
  ├── rag_node          → Claude SDK（生成检索 query）
  └── reply + report    → Claude SDK（并行生成）
```

当前项目用的 `ChatBedrock` 就是 LangChain 对 Bedrock Claude API 的封装，本质上已经是 LangGraph 调度 Claude。换成 `anthropic.Anthropic()` 直接调用也完全可以，只是换了调用方式，结果一样。每个 node 里 Claude 只做一件事（生成 SQL / 生成回复），不需要 tool_use 的自主决策能力。

**面试追问：什么场景会选 Claude SDK？**

流程开放、解法不确定的场景。比如编程助手："帮我重构这个函数" — Claude 需要自己决定先读哪些文件、先跑哪些测试。每次任务不同，流程不固定，LLM 自主决策是核心价值。

---

### 1.3 为什么用 API Gateway HTTP API 不用 REST API？

| 维度 | HTTP API | REST API |
|---|---|---|
| **价格** | $1.00/百万请求 | $3.50/百万请求 |
| **延迟** | 更低（轻量级代理） | 更高（功能更多） |
| **JWT 认证** | 原生支持（本项目用 Cognito JWT） | 需要 Lambda Authorizer 或 Cognito |
| **缺少的功能** | 无 API Key 管理、无 Usage Plan、无请求/响应转换 | 全都有 |

本项目不需要 API Key 管理和 Usage Plan（不是开放 API），HTTP API 满足所有需求且**便宜 70%**。

**面试追问：AWS 的 HTTP API 和 REST API 命名是不是和业界概念冲突？**

是的。业界概念里 REST API 是 HTTP API 的一种设计风格（资源用 URL、操作用 HTTP 方法、无状态）。但 AWS 把这两个词当成了两个产品的名字：

```
业界：HTTP API ⊃ REST API（REST 是 HTTP 的子集/风格）
AWS： HTTP API 和 REST API 是两个独立产品（2019 vs 2015）
```

两个 AWS 产品都能做 RESTful API，也都能做非 RESTful API。本项目的 API 设计是 RESTful 的（POST 创建 job，GET 查询 job），只是部署在 AWS HTTP API 产品上。

**面试追问：REST 的"无状态"是什么意思？**

每个请求自包含所有认证信息，服务端不存 session：

```
有状态（session 模式）：
  POST /login → 服务端存 session { id: "abc", user: "张三" }
  GET /diagnose → 只带 Cookie: session_id=abc
  → 服务端查 session 表 → 如果请求打到另一台实例，session 不在那 → 403

无状态（JWT 模式）：
  POST /login → 服务端生成 JWT token（内含 user=张三, role=admin, 过期时间）
  GET /diagnose → 带 Authorization: Bearer eyJhbG...
  → 任何实例都能解码验证 → 不需要查 session 表
```

本项目用 Cognito JWT。每次 login 生成的 token 字符串不同（过期时间不同），但解码出来的身份相同。API Gateway 在 Lambda 之前就验完 JWT，无效 token 根本到不了代码：

```hcl
# Terraform — API Gateway JWT Authorizer
jwt_configuration {
  audience = ["iodp-agent-client"]          # token 的 aud 字段必须匹配
  issuer   = "https://cognito-idp.../..."   # token 必须由这个 Cognito 签发
}
```

无状态的好处：Lambda 天然多实例并发，请求打到任何实例都能处理，不需要共享 session 存储。多轮对话的"状态"存在 DynamoDB（checkpointer），不是服务端内存。

**面试追问：HTTP API 的 29 秒超时怎么解决的？**

没有解决超时本身，而是**绕过了它**。v1 同步调用 LangGraph，29 秒内跑不完就超时。v2 改为异步 Job 模式：

```
POST /diagnose → 立即返回 202 + job_id（< 1秒）
                 后台 BackgroundTasks 跑 LangGraph
GET /diagnose/{job_id} → 轮询状态（每次 < 1秒）
```

每次 HTTP 请求都在 1 秒内完成，不触发 29 秒超时。LangGraph 的执行时间由 Lambda 的 15 分钟超时兜底。

---

## 2. 你发现的 Bug 和设计缺陷

### 2.1 ingest_timestamp 不刷新导致下游捞不到数据

**问题**：DLQ replay 把死信数据写回 Bronze Iceberg 表时，保留了原始的 `ingest_timestamp`（可能是几天前的 Kafka 消费时间）。但下游 `silver_parse_logs.py` 按 `ingest_timestamp` 的小时窗口过滤，只看最近一小时的数据。

```python
# silver_parse_logs.py — 只看最近一小时
bronze_df.filter(
    (col("ingest_timestamp") >= hour_start) &
    (col("ingest_timestamp") < hour_end)
)
```

**后果**：replay 写入 Bronze 的数据永远不会被 Silver job 消费，形成数据黑洞。

**修复**：在 replay job 里刷新 `ingest_timestamp = current_timestamp()`，让下游小时窗口能捞到。

**面试价值**：这是跨组件的系统性问题。单独看 replay job 或 silver job 都没有 bug，只有理解完整数据流才能发现。

### 2.2 affected_service 未系统注入

**问题**：`bug_report_agent.py` 对 LLM 生成的 Bug Report 注入了 7 个系统字段（report_id、user_id、error_codes 等），但漏了 `affected_service`。

**后果**：`affected_service` 完全依赖 LLM 输出。如果 LLM 漏写这个字段，`_validate_bug_report_schema` 校验失败，走 fallback 生成低置信度报告。但其实我们明明有 `top_services` 数据（从 Athena 查询结果提取的），应该直接注入。

**设计原则**：能从数据确定的字段必须系统注入，不让 LLM 生成。LLM 只负责需要推理的字段（severity、root_cause、recommended_fix）。

### 2.3 incident_time_range 两边都没覆盖

**问题**：BugReport schema 有 `incident_time_range` 字段，但 prompt 里没指导 LLM 生成它，系统注入也没覆盖它。fallback 路径里用了 `time_hint`，但正常路径没有。

### 2.4 Athena 和 OpenSearch 调用缺少 try-catch

**问题**：`log_analyzer_agent.py` 和 `rag_agent.py` 里的外部服务调用没有异常处理。Athena 超时、OpenSearch 索引不存在等情况会直接崩掉整个 graph。

**修复**：在 node 层面 catch，返回空结果让流程继续。比 `main.py` 的全局 catch 更好，因为 Reply Agent 可以基于有限信息（没有日志但有 RAG 文档，或两者都没有）生成降级回复。

### 2.5 DDL 缺了 4 张 Iceberg 表

**问题**：`athena/ddl/` 里只有 3 张表的建表 SQL，但代码里引用了 7 张表。缺了 `bronze_app_logs`、`silver_parsed_logs`、`gold_hourly_active_users`、`gold_incident_summary`。

**后果**：新环境部署后 Glue Job 启动就报表不存在。

### 2.6 requirements.txt 不存在

**问题**：`opensearch_indexer` Lambda 依赖 opensearchpy、pandas、pyarrow 等第三方库，但没有 `requirements.txt`。`dlq_replay` Lambda 的 Terraform 注释里写了 `pip install -r requirements.txt`，但实际不需要（只用了 boto3，Lambda 运行时自带）。

### 2.7 tickets 表没有写入代码

**问题**：Terraform 建了 `iodp-bug-tickets` 表和 GSI，但 `bug_report_agent.py` 没有往里写数据。Bug Report 只存在 `agent_jobs` 表的 `result_json` 里，1 小时 TTL 过期后丢失。

### 2.8 死代码

**问题**：`src/models/` 目录（request_models.py、output_models.py）和 `src/tools/schema_tool.py` 没有被任何代码 import。是早期设计的残留。

---

## 3. 成本考量

### 3.1 Athena 视图 vs Gold 预聚合表

```
v_user_session（视图）：每次查询扫描 Silver 全量数据
  1 天数据：~1GB 扫描 → $0.005/查询
  30 天趋势：~30GB 扫描 → $0.15/查询
  Dashboard 每 5 分钟刷新一次：~$130/月

gold.hourly_active_users（预聚合表）：每天 24 行
  30 天查询：~720 行，几 KB → $0.000005/查询
  Dashboard 每 5 分钟刷新一次：~$0.004/月
```

预聚合省了 3 万倍查询成本。Gold 层的存在意义就是把"扫几亿行"变成"读几百行"。

### 3.2 OpenSearch Serverless 是主要成本项

```
最低 2 OCU（搜索 1 + 索引 1）：
  $0.24/OCU/小时 × 2 × 24 × 30 = $345.60/月

对比其他服务（低流量）：
  API Gateway HTTP API:  ~$1/月
  Lambda:                ~$3/月
  DynamoDB:              ~$1/月
  S3 + CloudFront:       ~$2/月
  Athena:                ~$5/月
  
  OpenSearch 占总成本 97%
```

这就是 Makefile 里 `make destroy` 反复提醒"OpenSearch 每小时烧 $0.24"的原因。开发测试环境用完立刻销毁。

### 3.3 Lambda Container vs ZIP 部署

```
ZIP 部署：上限 250MB（解压后），装不下 LangChain + pandas + pyarrow
Container 部署：上限 10GB，随便装

Container 额外成本：
  ECR 存储：$0.10/GB/月，~500MB 镜像 → $0.05/月
  冷启动：比 ZIP 慢 2-5 秒（首次拉镜像）
```

选 Container 不是因为"好"，是因为 ZIP 装不下依赖。冷启动多 2-5 秒对异步 Job 模式不影响（用户不等着）。

---

## 4. 性能考量

### 4.1 Streaming Job 小文件问题

每 60 秒一个 micro-batch，每个分区产生一个 parquet 文件：

```
一天产生的文件数 = 24小时 × 60分钟 / 60秒 × 分区数
  event_date 分区：1 天 × 1440 batch = 1440 个文件/天
  如果还按 service_name 分区（10 个服务）：14400 个文件/天
```

解决方案：
- v2 改用 `event_date + log_level` 分区（log_level 只有 5 种值），减少分区数
- Iceberg 自带 compaction 可以合并小文件
- `write.metadata.previous-versions-max = 10` 限制元数据文件膨胀

### 4.2 Athena 查询性能

`v_error_log_enriched` 视图 JOIN 了 Gold + Silver 两张大表：

```sql
FROM gold.api_error_stats aes
JOIN silver.parsed_logs sl
  ON aes.service_name = sl.service_name
  AND aes.stat_hour = DATE_TRUNC('hour', sl.event_timestamp)
  AND aes.error_code = sl.error_code
WHERE sl.log_level IN ('ERROR', 'FATAL')
```

性能关键：
- `stat_hour` 范围过滤利用 Iceberg 分区裁剪（Gold 表按 `stat_date` 分区）
- `service_name` 利用 Iceberg metadata filtering
- 不加 WHERE 限制会导致全表 JOIN，可能几分钟才跑完

### 4.3 Agent 响应延迟

端到端延迟拆解：

```
POST /diagnose → 202：~200ms（写 DynamoDB）

后台执行：
  Router Agent LLM 调用：    ~2 秒
  Log Analyzer LLM + Athena：~5-15 秒（Athena 查询是瓶颈）
  RAG Agent LLM + OpenSearch：~3 秒
  Reply Agent LLM：          ~2 秒
  Bug Report Agent LLM：     ~3 秒（与 Reply 并行）
  ──────────────────────────
  总计：~15-25 秒

GET /diagnose/{job_id}：~50ms（读 DynamoDB）
```

Athena 查询是最大瓶颈。如果要优化，可以：
- 预计算错误日志到 DynamoDB（避免 Athena 冷启动）
- 或者用 Athena 的 CTAS（Create Table As Select）缓存常见查询

### 4.4 DynamoDB 热分区问题

`dq_reports` 表的 PK 是 `table_name`，只有 2-3 个值（bronze_app_logs、bronze_clickstream）。高流量时所有写入集中在同一个分区，可能触发 DynamoDB 限流。

当前不是问题（每 60 秒写一次），但如果 micro-batch 频率提高到每秒级别就需要重新设计 PK。

---

## 5. 安全考量

### 5.1 Athena SQL 注入防护

`athena_tool.py` 做了 SQL 安全校验：只允许 SELECT/WITH，禁止 DROP/DELETE/INSERT/CREATE。但 LLM 生成的 SQL 是动态的，理论上仍存在风险。

更安全的做法：Athena Workgroup 设置 `enforce_workgroup_configuration = true`，限制只能查特定数据库，即使 SQL 被注入也无法访问其他数据。

### 5.2 用户数据隔离

当前 Agent 只按 `user_id` 过滤日志，但没有验证"请求者就是该 user_id 的用户"。如果有人伪造 user_id，可以查到其他用户的错误日志。

生产环境应该：从 Cognito JWT token 里提取 user_id，不允许前端传入。

### 5.3 Bug Report 不泄露敏感信息

`BUG_REPORT_SYSTEM_PROMPT` 里写了"不要在 root_cause 中包含用户个人信息"，但这依赖 LLM 遵守。系统注入字段是安全的（代码控制），LLM 生成的 root_cause 和 recommended_fix 理论上可能泄露。

---

## 6. 扩展性问题

### 6.1 如果要支持第三个数据源（比如 metrics 指标数据）

需要改动：
- bigdata：新增 Streaming Job（MSK → Bronze）、Silver/Gold batch job、Athena DDL
- agent：Log Analyzer 的 prompt 里加新视图的字段说明，或者启用 `schema_tool.py` 动态查 Glue Catalog

### 6.2 如果数据量从 GB 到 TB 级

需要关注：
- Iceberg compaction 策略（定时合并小文件）
- Athena 查询分区裁剪必须生效（强制 WHERE 带分区键）
- Glue Job Worker 数量自动扩缩容（已配置 `enable-auto-scaling`）
- Silver 层 MERGE INTO 性能（TB 级数据的 merge 可能很慢，考虑改为 APPEND + 定期全量去重）

### 6.3 如果要支持多语言（英文客服 + 中文客服）

当前所有 prompt 是中文硬编码。需要：
- 抽取 prompt 到配置文件（按语言选择）
- Router Agent 先检测语言再路由到对应 prompt
- Bug Report 语言和用户回复语言可以不同（报告用英文给研发，回复用用户的语言）
