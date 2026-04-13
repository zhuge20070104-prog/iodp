# src/graph/nodes/rag_agent.py
"""
RAG Agent 节点 v2
改进：使用 get_error_logs() helper 读取 log_analyzer 子结构
输出写入 state["rag"] (RAGOutput)
"""

from langchain_aws import ChatBedrock
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ..state import AgentState, RAGDocument, RAGOutput, get_error_logs
from src.config import settings
from src.tools.opensearch_tool import vector_search

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

    context_for_rag = (
        f"用户描述：{raw_input}\n"
        f"错误码：{', '.join(error_codes) if error_codes else '未知'}\n"
        f"受影响服务：{', '.join(service_names) if service_names else '未知'}\n"
        f"错误信息摘要：{top_error_msg or '无'}\n"
        f"错误率：{max_error_rate:.1%}"
    )

    llm = ChatBedrock(
        model_id=settings.bedrock_model_id,
        region_name=settings.aws_region,
        model_kwargs={"max_tokens": 512, "temperature": 0},
    )

    rag_query_response = llm.invoke([
        SystemMessage(content=RAG_QUERY_GENERATION_PROMPT),
        HumanMessage(content=context_for_rag),
    ])
    rag_query = rag_query_response.content.strip()

    raw_hits = vector_search(
        query_text=rag_query,
        index_names=["product_docs", "incident_solutions"],
        top_k=5,
        opensearch_endpoint=settings.opensearch_endpoint,
        region=settings.aws_region,
        filter_error_codes=error_codes if error_codes else None,
    )

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

    return {
        "rag": RAGOutput(
            rag_query=rag_query,
            retrieved_docs=retrieved_docs,
        ),
        "messages": [AIMessage(content=msg)],
    }
