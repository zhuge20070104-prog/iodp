# 数据流深度解析

---

## 问题 1：sample_trace_ids 是用来追踪单个用户的单个 Session 的吗？

**简短回答：不完全是。**

`trace_id` 追踪的是**一次完整的服务调用链**（一个请求从前端到后端经过了哪些服务），  
`session_id` 才是追踪用户的一个操作会话。  
`sample_trace_ids` 是 Gold 层聚合时"随手留下的几个案例"，供工程师排查问题时有线索可查。

---

### 完整数据流举例：一个用户下单失败

**背景**：用户 `user-88888` 在 14:23 点击了"立即购买"，但订单服务超时失败了。

---

#### 第一步：前端埋点 → Kafka `user_clickstream`

用户点击按钮，前端发出一条点击事件：

```json
{
  "event_id":        "evt-click-00001",
  "user_id":         "user-88888",
  "session_id":      "sess-ABC-2026",
  "event_type":      "purchase",
  "event_timestamp": "2026-04-07T14:23:10Z",
  "page_url":        "https://shop.iodp.com/checkout",
  "referrer_url":    "https://shop.iodp.com/cart",
  "device_info": {
    "device_type": "mobile",
    "os":          "iOS 17",
    "browser":     "Safari"
  },
  "geo_info": {
    "country_code": "CN",
    "city":         "Shanghai",
    "ip_hash":      "hash-xyz-001"
  },
  "properties": {
    "product_id": "prod-999",
    "amount":     299.0
  }
}
```

---

#### 第二步：后端报错 → Kafka `system_app_logs`

同一时刻，order-service 处理这个请求时抛出超时异常，产生一条日志：

```json
{
  "log_id":          "log-order-00001",
  "trace_id":        "trace-xyz-9988",
  "span_id":         "span-order-001",
  "service_name":    "order-service",
  "instance_id":     "i-0abc123",
  "log_level":       "ERROR",
  "event_timestamp": "2026-04-07T14:23:11Z",
  "message":         "Database connection timeout after 3 retries",
  "error_details": {
    "error_code":  "DB_TIMEOUT",
    "error_type":  "TimeoutException",
    "http_status": 503,
    "stack_trace": "at com.iodp.order.db.connect(DB.java:42)..."
  },
  "request_info": {
    "method":      "POST",
    "path":        "/api/v1/orders",
    "user_id":     "user-88888",
    "duration_ms": 3021.5
  },
  "environment": "prod"
}
```

> 注意：`trace_id = "trace-xyz-9988"` 是这次请求的唯一调用链 ID，前后端共享。  
> `session_id` 只在点击流里有，日志里没有（日志是后端视角）。

---

#### 第三步：Bronze 层（原始落盘）

两条数据分别写入 Bronze，结构不变，只是展平嵌套字段：

**Bronze app_logs 的一行：**

| log_id | trace_id | service_name | log_level | event_timestamp | error_code | req_duration_ms | user_id |
|---|---|---|---|---|---|---|---|
| log-order-00001 | trace-xyz-9988 | order-service | ERROR | 2026-04-07 14:23:11 | DB_TIMEOUT | 3021.5 | user-88888 |

**Bronze clickstream 的一行：**

| event_id | user_id | session_id | event_type | event_timestamp | page_url | product_id |
|---|---|---|---|---|---|---|
| evt-click-00001 | user-88888 | sess-ABC-2026 | purchase | 2026-04-07 14:23:10 | /checkout | prod-999 |

---

#### 第四步：Silver 层（去重 + 标准化）

数据清洗后结构不变，只是保证了：
- `log_id` 唯一（去重）
- `event_timestamp` 类型是 TIMESTAMP 而不是字符串
- `req_duration_ms` 类型是 DOUBLE

---

#### 第五步：Gold 层（按小时聚合）

14:00~15:00 这一小时内，`order-service` 的 `DB_TIMEOUT` 一共发生了 1820 次，  
Gold Job 聚合后产生**一行**：

| stat_hour | service_name | error_code | total_requests | error_count | error_rate | p99_duration_ms | unique_users | sample_trace_ids |
|---|---|---|---|---|---|---|---|---|
| 2026-04-07 14:00:00 | order-service | DB_TIMEOUT | 52000 | 1820 | 0.035 | 3150.0 | 430 | ["trace-xyz-9988", "trace-abc-1234", "trace-def-5678", ...] |

---

#### `sample_trace_ids` 的作用

`trace-xyz-9988` 就是 `user-88888` 那次失败请求的 trace_id，被保留在 Gold 层。

当 Agent 发现 `error_rate = 3.5%` 触发告警时，它拿着这个 trace_id 去其他系统（如 Jaeger/X-Ray）查完整调用链：

```
trace-xyz-9988 的调用链：
  前端 → API Gateway (12ms)
       → order-service (3021ms ← 超时在这里)
            → payment-service (未到达)
            → inventory-service (未到达)
```

工程师一眼就知道问题在 `order-service` 连接数据库这一步，不需要在 1820 条日志里手动翻找。

**`sample_trace_ids` = "出事了，给你几个案例编号，去现场查"。**

---

---

## 问题 2：为什么按 error_rate 聚合，而不是按 error_code 聚合？

**短答：两者都用了，不是二选一。** `error_code` 是分组维度，`error_rate` 是聚合指标。  
真正的问题是：**为什么要算 error_rate，而不是直接数 error_count？**

---

### 用例子解释：error_count 的误导性

**场景：某天两个服务的错误数对比**

| 服务 | 总请求数 | error_count (DB_TIMEOUT) |
|---|---|---|
| order-service | 52,000 | 1,820 |
| user-service  | 200    | 18   |

如果只看 `error_count`：
- order-service 出错 1820 次，**看起来很严重**
- user-service 出错 18 次，**看起来没问题**

如果看 `error_rate`：
- order-service：1820 / 52000 = **3.5%**（在告警阈值 5% 以内，可接受）
- user-service：18 / 200 = **9%（已超告警阈值！需要立刻处理！）**

**结论：error_count 大不代表有问题，error_rate 高才是真正的异常信号。**

---

### Gold 表的聚合维度设计

```sql
GROUP BY
    stat_hour,        -- 按小时聚合（时间维度）
    service_name,     -- 按服务聚合（定位是哪个服务出问题）
    error_code        -- 按错误类型聚合（定位是什么问题）

计算：
    error_rate = error_count / total_requests   -- 真正的健康指标
    p99_duration_ms                             -- 性能指标
    unique_users                                -- 业务影响范围
```

`error_code` 是**分组依据**（"把同类错误归在一起数"），  
`error_rate` 是**度量结果**（"这类错误占总量的比例"）。

---

### 为什么不把 error_code 当聚合指标？

`error_code` 是字符串（`"DB_TIMEOUT"`、`"AUTH_FAIL"`），  
字符串没法"聚合"——你无法对字符串做 SUM、AVG、P99。  
只能对字符串做 GROUP BY（分类）、COUNT（计数）。

正确的思路：

```
按 (service_name, error_code) 分组
→ 数出每组有多少条（error_count）
→ 除以总请求数
→ 得到 error_rate（这才是可以比较、可以告警的数字）
```

---

---

## 问题 3：Agent 和 BI 工具如何使用数据？完整流程 + 最终制品

---

### 例子 A：Agent（AI 智能告警分析）

**触发场景**：14:00~15:00，`order-service` 的 DB_TIMEOUT 错误率达到 3.5%，Agent 被触发。

---

#### 数据从 Kafka 到 Agent 的完整路径

```
用户下单失败（user-88888, 2026-04-07 14:23:10）
        │
        ├── 前端埋点 ──────────────────────────────────────► Kafka: user_clickstream
        │   event_type=purchase, session_id=sess-ABC-2026       │
        │                                                        │
        └── 后端报错 ──────────────────────────────────────► Kafka: system_app_logs
            trace_id=trace-xyz-9988, error_code=DB_TIMEOUT      │
                                                                 │
                              ┌──────────────────────────────────┘
                              │ Glue Streaming（每60秒一个微批）
                              ▼
                      Bronze S3（扁平化原始数据）
                              │
                              │ Glue Batch（每小时）
                              ▼
                      Silver S3（去重 + 标准化）
                              │
                              │ Glue Batch（每小时整点）
                              ▼
                      Gold S3: api_error_stats
                      stat_hour=14:00, service=order-service,
                      error_code=DB_TIMEOUT, error_rate=0.035
                              │
                              │ CloudWatch Alarm（每5分钟扫描一次）
                              ▼
                      告警触发：error_rate > 0.03
                              │
                              │ SNS → Lambda → 触发 Agent
                              ▼
                      Agent: Log Analyzer 工具链启动
```

---

#### Agent 内部工作流程

**Step 1**：Agent 调用 Athena 工具，查 Gold 层确认异常

```sql
-- Agent 自动生成并执行
SELECT service_name, error_code, error_rate, p99_duration_ms,
       unique_users, sample_trace_ids
FROM iodp_gold_prod.api_error_stats
WHERE stat_date = '2026-04-07'
  AND stat_hour BETWEEN '2026-04-07 13:00:00' AND '2026-04-07 14:00:00'
  AND error_rate > 0.03
ORDER BY error_rate DESC
LIMIT 10
```

Athena 返回：

| stat_hour | service_name | error_code | error_rate | p99_duration_ms | unique_users | sample_trace_ids |
|---|---|---|---|---|---|---|
| 14:00 | order-service | DB_TIMEOUT | 0.035 | 3150.0 | 430 | ["trace-xyz-9988", ...] |
| 13:00 | order-service | DB_TIMEOUT | 0.008 | 890.0  | 95  | ["trace-old-001", ...]  |

Agent 发现 13:00 只有 0.8%，14:00 突然升到 3.5%，**是突发性恶化**。

---

**Step 2**：Agent 查 DynamoDB，确认 DQ 报告没问题（不是数据质量问题导致的假告警）

```python
dq_reports.query(
    KeyConditionExpression="table_name = :t AND report_timestamp BETWEEN :s AND :e",
    ExpressionAttributeValues={
        ":t": "bronze_app_logs",
        ":s": "2026-04-07T14:00:00Z",
        ":e": "2026-04-07T15:00:00Z"
    }
)
```

返回：`failure_rate = 0.0087`，`threshold_breached = false`  
→ 数据质量正常，不是假告警。

---

**Step 3**：Agent 关联 user_clickstream，评估业务影响

```sql
-- 查 Silver 层 clickstream，看这 430 个受影响用户做了什么操作
SELECT event_type, COUNT(*) as cnt, COUNT(DISTINCT user_id) as users
FROM iodp_silver_prod.enriched_clicks
WHERE event_date = '2026-04-07'
  AND event_timestamp BETWEEN '2026-04-07 14:00:00' AND '2026-04-07 15:00:00'
  AND event_type IN ('purchase', 'checkout', 'add_to_cart')
GROUP BY event_type
ORDER BY cnt DESC
```

返回：

| event_type | cnt | users |
|---|---|---|
| purchase  | 1820 | 430 |
| checkout  | 5200 | 1100 |
| add_to_cart | 9800 | 2300 |

Agent 推断：1820 次购买尝试失败，430 个用户受影响，checkout 漏斗转化率异常下降。

---

**Step 4**：Agent 输出最终分析报告（最终制品）

```
=== 自动告警分析报告 ===
时间：2026-04-07 15:02:00
触发条件：order-service DB_TIMEOUT error_rate = 3.5% > 阈值 3%

【问题摘要】
order-service 在 14:00-15:00 出现数据库连接超时，
错误率从正常值 0.8% 急升至 3.5%，P99 响应时间从 890ms 恶化至 3150ms。

【业务影响】
- 受影响用户：430 人
- 失败购买请求：1,820 次
- 预估GMV损失：约 ¥543,180（按均价 ¥299 估算）
- checkout→purchase 转化率：从正常 45% 下降至 35%

【排查线索】
异常 trace 样本（可在 Jaeger 查完整调用链）：
  - trace-xyz-9988
  - trace-abc-1234
  - trace-def-5678

【初步判断】
14:00 左右开始，DB 连接池耗尽或 RDS 实例异常。
建议检查：RDS CloudWatch → DatabaseConnections 指标

【建议动作】
1. 立即：检查 RDS order-db 连接数是否打满
2. 短期：增加连接池大小或扩容 RDS
3. 长期：增加 DB_TIMEOUT 的重试与熔断机制
```

---

### 例子 B：BI 工具（数据分析师看报表）

**场景**：产品经理每天早上看昨天的用户行为漏斗报表。

---

#### 数据从 Kafka 到 BI 的完整路径

```
全天用户行为事件
    ├── user_clickstream (点击、浏览、加购、下单...)
    └── system_app_logs  (API 错误、响应时间...)
             │
             │ 同上，经过 Bronze → Silver
             │
             ▼
    Gold: hourly_active_users（每小时活跃用户数）
    Gold: api_error_stats    （每小时错误率）
             │
             │ Athena SQL（BI 工具连接 Athena，如 QuickSight / Superset）
             ▼
    BI Dashboard
```

---

#### BI 工具执行的 SQL（背后自动生成）

**漏斗分析：**

```sql
-- 每小时各阶段用户数（漏斗）
SELECT
    DATE_TRUNC('hour', event_timestamp) AS hour,
    COUNT(DISTINCT CASE WHEN event_type = 'view'         THEN user_id END) AS view_users,
    COUNT(DISTINCT CASE WHEN event_type = 'add_to_cart'  THEN user_id END) AS cart_users,
    COUNT(DISTINCT CASE WHEN event_type = 'checkout'     THEN user_id END) AS checkout_users,
    COUNT(DISTINCT CASE WHEN event_type = 'purchase'     THEN user_id END) AS purchase_users
FROM iodp_silver_prod.enriched_clicks
WHERE event_date = '2026-04-07'
GROUP BY 1
ORDER BY 1
```

**叠加错误率（找到转化率下降的原因）：**

```sql
-- 把业务漏斗和技术错误率叠加在一张图上
SELECT
    f.hour,
    f.purchase_users,
    f.checkout_users,
    ROUND(f.purchase_users * 1.0 / NULLIF(f.checkout_users, 0), 3) AS conversion_rate,
    e.error_rate AS db_timeout_rate
FROM funnel_hourly f
LEFT JOIN iodp_gold_prod.api_error_stats e
    ON f.hour = e.stat_hour
    AND e.service_name = 'order-service'
    AND e.error_code = 'DB_TIMEOUT'
ORDER BY f.hour
```

---

#### 最终制品：BI Dashboard（展现形式）

```
┌─────────────────────────────────────────────────────────────────────┐
│           IODP 运营日报  2026-04-07                                  │
├──────────────────────────┬──────────────────────────────────────────┤
│  购买漏斗转化率（折线图）  │  API 错误率趋势（折线图）                │
│                          │                                          │
│  100% ─ 浏览用户          │  5% ┤                                    │
│   45% ─ 加购用户          │     │         ●  ← 14:00 DB_TIMEOUT 3.5%│
│   28% ─ 结算用户          │  3% ┤ ─ ─ ─ ─ ─  ← 告警阈值             │
│   14% ─ 下单成功          │     │                                    │
│    ↑14:00开始下降          │  1% ┤ ●●●●●●●●  ← 正常水位              │
│                          │     └─────────────────────────────────  │
│                          │       10  11  12  13  14  15  16 (时)   │
├──────────────────────────┴──────────────────────────────────────────┤
│  关键指标卡片                                                         │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌──────────────┐  │
│  │ 日活用户    │  │ 总下单量   │  │ 购买转化率  │  │ 故障时段损失 │  │
│  │  128,400   │  │  18,200   │  │   14.2%    │  │  ¥543,180   │  │
│  │ ▲3% vs昨日 │  │ ▼8% vs昨日│  │ ▼2pp vs昨日│  │ 14:00-15:00 │  │
│  └────────────┘  └────────────┘  └────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

**产品经理一眼看出**：14:00 转化率下跌，和 DB_TIMEOUT 错误率上升时间完全吻合。  
这就是为什么 **clickstream（用户行为）和 app_logs（系统错误）** 要放在同一个数据平台里——  
技术问题和业务影响，**在同一张图里就能关联起来**。

---

## 总结：三个问题的核心答案

| 问题 | 核心答案 |
|---|---|
| sample_trace_ids 是追踪 session 的吗？ | 不是。trace_id 追踪一次请求的调用链，是排查具体故障的"线索编号"。session_id 才是用户的操作会话。两者在 Silver 层可以通过 user_id 关联。 |
| 为什么用 error_rate 而不是 error_code 聚合？ | error_code 是分组维度（字符串，不能计算），error_rate 是业务健康指标（数字，可以告警）。小服务 18 次错误可能比大服务 1820 次错误更危险，只有比例才能反映真实风险。 |
| Agent 和 BI 怎么用数据？ | Agent 自动查询 Gold + Silver，交叉印证，输出结构化告警报告 + 排查建议。BI 工具通过 Athena SQL 把技术指标和业务漏斗叠加在同一张图，让非技术人员也能看懂故障对业务的影响。 |
