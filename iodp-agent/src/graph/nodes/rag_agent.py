# src/graph/nodes/rag_agent.py
"""
RAG Agent 节点 v2
改进：使用 get_error_logs() helper 读取 log_analyzer 子结构
输出写入 state["rag"] (RAGOutput)

后端：Amazon S3 Vectors（GA 2025-12 取代 OpenSearch Serverless，成本降低 ~90%）
"""

from langchain_core.messages import AIMessage, HumanMessage

from ..state import AgentState, RAGDocument, RAGOutput, get_error_logs
from src.config import settings
from src.log_utils import log_event
from src.tools.s3_vectors_tool import vector_search
from ._llm_helpers import build_router_llm, cached_system

RAG_QUERY_GENERATION_PROMPT = """
你是一个检索专家。根据以下信息，生成一段用于向量检索的自然语言查询，
使其能从技术文档库中召回最相关的故障排查文档。

要求：
- 包含所有出现的 error_code
- 描述核心症状（慢？报错？无响应？）
- 长度在 2-4 句话之间
- 只输出查询文本本身，不要任何前缀
""".strip()


def rag_agent_node(state: AgentState) -> dict:
    """RAG Agent：生成检索 Query → 向量检索 → 返回相关文档"""
    error_logs  = get_error_logs(state)
    raw_input   = state.get("raw_user_input", "")

    error_codes   = list({log["error_code"] for log in error_logs if log.get("error_code")})
    service_names = list({log["service_name"] for log in error_logs if log.get("service_name")})
    top_error_msg = error_logs[0]["error_message"] if error_logs else ""
    max_error_rate = max((log["error_rate"] for log in error_logs), default=0.0)

    log_event(
        "node", "enter", node="rag",
        error_codes=error_codes, service_names=service_names,
        max_error_rate=max_error_rate,
        thread_id=state.get("thread_id"), job_id=state.get("job_id"),
    )

    context_for_rag = (
        f"用户描述：{raw_input}\n"
        f"错误码：{', '.join(error_codes) if error_codes else '未知'}\n"
        f"受影响服务：{', '.join(service_names) if service_names else '未知'}\n"
        f"错误信息摘要：{top_error_msg or '无'}\n"
        f"错误率：{max_error_rate:.1%}"
    )

    llm = build_router_llm(max_tokens=512, temperature=0)

    rag_query_response = llm.invoke([
        cached_system(RAG_QUERY_GENERATION_PROMPT),
        HumanMessage(content=context_for_rag),
    ])
    rag_query = rag_query_response.content.strip()
    log_event("node", "rag_query_generated", node="rag", query_chars=len(rag_query), query=rag_query)

    # 两阶段检索：先用 error_code 精确 filter，0 命中时去掉 filter 走纯语义召回。
    # 触发场景：日志真实抽中的 error_code 不在 KB seed 范围内（如 producer 抽到
    # E2002 但 KB 只 seed 了 E2001/E2003/E5001）。语义召回能拿到同类别（同 service
    # 或同症状）的相似 incident，比 0 命中有参考价值得多。
    raw_hits = []
    try:
        if error_codes:
            raw_hits = vector_search(
                query_text=rag_query,
                index_names=["product-docs", "incident-solutions"],
                top_k=5,
                vector_bucket_name=settings.vector_bucket_name,
                region=settings.aws_region,
                filter_error_codes=error_codes,
            )
            if not raw_hits:
                log_event(
                    "node", "rag_filter_miss", node="rag",
                    filter_error_codes=error_codes,
                    fallback="semantic_search_no_filter",
                )
        # 没拿到带 filter 的命中（或本来就没 error_code）→ 不带 filter 再搜一次
        if not raw_hits:
            raw_hits = vector_search(
                query_text=rag_query,
                index_names=["product-docs", "incident-solutions"],
                top_k=5,
                vector_bucket_name=settings.vector_bucket_name,
                region=settings.aws_region,
                filter_error_codes=None,
            )
    except Exception as e:
        log_event("node", "rag_search_failed", node="rag", error=str(e))
        raw_hits = []

    retrieved_docs: list[RAGDocument] = [
        RAGDocument(
            doc_id=hit["_id"],
            title=hit["_source"].get("title", ""),
            content=hit["_source"].get("content", ""),
            doc_type=hit["_source"].get("doc_type", "product_doc"),
            relevance_score=hit["_score"],
            error_codes=hit["_source"].get("error_codes", []),
        )
        for hit in raw_hits
    ]

    msg = (
        f"[RAG Agent] 检索到 {len(retrieved_docs)} 篇相关文档，"
        f"最高相关度 {retrieved_docs[0]['relevance_score']:.2f}。"
        if retrieved_docs
        else "[RAG Agent] 未找到相关文档。"
    )

    log_event(
        "node", "exit", node="rag",
        docs=len(retrieved_docs),
        top_score=retrieved_docs[0]["relevance_score"] if retrieved_docs else None,
        doc_types=[d["doc_type"] for d in retrieved_docs],
    )

    return {
        "rag": RAGOutput(
            rag_query=rag_query,
            retrieved_docs=retrieved_docs,
        ),
        "messages": [AIMessage(content=msg)],
    }
