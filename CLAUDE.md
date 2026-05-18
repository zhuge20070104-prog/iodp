# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Monorepo layout

Two sibling projects deployed in sequence, with the root `Makefile` orchestrating cross-project ARN passing:

- `iodp-bigdata/` — Serverless Medallion data lake (Firehose → S3 Bronze/Silver/Gold Iceberg → Athena). Produces the data and the ARNs that the agent consumes.
- `iodp-agent/` — Multi-agent diagnostic system (FastAPI + LangGraph on Lambda, DashScope Qwen via OpenAI-compatible API, S3 Vectors RAG). Consumes BigData's Athena views, DynamoDB DQ reports, and S3 Vectors indexes.

**Deploy order is load-bearing**: BigData must exist before Agent, because root `make init-agent` reads BigData's `terraform output` for `dq_reports_table_arn`, `gold_bucket_arn`, `athena_workgroup`, `athena_result_bucket` and injects them as TF vars into the agent project. Destroy order is reversed (agent first, then bigdata).

## Common commands

All commands assume `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are exported. Default region is `ap-southeast-1`, default env is `dev`.

### Whole platform (from repo root)

```bash
make init       # Phase 1: bigdata → Phase 2: agent (auto-wires ARNs)
make deploy     # Update both
make destroy    # Agent first, then bigdata (S3 data is permanently lost)
make test-api   # End-to-end POST /diagnose
make status     # Terraform output + AWS resource status for both
```

### iodp-bigdata (`cd iodp-bigdata`)

```bash
make init                  # Two-step bootstrap: deploy-infra → deploy-ddl
make deploy-infra          # terraform apply with triggers_enabled=false
make deploy-ddl            # bash scripts/apply_ddl.sh — Athena creates 5 Iceberg tables
make produce-sample        # Push sample events to Firehose (COUNT=1000 default)
make demo-pipeline         # Manually run Silver → wait 5min → Gold
make enable-triggers       # Switch to cron mode (~$35/wk)
make disable-triggers      # Switch back to manual (default, FinOps)
```

**Two-step bootstrap is non-negotiable**: Glue Database must be created by Terraform (`aws_glue_catalog_database`), but Iceberg Tables must be created by Athena DDL (Terraform's Iceberg `TBLPROPERTIES` support is incomplete). Running DDL before `deploy-infra` → `Database does not exist`. Enabling triggers before DDL → `TableNotFoundException` from cron-fired jobs.

Glue triggers default to **disabled** (FinOps). Demos rely on `make demo-pipeline` to fan out manual job runs; only flip to cron when you actually need 24×7 ingestion.

### iodp-agent (`cd iodp-agent`)

```bash
make init           # 7-step: tf init → create ECR → docker build → push → tf apply full infra → seed-data → index-kb
make deploy         # Rebuild image + update Lambda only (no infra changes)
make deploy-all     # Backend + frontend (S3/CloudFront)
make seed-data      # Inject demo data into Athena + DynamoDB
make index-kb       # Re-index RAG knowledge base into S3 Vectors
make test-api       # POST /diagnose with sample payload
```

### Running a single test

`pytest` is used in both projects' `tests/unit/`. There's no `make test` target. Run directly:

```bash
cd iodp-agent && python -m pytest tests/unit/test_router_agent.py -k "test_classify_intent_tech_issue" -v
cd iodp-bigdata && python -m pytest tests/unit/<file>.py::<test_name> -v
```

## Architecture: data flow across the two projects

```
Producer (boto3 put_record_batch)
    │ NDJSON over HTTPS (Firehose Direct PUT, IAM SigV4)
    ▼
Firehose × 2 (clickstream, app_logs)  — buffer 5 MB / 60s, GZip + dynamic partitioning
    ▼
S3 Bronze (year=/month=/day=/hour=/*.gz, schema-on-read NDJSON)
    ▼ Glue Batch (silver_enrich_clicks, silver_parse_logs) — DQ + dedup + MERGE
S3 Silver (Iceberg Parquet)
    ▼ Glue Batch (hourly_active_users, api_error_stats, incident_summary)
S3 Gold (Iceberg Parquet, pre-aggregated)
    │
    ├──→ Athena v_error_log_enriched view ──→ Agent log_analyzer_agent (real-time error lookup)
    ├──→ DynamoDB iodp-dq-reports-{env}   ──→ Agent log_analyzer (filter false positives)
    └──→ S3 Vectors lambda/vector_indexer ──→ Agent rag_agent (historical incident similarity)
```

The agent **does not** import bigdata code. The contract between them is purely AWS resource ARNs passed via Terraform vars and well-known table/view names.

## iodp-bigdata architecture notes

- **Schema-on-read**: Bronze stays NDJSON; there is no `schemas/` directory. The data contract lives in two places — `scripts/produce_sample_events.py` (producer-side example) and the docstring "Bronze NDJSON 预期 schema" block at the top of `glue_jobs/batch/silver_*.py`. If upstream adds fields, Bronze keeps them but Silver ignores until you edit the `.select()` and the Iceberg DDL.
- **No VPC, no MSK, no Glue Streaming** — these were the "Plan D" FinOps cut (97% cost reduction). Don't reintroduce them without checking `EXPLANATION.md` and `INTERVIEW.md` for the reasoning. Modules `networking/` and `streaming/` are deleted; any reference in stale docs should be ignored.
- **DQ lives in the Silver batch job** (not in a separate streaming job). Framework is `glue_jobs/lib/data_quality.py`; thresholds are in DynamoDB `iodp-dq-threshold-config-{env}` so ops can tune without redeploying.
- **8 Terraform modules**: `storage`, `dynamodb`, `ingestion` (Firehose), `compute` (Glue Catalog + Batch + Triggers), `observability`, `dlq_replay`, `replay_jobs`, `vector_indexer`. The root `main.tf` wires them; cross-module references are ARN-based.

## iodp-agent architecture notes

- **Async job pattern**: `POST /diagnose` returns 202 immediately with a `job_id` and kicks the LangGraph run into `BackgroundTasks`; clients poll `GET /diagnose/{job_id}`. This exists specifically to dodge API Gateway's 29-second timeout. Do not change this to synchronous.
- **LangGraph state machine** (see `src/graph/graph_builder.py`):
  ```
  router_agent ─┬─ tech_issue + user_id  → log_analyzer → rag → (reply_agent ‖ bug_report_agent) → END
                ├─ tech_issue no user_id → END (ask for user_id, next turn)
                ├─ inquiry               → rag → reply → END
                ├─ refund / unknown      → reply → END
                ├─ security_violation    → reply → END (refusal)
                └─ need_more_info        → END (clarify)
  ```
  `reply_agent` and `bug_report_agent` run in parallel after `rag_agent`; the merge happens in `merge_synthesizer`.
- **Multi-turn conversations** are persisted via the DynamoDB LangGraph checkpointer keyed on `thread_id`. One `thread_id` produces many `job_id`s (each user message = one job).
- **Dual-model routing for FinOps**: complex reasoning nodes (`log_analyzer`, `bug_report`) use `qwen-max`; simple nodes (`router`, `rag`, `reply`) use `qwen-turbo`. Configured in `src/config.py` (`llm_reasoning_model` / `llm_router_model`). LLM is reached via OpenAI-compatible API at `dashscope.aliyuncs.com/compatible-mode/v1`; the project switched off Bedrock because the AWS account is China-registered and can't get Bedrock allowlisting.
- **Three DynamoDB tables**: `iodp-agent-state-{env}` (checkpointer, 7d TTL), `iodp-bug-tickets-{env}` (bug reports + severity GSI), `iodp-agent-jobs-{env}` (async job tracking, 1h TTL, GSI on thread_id).
- **RAG is S3 Vectors, not OpenSearch**. Replaced OpenSearch Serverless in Dec 2025 for ~90% cost savings. `boto3>=1.40` ships the `s3vectors` client; embedding goes through DashScope `text-embedding-v3` (same key as chat).
- **Settings precedence**: Pydantic `BaseSettings` with `env_prefix="IODP_"`. Override anything at runtime via env var (e.g. `IODP_MAX_CLARIFICATION_ITERATIONS=5`).

## Cross-project gotchas

- After running `make destroy` in `iodp-bigdata`, the agent's Lambda still has the old DQ table / Gold bucket ARNs baked into its env vars. Re-run `make init` from the root (or `make destroy && make init`) to repair the wiring.
- The root `make init-agent` falls back to *constructed* ARN strings (e.g. `arn:aws:dynamodb:...:table/iodp_dq_reports_${ENV}`) when `terraform output` returns nothing. If you're deploying the agent without an actual bigdata stack, those fallback ARNs point to nothing — log_analyzer queries will fail at runtime, not at deploy.
- Glue scripts must be uploaded to S3 *before* `make init` in bigdata if the scripts bucket already exists from a prior run — use `bash scripts/upload_glue_scripts.sh dev`.
- Cost reminder embedded in the Makefiles: enabling Glue triggers costs ~$35/wk; S3 Vectors is near-free at rest; Lambda + DashScope are pay-per-invoke (Qwen 按 token 计费，比 Bedrock Claude 便宜 ~80%). The deliberate default is "everything off until you demo."

## Shell environment

This repo is developed on Windows but the Makefiles assume bash (they use `$(shell ...)`, `&&`, single-quoted heredocs). Use the Bash tool for `make` invocations and shell scripts under `scripts/`. PowerShell is fine for plain `aws`/`terraform`/`docker` one-liners.
