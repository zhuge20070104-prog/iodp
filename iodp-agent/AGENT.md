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
    ├──→ Bedrock Claude 3.5 Sonnet (LLM 推理)
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
│       └── s3_vectors_tool.py  # Bedrock Embedding + S3 Vectors query_vectors
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
| **Bedrock** | Claude 3.5 Sonnet LLM + Titan Embedding | 托管推理，无需部署模型 |
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

## 已删除的死代码

以下文件因无任何引用已移除：

| 文件 | 原因 |
|------|------|
| `src/models/request_models.py` | `DiagnoseRequest` 在 `main.py` 里重新定义了，此文件无人 import |
| `src/models/output_models.py` | `BugReportOutput`、`DiagnosisResult` 无人 import，功能已由 `state.py` 的 TypedDict 覆盖 |
| `src/tools/schema_tool.py` | 3 个函数（get_table_schema、list_available_tables、schema_summary_for_llm）无人调用，Log Analyzer 的表结构硬编码在 prompt 中 |
