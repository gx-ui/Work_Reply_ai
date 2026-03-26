"""
Work reply AI Backend
"""
from __future__ import annotations
import logging
import time
import traceback
from datetime import datetime
from typing import Dict, Any
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from backend.model.request_entity import ChatRequest
from backend.model.response_entity import Suggestion, Summary
from backend.services.agent_service import init_state, get_state, ensure_agentos_runtime, get_agentos_app, team_run, agent_run, get_knowledge_sources, reset_knowledge_sources
from backend.services.prompt_service import build_agent_input, build_summary_input
from tools.rag_retrieval_tool import KnowledgeRetrievalTool
from prompt.query_agent_prompt import QUERY_PROMPT_TEMPLATE
from utils.common import parse_suggestion, parse_summary, parse_query_answer
from utils.milvus_utils import clip_text
from utils.log_utils import configure_logging

logger = logging.getLogger("work_reply_ai")
configure_logging()
init_state()

app = FastAPI(title="Work reply AI Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agentos_app = get_agentos_app(allow_rag=True)
if agentos_app is not None:
    app.mount("/agentos", agentos_app)
    logger.info("✅ AgentOS 子应用挂载成功：/agentos")
else:
    logger.warning("⚠️ AgentOS 子应用未挂载")

api_router = APIRouter(prefix="/work_reply_ai")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    request_id = f"{int(time.time() * 1000)}"
    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        return response
    except Exception as e:
        process_time = time.time() - start_time
        logger.error(f"❌ 请求异常\n"
                     f"🆔 ID: {request_id}\n"
                     f"🔗 路径：{request.method} {request.url.path}\n"
                     f"⚡ 错误：{type(e).__name__}: {e}\n"
                     f"⏱️ 耗时：{process_time:.3f}s")
        logger.error(traceback.format_exc())
        raise


async def _handle_team_intent(chat_req: ChatRequest) -> Dict[str, Any]:
    ensure_agentos_runtime(allow_rag=True)
    state = get_state()
    works = chat_req.works_info
    core = chat_req.core_info
    attention = chat_req.attention_info
    logger.info(
        f"📨 收到聊天请求\n"
        f"🎯 意图：{chat_req.intent}\n"
        f"📝 标题：{str(works.title or '').strip()}\n"
        f"📝 描述：{str(works.desc or '').strip()}\n"
        f"🏷️ 标签：{works.tags[:20]}\n"
        f"👤 客户：{core.customer_name}\n"
        f"🧩 项目：{core.project_name}\n"
        f"🏬 商城：{core.mall_name}\n"
        f"⚠️ 项目注意：{attention.project_attention}\n"
        f"⚠️ 供应商注意：{attention.supplier_attention}\n"
        f"📜 历史记录：{works.history}"
    )
    reset_knowledge_sources()

    # summary 意图：直接调用 summary_agent，跳过 Team Router
    if chat_req.intent == "summary":
        if state.agent_summary is None:
            raise HTTPException(status_code=500, detail="Summary Agent 初始化失败")
        summary_prompt = build_summary_input(chat_req)
        raw = await agent_run(state.agent_summary, summary_prompt)
        summary_raw = parse_summary(raw)
        summary = Summary(
            info_summary=str(summary_raw.get("info_summary") or "").strip() or "待确认",
            reviews=str(summary_raw.get("reviews") or summary_raw.get("review") or "").strip() or "无",
        )
        return {"summary": summary.model_dump()}

    # query 意图：直接全库语义检索，然后用 LLM 整理回答
    if chat_req.intent == "query":
        user_query = str(chat_req.query or "").strip()
        if not user_query:
            raise HTTPException(status_code=400, detail="query 字段不能为空")

        retrieval_tool = KnowledgeRetrievalTool(config_loader=state.config)
        raw_results = retrieval_tool.search(query=user_query, limit=5)

        sources = []
        search_text = "未找到相关结果"
        if isinstance(raw_results, list) and raw_results:
            seen_sources = set()
            lines = [f"检索到 {len(raw_results)} 条结果：", ""]
            for i, item in enumerate(raw_results, 1):
                text = str(item.get("text", "") or "")
                fn = str(item.get("file_name", "") or "").strip()
                safe_text = clip_text(text, 500)
                source_label = f"[来源: {fn}] " if fn else ""
                lines.append(f"【{i}】{source_label}{safe_text}")
                lines.append("")
                if fn and fn not in seen_sources:
                    seen_sources.add(fn)
                    sources.append(fn)
            search_text = "\n".join(lines)

        # 用 LLM 整理检索结果
        if state.agent_plain is None:
            raise HTTPException(status_code=500, detail="Query Agent 初始化失败")
        query_prompt = QUERY_PROMPT_TEMPLATE.format(
            user_query=user_query,
            search_results=search_text
        )
        raw_answer = await agent_run(state.agent_plain, query_prompt)
        parsed = parse_query_answer(raw_answer)
        # 合并：LLM 解析出的 sources 与 Milvus 直接提取的 sources 取并集
        llm_sources = parsed.get("sources") or []
        all_sources = list(dict.fromkeys(sources + [s for s in llm_sources if s not in sources]))

        return {"answer": parsed.get("answer", ""), "sources": all_sources}

    # suggestion 意图：直接调用 work_reply_agent（优先 RAG），跳过 Team Router
    suggestion_agent = (
        state.agent_rag
        if (state.rag_enabled and state.agent_rag is not None)
        else state.agent_plain
    )
    if suggestion_agent is None:
        raise HTTPException(status_code=500, detail="Suggestion Agent 初始化失败")
    suggestion_prompt = build_agent_input(chat_req, works_tags=works.tags)
    raw = await agent_run(suggestion_agent, suggestion_prompt)
    suggestion_content = parse_suggestion(raw)
    if not suggestion_content:
        suggestion_content = "知识库中暂未检索到相关信息，建议补充订单/问题细节后再核实处理。"
    suggestion = Suggestion(content=suggestion_content)
    return {"suggestion": suggestion.content, "knowledge_sources": get_knowledge_sources()}


@api_router.post("/chat")
async def unified_chat(request: ChatRequest):
    try:
        return await _handle_team_intent(request)
    except Exception as e:
        logger.error(f"❌ /chat 处理失败\n"
                     f"⚡ 错误类型：{type(e).__name__}\n"
                     f"💥 错误信息：{e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/health")
async def health_check():
    state = get_state()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "Work reply AI Backend",
        "rag_enabled": bool(state.rag_enabled),
    }

@app.get("/")
async def root():
    return {
        "service": "Work reply AI Backend",
        "version": "1.0",
        "endpoints": {
            "chat": "/work_reply_ai/chat",
            "health": "/work_reply_ai/health",
            "agentos": "/agentos",
        },
    }


app.include_router(api_router)
