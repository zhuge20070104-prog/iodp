# Gold 层 / Athena 层 / RAG 深度解析

---

## 第一部分：Gold 层 → Athena 层，数据怎么流转的？

### 先搞清楚两者的关系

很多人以为 Gold 和 Athena 是两个独立的系统，其实不是：

```
Gold 层  =  数据本体（Parquet 文件存在 S3 上）
Athena   =  查询引擎（负责读 Gold 层的文件，执行 SQL）

Gold 层是"图书馆的书"，Athena 是"图书馆的阅读室"。
书不会跑进阅读室，你去阅读室读书。
```

Glue Data Catalog 是"书目索引"，告诉 Athena：
- 这本书（表）在哪个书架（S3 路径）
- 这本书有哪些章节（分区键：stat_date、service_name）
- 这本书用什么语言写的（格式：Iceberg / Parquet）

---

### Gold 层三张表，数据转化全流程

#### 表一：`api_error_stats`（来自 system_app_logs）

**输入**：Silver `parsed_logs`，行级日志，每条是一个错误事件

```
Silver parsed_logs（14:00~15:00 这一小时的数据，约 52000 行）：

log_id           | service_name  | error_code | user_id     | req_duration_ms | event_timestamp
-----------------|---------------|------------|-------------|-----------------|---------------------
log-order-00001  | order-service | DB_TIMEOUT | user-88888  | 3021.5          | 2026-04-07 14:23:11
log-order-00002  | order-service | DB_TIMEOUT | user-11111  | 2988.0          | 2026-04-07 14:23:15
log-order-00003  | order-service | AUTH_FAIL  | user-22222  | 120.0           | 2026-04-07 14:24:01
...（共 52000 行）
```

**Glue Gold Job 做的事**（`gold_api_error_stats.py`）：

```
第一步：统计每个服务的总请求数
  order-service → 52000 条

第二步：按 (stat_hour, service_name, error_code) 分组聚合
  - error_count    = COUNT(*)
  - p99_duration   = PERCENTILE(req_duration_ms, 0.99)
  - unique_users   = COUNT(DISTINCT user_id)
  - sample_traces  = 随机取 5 个 trace_id

第三步：计算 error_rate = error_count / total_requests
```

**输出**：Gold `api_error_stats`，聚合后只剩几行

```
stat_hour            | service_name  | error_code | total_requests | error_count | error_rate | p99_duration_ms | unique_users | sample_trace_ids
---------------------|---------------|------------|----------------|-------------|------------|-----------------|--------------|------------------
2026-04-07 14:00:00  | order-service | DB_TIMEOUT | 52000          | 1820        | 0.035      | 3150.0          | 430          | ["trace-xyz-9988",...]
2026-04-07 14:00:00  | order-service | AUTH_FAIL  | 52000          | 310         | 0.006      | 125.0           | 290          | ["trace-auth-001",...]
2026-04-07 14:00:00  | pay-service   | TIMEOUT    | 18000          | 54          | 0.003      | 890.0           | 51           | ["trace-pay-001",...]
```

**52000 行 → 3 行**，这就是 Gold 的核心价值：**压缩 + 聚合**。

---

#### 表二：`hourly_active_users`（来自 user_clickstream）

**输入**：Silver `enriched_clicks`，每条是一个用户点击事件

```
Silver enriched_clicks（14:00~15:00，约 320000 行）：

event_id         | user_id    | session_id     | event_type  | event_timestamp
-----------------|------------|----------------|-------------|---------------------
evt-click-00001  | user-88888 | sess-ABC-2026  | purchase    | 2026-04-07 14:23:10
evt-click-00002  | user-11111 | sess-DEF-2026  | view        | 2026-04-07 14:23:12
evt-click-00003  | user-88888 | sess-ABC-2026  | checkout    | 2026-04-07 14:22:55
evt-click-00004  | user-33333 | sess-GHI-2026  | add_to_cart | 2026-04-07 14:24:01
...（共 320000 行）
```

**Glue Gold Job 做的事**（`gold_hourly_active_users.py`）：

```
按 stat_hour 分组，用 COUNT(DISTINCT user_id) 按 event_type 分别统计
```

**输出**：Gold `hourly_active_users`，每小时一行

```
stat_hour            | active_users | view_users | add_to_cart_users | checkout_users | purchase_users | new_users
---------------------|--------------|------------|-------------------|----------------|----------------|----------
2026-04-07 14:00:00  | 28400        | 28400      | 9800              | 5200           | 3380           | 1200
2026-04-07 13:00:00  | 26100        | 26100      | 9100              | 5100           | 3800           | 980
```

**320000 行 → 1 行**。

---

#### 表三：`incident_summary`（来自 Gold api_error_stats，专为 RAG 准备）

这张表比较特殊，**不是从 Silver 来的，是从 Gold api_error_stats 再加工的**，
目的是生成可读的自然语言摘要，供向量化后给 RAG 检索。

**输入**：Gold `api_error_stats` 中持续超过阈值的记录

**Glue Job 做的事**：识别一次"事故"（连续多个小时 error_rate > 5%），生成工单

**输出**：Gold `incident_summary`，格式是 JSON Lines

```json
{
  "incident_id":     "INC-2026-04-07-001",
  "title":           "order-service DB_TIMEOUT 大规模故障",
  "service_name":    "order-service",
  "error_codes":     ["DB_TIMEOUT"],
  "severity":        "HIGH",
  "start_time":      "2026-04-07 14:00:00",
  "end_time":        "2026-04-07 16:00:00",
  "peak_error_rate": 0.087,
  "affected_users":  2100,
  "symptoms":        "order-service 在 14:00-16:00 出现大规模 DB_TIMEOUT 错误，错误率峰值 8.7%，P99 响应时间从正常 900ms 恶化至 3200ms，共 2100 名用户购买失败。",
  "root_cause":      "RDS order-db 连接池耗尽，原因是凌晨上线的新版本未关闭旧连接，导致连接数在 2 小时内打满。",
  "resolution":      "回滚新版本，重启 RDS，增加连接池上限至 200。监控 DatabaseConnections < 150 为健康阈值。",
  "resolved_at":     "2026-04-07 16:23:00"
}
```

这条记录不是给人直接读的，而是准备被**向量化**，存进 OpenSearch，供 RAG 检索用。

---

### Athena 视图：v_error_log_enriched

Gold 层是聚合数据（每小时一行），Silver 层是明细数据（每个错误一行）。
Agent 有时需要两者合并——既要知道这小时 error_rate 多高，又要看具体是哪些用户报了什么错。

Athena 视图 `v_error_log_enriched` 就是把两者 JOIN 在一起：

```
Gold api_error_stats（统计）     JOIN     Silver parsed_logs（明细）
  stat_hour = 14:00                         event_timestamp ∈ [14:00, 15:00)
  service_name = order-service              service_name = order-service
  error_code = DB_TIMEOUT                   error_code = DB_TIMEOUT

结果：每行 = 一条具体的错误日志 + 当时整体的错误率指标
```

**视图输出示例（一行）**：

```
stat_hour        | service_name  | error_code | error_rate | p99_duration_ms | user_id    | req_path       | http_status | trace_id        | event_timestamp
-----------------|---------------|------------|------------|-----------------|------------|----------------|-------------|-----------------|--------------------
2026-04-07 14:00 | order-service | DB_TIMEOUT | 0.035      | 3150.0          | user-88888 | /api/v1/orders | 503         | trace-xyz-9988  | 2026-04-07 14:23:11
```

Agent 查这个视图：**一次查询，同时拿到宏观指标 + 微观明细**，不用查两次。

---

---

## 第二部分：RAG 数据源怎么进去的？Agent 怎么查？

### RAG 是什么，为什么需要它

**没有 RAG 时**：Agent 查完 Athena，知道 `DB_TIMEOUT error_rate = 3.5%`，
但不知道这个错误怎么修、以前有没有发生过、上次怎么解决的。LLM 自己也不知道，
因为这是公司内部的系统，LLM 训练数据里没有。

**有了 RAG**：Agent 去知识库里搜"DB_TIMEOUT order-service"，找到历史工单，
发现上次也是这个错误，上次的解决方案是"重启 RDS 连接池"——直接复用经验。

**RAG = 给 Agent 配了一个"公司内部故障手册"。**

---

### RAG 知识库里存了什么？

OpenSearch 里有两个索引：

| 索引 | 存的内容 | 数据来源 |
|---|---|---|
| `product_docs` | 产品技术文档、运维手册、API 说明 | 人工维护，离线导入 |
| `incident_solutions` | 历史故障工单 + 解决方案 | **项目一 Gold 层 `incident_summary` 自动生成** |

重点看 `incident_solutions`，这是项目一自动产生的：

---

### incident_summary → OpenSearch 的完整路径

```
Gold Job: gold_incident_summary.py
每天跑一次，从 api_error_stats 识别故障事件，生成自然语言摘要
        │
        ▼
s3://iodp-gold-prod/incident_summary/
  └── stat_date=2026-04-07/
        └── part-0000.parquet    ← 包含上面的 JSON 结构那条记录
        │
        │ S3 Event Notification（文件一写入立刻触发）
        ▼
Lambda: opensearch_indexer
  ① 读取 Parquet 文件
  ② 把 title + symptoms + root_cause + resolution 拼成一段文字：
     "order-service DB_TIMEOUT 大规模故障
      order-service 在 14:00-16:00 出现大规模 DB_TIMEOUT 错误...
      RDS order-db 连接池耗尽，原因是...
      回滚新版本，重启 RDS..."
  ③ 调用 AWS Bedrock Titan Embeddings，把这段文字转成 1024 维向量
  ④ 把文字 + 向量一起写入 OpenSearch incident_solutions 索引
        │
        ▼
OpenSearch incident_solutions 索引中新增一条：
  incident_id:  "INC-2026-04-07-001"
  content:      "order-service DB_TIMEOUT 大规模故障 ..."（原文）
  error_codes:  ["DB_TIMEOUT"]
  resolution:   "回滚新版本，重启 RDS..."
  embedding:    [0.023, -0.187, 0.341, ...]  ← 1024个浮点数，代表语义
```

**从 Gold 写入 S3 到 OpenSearch 可检索，延迟 < 5 分钟。**

---

### Agent 如何查 RAG？完整上下文转化流程

**场景**：用户投诉"我昨晚下单失败"，Agent 已经查完 Athena，拿到了错误信息。

#### Step 1：Log Analyzer Agent 查 Athena，拿到错误上下文

```python
# Log Analyzer 查 v_error_log_enriched 视图
# 返回的 error_logs 列表（传给下一个节点）：
error_logs = [
    {
        "error_code":    "DB_TIMEOUT",
        "service_name":  "order-service",
        "error_rate":    0.035,
        "error_message": "Database connection timeout after 3 retries",
        "p99_duration_ms": 3150.0,
    }
]
```

#### Step 2：RAG Agent 把错误上下文 → 自然语言检索 Query

LLM 收到这段上下文（Claude 3.5 Sonnet）：

```
用户描述：我昨晚下单失败
错误码：DB_TIMEOUT
受影响服务：order-service
错误信息摘要：Database connection timeout after 3 retries
错误率：3.5%
```

LLM 生成检索 Query（不是直接搜 "DB_TIMEOUT"，而是语义化的自然语言）：

```
"order-service 数据库连接超时故障，DB_TIMEOUT 错误率异常升高，
 响应时间 P99 超过 3 秒，用户购买请求失败，疑似连接池耗尽或 RDS 异常"
```

#### Step 3：把 Query 向量化，在 OpenSearch 里做相似度搜索

```
Query 文字 → Bedrock Titan Embeddings → [0.031, -0.192, 0.318, ...]（向量）
                                                │
                              OpenSearch kNN 搜索：
                              找最近的 5 个向量（余弦相似度最高的文档）
                                                │
                              命中：INC-2026-04-07-001
                              相似度分：0.94（非常高）
```

#### Step 4：返回召回结果

OpenSearch 返回最相关的历史工单：

```
标题：order-service DB_TIMEOUT 大规模故障
相似度：0.94
error_codes：["DB_TIMEOUT"]
根因：RDS order-db 连接池耗尽，原因是新版本未关闭旧连接
解决方案：回滚新版本，重启 RDS，增加连接池上限至 200
```

#### Step 5：Synthesizer Agent 把所有信息合成最终回复

输入给 Synthesizer 的上下文：

```
【Log Analyzer 找到的问题】
  - order-service DB_TIMEOUT，error_rate 3.5%，430 人受影响

【RAG 找到的历史经验】
  - 上次同样问题：RDS 连接池耗尽
  - 上次解决方案：回滚 + 重启 RDS + 扩连接池

【用户原始投诉】
  - "我昨晚下单失败"
```

Synthesizer 输出两个制品：

**① 给用户的回复（自然语言）**：
```
您好，我们已确认您昨晚 14:00-16:00 下单失败是由于我们的订单服务
出现数据库连接异常（DB_TIMEOUT），已影响约 430 位用户。
故障已于 16:23 修复。如需重新下单，现在可以正常使用。
对您造成的不便深感抱歉。
```

**② 给研发团队的 Bug 报告（结构化 JSON）**：
```json
{
  "ticket_id":     "BUG-2026-040701",
  "severity":      "HIGH",
  "service":       "order-service",
  "error_code":    "DB_TIMEOUT",
  "duration":      "14:00 - 16:23",
  "affected_users": 430,
  "root_cause":    "RDS 连接池耗尽（参考历史工单 INC-2026-04-07-001）",
  "suggested_fix": "检查新版本连接池关闭逻辑，复现后参照上次解决方案",
  "sample_traces": ["trace-xyz-9988", "trace-abc-1234"]
}
```

---

### 完整数据流总结图

```
Kafka: user_clickstream          Kafka: system_app_logs
       │                                │
       │ Glue Streaming                 │ Glue Streaming
       ▼                                ▼
Bronze: clickstream             Bronze: app_logs
       │                                │
       │ Glue Batch（每小时）            │ Glue Batch（每小时）
       ▼                                ▼
Silver: enriched_clicks         Silver: parsed_logs
       │                                │
       │ Glue Batch（每小时）            │ Glue Batch（每小时）
       ▼                                ▼
Gold: hourly_active_users       Gold: api_error_stats
（漏斗数据：view/cart/            （错误统计：error_rate/
  checkout/purchase 用户数）        p99/unique_users/traces）
       │                                │
       │                                │ Glue Batch（每天）
       │                                ▼
       │                        Gold: incident_summary
       │                        （自然语言故障摘要 JSON）
       │                                │
       │                                │ S3 Event → Lambda
       │                                ▼
       │                        OpenSearch: incident_solutions
       │                        （向量化，1024维，供 RAG 检索）
       │                                │
       ▼                                ▼
   Athena SQL查询              ┌─────────────────────────┐
（BI Dashboard）               │  Agent 查询链路          │
                               │  1. Log Analyzer        │
                               │     → Athena 拿错误数据  │
                               │  2. RAG Agent           │
                               │     → OpenSearch 拿历史  │
                               │  3. Synthesizer         │
                               │     → 输出报告 + 工单    │
                               └─────────────────────────┘
```

---

## 关键区别总结

| 对比项 | Gold 层 | Athena | OpenSearch (RAG) |
|---|---|---|---|
| **本质** | 数据文件（S3 Parquet） | SQL 查询引擎 | 向量搜索引擎 |
| **存的是什么** | 聚合后的结构化数字 | 不存数据，只查询 | 非结构化文本 + 向量 |
| **查询方式** | SQL（精确匹配） | SQL | 语义相似度搜索 |
| **谁在用** | Athena、BI、Agent | BI Dashboard、Agent | RAG Agent |
| **回答什么问题** | "这小时 error_rate 是多少？" | "给我过滤出 error_rate > 3% 的" | "以前遇到这个问题怎么解决的？" |

---

## 第三部分：从"我下单失败"到查 Athena，中间发生了什么？

**核心答案：没有映射表。靠两个机制：LLM 提取槽位 + 代码函数转换时间。**

---

### 槽位（Slot）是什么

Agent 要去 Athena 查数据，必须知道两个参数：
- **谁**：`user_id`（查哪个用户的日志）
- **什么时候**：`time_start` / `time_end`（查哪个时间段）

从用户一句话里把这两个参数提取出来，就叫**槽位填充**。

---

### Step 1：Router Agent —— LLM 提取槽位，不足则追问

用户输入：`"我昨晚下单失败"`

LLM 读这句话，判断意图 + 提取实体，输出 JSON：

```json
{
  "intent":             "tech_issue",
  "user_id":            null,
  "incident_time_hint": "昨晚",
  "missing_info":       ["user_id"],
  "clarification_question": "非常抱歉！为了帮您查询，请问您的账户ID是多少？"
}
```

`user_id = null` → 系统把追问返回给用户，**流程暂停**：

```
Agent → 用户："请问您的账户ID是多少？"
用户 → Agent："user-88888"
```

第二轮 Router 运行，槽位填满，流程继续：

```json
{
  "intent":             "tech_issue",
  "user_id":            "user-88888",
  "incident_time_hint": "昨晚",
  "missing_info":       []
}
```

> 最多追问 3 次，3 次还填不满 → 转人工客服。

---

### Step 2：Log Analyzer Agent —— 代码转时间，LLM 生 SQL

**"昨晚" → 具体时间戳**：这步是代码函数做的，不是 LLM：

```python
def _parse_time_hint("昨晚"):
    base_date = 今天 - 1天           # "昨" → 昨天
    hour = 当前小时                  # 没说几点 → 用当前小时兜底
    # 如果是 "昨晚11点" → 提取 11，加 12 = 23时
    time_start = 昨天 (hour-1):00
    time_end   = 昨天 (hour+2):59
# 结果："2026-04-06 22:00:00" ~ "2026-04-07 01:00:00"
```

时间确定后，LLM 用 `user_id + 时间范围` 生成 SQL：

```sql
SELECT service_name, error_code, error_rate, error_message,
       p99_duration_ms, trace_id, event_timestamp
FROM iodp_gold_prod.v_error_log_enriched
WHERE user_id = 'user-88888'
  AND event_timestamp BETWEEN '2026-04-06 22:00:00' AND '2026-04-07 01:00:00'
  AND log_level IN ('ERROR', 'FATAL')
ORDER BY error_rate DESC
LIMIT 20
```

**注意：SQL 里没有 `service_name = 'order-service'`。**

LLM 不需要知道"下单 = order-service"，它只用 `user_id` 查。
Athena 返回结果里，`service_name` 字段自然就是 `order-service`。
**是数据自己告诉了 Agent 是哪个服务出问题，不是规则映射的。**

---

### 完整槽位流转图

```
用户："我昨晚下单失败"
        │
        ▼ Router Agent（LLM）
  intent = tech_issue
  time_hint = "昨晚"
  user_id = null  ← 缺失，触发追问
        │
        ▼ 返回给用户
  "请问您的账户ID？"
        │
        ▼ 用户："user-88888"
        │
        ▼ Router Agent 第二轮（LLM）
  user_id = "user-88888"  槽位齐全
        │
        ▼ Log Analyzer Agent
  _parse_time_hint("昨晚")
  → 代码函数 → "22:00 ~ 01:00"（不是 LLM 做的）
        │
        ▼ LLM 生成 SQL
  WHERE user_id='user-88888' AND 时间范围
  （不需要写死 service_name）
        │
        ▼ Athena 执行
  返回：service_name=order-service, error_code=DB_TIMEOUT...
        │
        ▼ RAG + Synthesizer → 最终报告
```
