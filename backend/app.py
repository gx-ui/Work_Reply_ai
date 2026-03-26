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
from utils.common import parse_suggestion, parse_summary
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
