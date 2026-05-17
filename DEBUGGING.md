# DEBUGGING.md

IODP 项目从首次 `make init` 到端到端跑通的全部 debug 记录（2026-05-16 ~ 2026-05-17）。
按子项目和阶段分组。每条包含：症状 → 根因 → 修复 → 文件位置。

---

## Phase 1 — iodp-bigdata 部署期

### 1.1 HCL 单行 transition 语法

- **症状**：`terraform apply` 报 parse error
- **根因**：把 `transition { days = 60; storage_class = "..." }` 写成分号分隔的单行，HCL 不支持
- **修复**：拆成多行
- **文件**：[iodp-bigdata/terraform/modules/storage/main.tf](iodp-bigdata/terraform/modules/storage/main.tf)

### 1.2 Terraform Backend 切换报错

- **症状**：`Backend configuration changed` 拒绝 init
- **根因**：之前用过 local backend，后改 S3 backend
- **修复**：`terraform init -reconfigure`（不要用 `-migrate-state`，会把空 state 覆盖到 S3）
- **文件**：[iodp-bigdata/Makefile](iodp-bigdata/Makefile)

### 1.3 ECR RepositoryAlreadyExistsException

- **症状**：第二次 `make init` 报 ECR repo 已存在
- **根因**：Terraform 不知道这个 repo 是它管的（之前手工 create 过或 state 丢了）
- **修复**：Makefile 里加 detect-and-import 逻辑：`aws ecr describe-repositories` 检测存在 → `terraform import` 接管
- **文件**：[iodp-agent/Makefile](iodp-agent/Makefile)

### 1.4 Glue Catalog Database 缺 tags

- **症状**：`aws_glue_catalog_database` apply 提示 provider 报错或漂移
- **根因**：Provider bug，不会自动从 `default_tags` 继承到 Glue DB
- **修复**：三个 db 资源显式加 `tags = var.tags`
- **文件**：[iodp-bigdata/terraform/modules/compute/main.tf](iodp-bigdata/terraform/modules/compute/main.tf)

### 1.5 Vector Bucket count = 0

- **症状**：`vector_indexer` module 初次 apply 找不到 vector bucket arn
- **根因**：S3 Vectors 是新服务，初次部署时 vector bucket 还没创出来
- **修复**：`vector_indexer` 加 `count = var.vector_bucket_arn != "" ? 1 : 0`，允许两阶段部署
- **文件**：[iodp-bigdata/terraform/main.tf](iodp-bigdata/terraform/main.tf)

### 1.6 DLQ Replay fileexists 报错

- **症状**：`fileexists("dlq_replay.zip")` 在 plan 阶段就报错（文件还没生成）
- **根因**：用 `fileexists` 兜底打包逻辑，CI 里没这个文件
- **修复**：换成 `data "archive_file"` 让 Terraform 自己打包；同时去掉 `reserved_concurrent_executions`（账号没配额）
- **文件**：[iodp-bigdata/terraform/modules/dlq_replay/main.tf](iodp-bigdata/terraform/modules/dlq_replay/main.tf)

### 1.7 Dashboard Widget Region 缺失

- **症状**：CloudWatch dashboard JSON apply 后图表显示 "No data"
- **根因**：每个 widget 没指定 region，默认查 us-east-1（实际数据在 ap-southeast-1）
- **修复**：observability module 加 `aws_region` 变量，所有 widget 注入 `region = var.aws_region`
- **文件**：[iodp-bigdata/terraform/modules/observability/main.tf](iodp-bigdata/terraform/modules/observability/main.tf)

### 1.8 Iceberg TBLPROPERTIES 不支持

- **症状**：`apply_ddl.sh` 跑到 CREATE TABLE 报 `Property xxx not supported`
- **根因**：Athena Iceberg 的 TBLPROPERTIES 跟 Spark Iceberg 不一样，不认 `write.parquet.compression-codec` / `write.delete.mode` / `write.update.mode` / `format-version`
- **修复**：5 个 DDL 都改成 `write_compression = 'snappy'`，其他属性删掉
- **文件**：[iodp-bigdata/athena/ddl/*.sql](iodp-bigdata/athena/ddl/)

### 1.9 View 引用了写死的 prod database

- **症状**：dev 环境查 `v_error_log_enriched` 报 `Database iodp_gold_prod does not exist`
- **根因**：view 定义里 hardcode 了 `iodp_gold_prod`
- **修复**：改成 `iodp_gold_${ENVIRONMENT}`，apply_ddl.sh 里 envsubst 替换
- **文件**：[iodp-bigdata/athena/views/v_error_log_enriched.sql](iodp-bigdata/athena/views/v_error_log_enriched.sql)

### 1.10 apply_ddl.sh 忽略 views/ 子目录

- **症状**：DDL 全部 apply 完了，但 view 没建出来，agent 查 `v_error_log_enriched` 报 view not found
- **根因**：脚本只 loop `$DDL_DIR/*.sql`，没扫 `$VIEWS_DIR`
- **修复**：`for ddl_file in "$DDL_DIR"/*.sql "$VIEWS_DIR"/*.sql; do`
- **文件**：[iodp-bigdata/scripts/apply_ddl.sh](iodp-bigdata/scripts/apply_ddl.sh)

---

## Phase 2 — iodp-agent 部署期

### 2.1 Agent backend 切换到 local

- **症状**：装两套 S3 backend 互相冲突
- **根因**：agent 的 state 跟 bigdata 共用一个 S3 backend 容易踩坑
- **修复**：agent 改成 `backend "local" {}`，bigdata 继续用 S3 backend
- **文件**：[iodp-agent/terraform/backend.tf](iodp-agent/terraform/backend.tf)

### 2.2 init 步骤里删了 terraform.tfstate

- **症状**：第二次 `make init`，之前 import 的 ECR repo 又"丢失"，重新 import 又失败
- **根因**：原 Makefile 里 `rm -f terraform.tfstate`，但 ECR import 已经在这里面了
- **修复**：删掉 `rm -f terraform.tfstate`，保留跨次 import 的状态
- **文件**：[iodp-agent/Makefile](iodp-agent/Makefile)

### 2.3 IAM 权限缺 Bedrock inference profile

- **症状**：Lambda 调 Bedrock 报 `AccessDeniedException` on `arn:aws:bedrock:...:inference-profile/...`
- **根因**：Bedrock cross-region inference 是单独的 ARN namespace
- **修复**：IAM policy 加 `arn:aws:bedrock:*:*:inference-profile/*`
- **文件**：[iodp-agent/terraform/main.tf](iodp-agent/terraform/main.tf)（后期废弃，迁 Qwen 后无关）

### 2.4 IAM 权限缺 cross-account bucket wildcard

- **症状**：Athena 查 `v_error_log_enriched`（JOIN Silver + Gold）时 access denied
- **根因**：IAM 只授 gold bucket，没授 silver；且 bucket name 后缀含 account_id 不确定
- **修复**：用 wildcard `arn:aws:s3:::iodp-{silver,gold}-*-*/*`；加 Glue partition 读权限
- **文件**：[iodp-agent/terraform/main.tf](iodp-agent/terraform/main.tf)

### 2.5 Lambda 启动 ImportError: langgraph.checkpoint.dynamodb

- **症状**：`Unable to import module 'lambda_handler': No module named 'langgraph.checkpoint.dynamodb'`
- **根因**：包名搞错，实际导入路径是 `langgraph_checkpoint_dynamodb.saver`
- **修复**：先在 requirements 加 `langgraph-checkpoint-dynamodb>=0.0.4`；最终因为它要求两张表，简化为 `MemorySaver()` 模块单例
- **文件**：[iodp-agent/src/graph/checkpointer.py](iodp-agent/src/graph/checkpointer.py)

### 2.6 MemorySaver 不是单例 → 多轮对话失效

- **症状**：用户给 user_id 后，下一 turn router 又问"请提供 user_id"
- **根因**：每次 `build_graph()` 时创建新的 MemorySaver 实例，state 没持久化
- **修复**：模块级 `_CHECKPOINTER = MemorySaver()` 单例
- **文件**：[iodp-agent/src/graph/checkpointer.py](iodp-agent/src/graph/checkpointer.py)

---

## Phase 3 — LLM / Embedding 迁移（Bedrock → Qwen）

### 3.1 Anthropic 模型不可用

- **症状**：`Access to Anthropic models is not allowed from unsupported countries`
- **根因**：AWS 账号注册地是中国，Anthropic 模型按 region+billing-country 双限制
- **修复**：换 provider。Bedrock 申请 allowlisting 路径对个人账号封死（要"企业法人身份验证"），改用第三方 OpenAI 兼容 API（DeepSeek / 通义千问 / 智谱）
- **决策**：选通义千问 Qwen，因为一个 dashscope key 同时支持 chat + embedding，运维简单
- **文件**：[iodp-agent/src/config.py](iodp-agent/src/config.py) 注释里留了切 provider 的 cheat sheet

### 3.2 ChatBedrock → ChatBedrockConverse → ChatOpenAI

- **症状**：切 Qwen 之前先试过 `ChatBedrock` 和 `ChatBedrockConverse`，前者不支持 tool calling，后者还要 Bedrock 权限
- **修复**：统一 `ChatOpenAI(base_url=settings.llm_base_url)`
- **文件**：[iodp-agent/src/graph/nodes/_llm_helpers.py](iodp-agent/src/graph/nodes/_llm_helpers.py)

### 3.3 Embedding 也得跟着换

- **症状**：之前 RAG embedding 用 Bedrock Cohere，换 Qwen 后这条路断了
- **修复**：embedding 也走 OpenAI 兼容 client，用 qwen `text-embedding-v3`（1024 维）。S3 Vectors index dimension 必须跟这个一致
- **文件**：[iodp-agent/src/tools/s3_vectors_tool.py](iodp-agent/src/tools/s3_vectors_tool.py)、[iodp-agent/scripts/index_knowledge_base.py](iodp-agent/scripts/index_knowledge_base.py)

### 3.4 Missing credentials: api_key

- **症状**：Lambda 实际跑起来报 OpenAI client 没拿到 api_key
- **根因**：`make deploy` 之前只做 `aws lambda update-function-code`，没跑 `terraform apply`，于是 LLM_API_KEY 环境变量没注入到 Lambda
- **修复**：`make deploy` 改成跑完整 `terraform apply` + 强制 lambda image update
- **文件**：[iodp-agent/Makefile](iodp-agent/Makefile)、[iodp-agent/terraform/variables.tf](iodp-agent/terraform/variables.tf) 加 `llm_api_key` (sensitive=true)

### 3.5 boto3 1.40+ 才有 s3vectors client

- **症状**：`make index-kb` 本地跑报 `UnknownServiceError: Unknown service: 's3vectors'`
- **根因**：S3 Vectors 是 2025-12 才 GA 的服务，本地 WSL 的 boto3 太老
- **修复**：`pip3 install -U boto3 botocore --break-system-packages`；scripts/requirements.txt 显式锁 `>=1.40.0`
- **文件**：[iodp-agent/scripts/requirements.txt](iodp-agent/scripts/requirements.txt)
- **注意**：Lambda 镜像里的 boto3 跟本地是独立的两份。Docker build 时按 [iodp-agent/requirements.txt](iodp-agent/requirements.txt) 装，已经 `>=1.40.0`，所以 Lambda 不用重 build

---

## Phase 4 — Seed Data / 数据契约

### 4.1 awswrangler 缺包

- **症状**：`seed_test_data.py` 跑起来报 `ModuleNotFoundError: awswrangler`
- **修复**：[iodp-agent/scripts/requirements.txt](iodp-agent/scripts/requirements.txt) 加 `awswrangler>=3.0.0`

### 4.2 Athena `InvalidBucketName: s3://s3://...`

- **症状**：`execute_athena_query` 报 InvalidBucketName
- **根因**：env var `IODP_ATHENA_RESULT_BUCKET` 注入的是完整 `s3://iodp-agent-dev-athena-results/`，但 [iodp-agent/src/tools/athena_tool.py](iodp-agent/src/tools/athena_tool.py) 又拼 `f"s3://{output_bucket}/..."` → 变成 `s3://s3://...`
- **修复**：加 prefix detection — 已经是 `s3://` 开头就不再拼
- **文件**：[iodp-agent/src/tools/athena_tool.py:64-68](iodp-agent/src/tools/athena_tool.py#L64-L68)

### 4.3 Athena Iceberg Schema change detected

- **症状**：连续踩 3 次：
  - `modified_columns {stat_date: string}` —— pandas 推 stat_date 成 string，DDL 是 DATE
  - `modified_columns {http_status: bigint}` —— 没显式 cast int32
  - `modified_columns {stat_date: date}` —— cast 成 DATE 又对不上别的表（incident_summary 是 STRING）
- **修复**：seed 脚本里逐列显式 cast
  - `stat_hour=pd.to_datetime(...)` (TIMESTAMP)
  - `stat_date=...dt.date` (DATE，但 incident_summary 是 STRING 例外)
  - `http_status=int32`
  - `event_timestamp/ingest_timestamp/processing_timestamp=pd.to_datetime`
- **文件**：[iodp-agent/scripts/seed_test_data.py](iodp-agent/scripts/seed_test_data.py)

### 4.4 HIVE_BAD_DATA: staging path contamination

- **症状**：`Field stat_date's type BINARY (parquet) is incompatible with type date`
- **根因**：用户用 Perplexity 帮忙诊断出 — `awswrangler.athena.to_iceberg` 默认 staging 临时路径是固定的（如 `_tmp/seed/`），上一次 seed 跑挂了之后那些"字段是 STRING 的 parquet"残留在 staging，下一次 seed 用 DATE 类型时 Athena 试图读老 parquet，类型对不上
- **修复**：每次 seed 用 unique `_RUN_ID = uuid.uuid4().hex[:8]`，所有 to_iceberg 调用都用 `temp_path=f".../{_RUN_ID}/"` + `keep_files=False`
- **文件**：[iodp-agent/scripts/seed_test_data.py](iodp-agent/scripts/seed_test_data.py)

### 4.5 DynamoDB DQ table 名带破折号

- **症状**：Athena 查不到 DQ records
- **根因**：[iodp-agent/src/config.py](iodp-agent/src/config.py) 里写的是 `iodp_dq_reports_dev`（下划线），实际 Terraform 建的是 `iodp-dq-reports-dev`（破折号）
- **修复**：env var `IODP_DQ_REPORTS_TABLE` hardcode 注入 `iodp-dq-reports-${env}`
- **文件**：[iodp-agent/terraform/main.tf](iodp-agent/terraform/main.tf)

---

## Phase 5 — Agent Runtime Bugs（演示阶段）

### 5.1 LLM 不抓中文 user_id

- **症状**：用户说"账户ID: usr_seed_0001"，LLM 返回 `intent=need_more_info, clarification_question="请提供账户ID"`
- **根因**：Qwen Router prompt 没强调中文格式抓取
- **修复**：router 加 regex fallback，所有历史 HumanMessage 合并扫描 `\b(usr[_-]\w+|user[_-]?\d+|u_\d+)\b`；拿到就强制 `intent=tech_issue`
- **文件**：[iodp-agent/src/graph/nodes/router_agent.py:95-118](iodp-agent/src/graph/nodes/router_agent.py#L95-L118)

### 5.2 log_analyzer 让 LLM 生成 SQL 不稳定

- **症状**：bug_report 永远 `evidence_trace_ids=[]`，root_cause 总是"证据不足"
- **根因**：让 LLM 生成 Athena SQL 太不可靠（拼错表名、缺 quote、SELECT 列写错），错了被 try/except 吞掉
- **修复**：废弃 LLM 生成 SQL，hardcode SQL 直接查 `v_error_log_enriched`。这是架构契约里就规定好的视图，没必要每次让 LLM 现想
- **文件**：[iodp-agent/src/graph/nodes/log_analyzer_agent.py:72-96](iodp-agent/src/graph/nodes/log_analyzer_agent.py#L72-L96)

### 5.3 Anonymous 用户死循环

- **症状**：用户说"查不到账户ID"、"没账户ID"，router 还是反复追问
- **根因**：router 的 prompt 不识别"拒绝提供"语义；`max_clarification_iterations=3` 的限速也没生效（MemorySaver 在 Lambda 冷启动会丢内存）
- **修复**：
  - regex 检测 `(没有|查不到|不知道|不记得|忘了|没ID|无账户|...)` → 设 `user_id="anonymous"`
  - log_analyzer 看到 `is_anonymous` 时 SQL 跳过 `WHERE user_id =`，改查该时段全平台错误
  - bug_report 看到 anonymous 时 prompt 加注解"该时段平台层面有故障可能影响了您 / 建议提供账户ID精确定位"
- **文件**：
  - [iodp-agent/src/graph/nodes/router_agent.py:111-119](iodp-agent/src/graph/nodes/router_agent.py#L111-L119)
  - [iodp-agent/src/graph/nodes/log_analyzer_agent.py:80-83](iodp-agent/src/graph/nodes/log_analyzer_agent.py#L80-L83)
  - [iodp-agent/src/graph/nodes/bug_report_agent.py:107,120-126](iodp-agent/src/graph/nodes/bug_report_agent.py#L107-L126)

### 5.4 时间 regex 漏 day_word

- **症状**：用户输入"昨天晚上10点"，router 只 capture 到"晚上 10点"漏掉"昨天"；`_parse_time_hint` base_date=今天，查询窗口对不上 seed data（昨晚 22:00）
- **根因**：旧 regex 是 `(昨晚|今晚|昨天|今天|...|晚上|...)?\s*\d{1,2}\s*[点]`，只能 OR 出一个 group，匹配引擎选了"晚上 10点"
- **修复**：拆成两次 `findall`，day_words 和 period_words 分别累加 join；range 单独抓
- **文件**：[iodp-agent/src/graph/nodes/router_agent.py:103-126](iodp-agent/src/graph/nodes/router_agent.py#L103-L126)

### 5.5 LLM 半对结果覆盖了 regex fallback

- **症状**：上面修了 regex 还是不行 —— bug_report 显示 `start: "晚上10点"`，没有"昨天"
- **根因**：`if not result.get("incident_time_hint"):` 这个守卫让 LLM 返回非空时 regex 根本不跑。但 LLM 返回的是"半对"结果（漏 day_word）
- **修复**：regex 总是跑，找到完整短语就覆盖 LLM 输出。LLM 半对不如 regex 全对
- **文件**：[iodp-agent/src/graph/nodes/router_agent.py:102-126](iodp-agent/src/graph/nodes/router_agent.py#L102-L126)

### 5.6 时间区间不识别

- **症状**：用户说"10点到11点"，`_parse_time_hint` 只抓单时间点，加 ±1.5h 缓冲
- **修复**：`_parse_time_hint` 加 range 解析：`(\d+)\s*[点]\s*(?:到|至|~|-)\s*(\d+)\s*[点]` → `start_hour=22, end_hour=23`
- **文件**：[iodp-agent/src/graph/nodes/log_analyzer_agent.py:24-65](iodp-agent/src/graph/nodes/log_analyzer_agent.py#L24-L65)

### 5.7 跨 turn state 污染（重大架构 bug）

- **症状**：开新对话连续问 3 个不同意图（"币种" → "开发票" → "退款"），三条回复一字不差全是"币种"那条
- **根因**：[iodp-agent/src/graph/state.py:102-117](iodp-agent/src/graph/state.py#L102-L117) 的 `merge_synthesizer` 用 `or` 语义：
  ```python
  user_reply=a.get("user_reply") or b.get("user_reply")
  ```
  上一 turn 的 user_reply 是 truthy，新 turn 的 reply_agent 输出永远进不来
- **设计澄清**：当前架构没有显式 turn 概念。作者隐式定义 turn = 一次 POST /diagnose = 一个 job_id；`initial_state` 已经把 `router/log_analyzer/rag/synthesizer` 都传 None 想"清空"。但只有 synthesizer 用了自定义 reducer，把 `b=None` 当作"输入方没贡献"忽略了
- **修复**：reducer 改为 `if b is None: return None`，让 turn 起点的显式 reset 信号生效。并行 fan-out 时 b 永远是 dict 不是 None，不影响
- **文件**：[iodp-agent/src/graph/state.py:102-127](iodp-agent/src/graph/state.py#L102-L127)

---

## 跨项目契约层 gotchas

### 6.1 Glue Database 必须 Terraform 建，Iceberg Table 必须 DDL 建

- bigdata 的 `make init` 是两步：`deploy-infra` → `deploy-ddl`，不能颠倒
- 原因：Terraform 的 Iceberg TBLPROPERTIES 支持不完整；Glue Database 又不能用 SQL 建

### 6.2 Agent 部署强依赖 BigData

- root `make init-agent` 从 bigdata `terraform output` 读 `dq_reports_table_arn` / `gold_bucket_arn` / `athena_workgroup` / `athena_result_bucket` 注入到 agent
- 如果 bigdata 已 `make destroy`，agent 重新 init 会读到 fallback 字符串（如 `arn:aws:dynamodb:...:table/iodp_dq_reports_dev`），表实际不存在，运行时才报错

### 6.3 Glue Triggers 默认关

- FinOps 设计：cron 跑一周 ~$35。Demo 时用 `make demo-pipeline` 手动跑批
- 不要在 DDL 还没 apply 时 enable triggers，否则 cron 触发 job 报 TableNotFoundException

---

## 排错心法（个人总结）

1. **awswrangler.athena.to_iceberg 报 schema 错时，第一件事不是改 pandas 类型，是清 staging 目录或换 unique temp_path**。Schema 错经常是上次失败的残留 parquet 在污染下一次。
2. **LLM 生成结构化输出时，永远要 regex 兜底**。Qwen / Claude / GPT 都会"半对"，比如"昨天晚上10点"只抓"晚上10点"。
3. **LangGraph 自定义 reducer 要考虑跨 turn 重置语义**。`or` 这种"取真值"看着自然，但跨 turn 时会锁死上一轮的值。
4. **Lambda 镜像和本地 Python 是两份 boto3**。本地装的 boto3 不影响线上，反之亦然。看哪个挂就在哪个环境里 verify 版本。
5. **Athena view 是 metastore 里的 logical object，需要单独 apply DDL**，跟 table 同等地位。脚本扫 ddl/ 目录的时候别漏 views/。
6. **AWS region 在 dashboard widget 里要显式写**，CloudWatch 默认 us-east-1，跟实际数据 region 不一致就显示 "No data"。
