"""
Work reply AI Backend
"""
from __future__ import annotations
import json
import logging
import re
import time
import traceback
from datetime import datetime
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from entity.request import ChatRequest
from entity.response import QueryAnswer, Suggestion, Summary
from db.chat_run_store import persist_chat_run_safe
from services.agent_service import (
    init_state,
    get_state,
    ensure_agents,
    ensure_summary_agent,
    agent_run,
    agent_run_stream_collect,
    reset_knowledge_sources,
    merge_knowledge_source_names,
    RuntimeState,
)
from utils.log_utils import (
    configure_logging,
    begin_request_trace,
    end_request_trace,
    update_request_context,
    reset_request_context,
    get_request_context,
    get_request_stats,
    make_request_id,
)

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

# 与线上网关路径一致：https://ai-gateway-show.yunzhonghe.com/cs_assist_ai/...
api_router = APIRouter(prefix="/cs_assist_ai")


def _persist_chat_run_if_enabled(
    state: RuntimeState,
    chat_req: ChatRequest,
    *,
    suggestion: Optional[Suggestion] = None,
    summary: Optional[Summary] = None,
    query_answer: Optional[QueryAnswer] = None,
) -> None:
    cr_cfg = state.config.get_chat_run_persistence_config()
    if not cr_cfg.get("enabled") or state.mysql_engine is None:
        return
    persist_chat_run_safe(
        state.mysql_engine,
        cr_cfg["table"],
        chat_req,
        suggestion=suggestion,
        summary=summary,
        query_answer=query_answer,
    )


def _parse_model_json(raw: str) -> Dict[str, Any]:
    """解析模型输出根对象为 dict。

    - 模型常在合法 JSON 后再拼接说明，json.loads 会报 Extra data；用 raw_decode 只取第一个对象。
    - 可选剥离首尾 ``` / ```json 代码块。
    """
    s = str(raw or "").strip()
    if not s:
        raise ValueError("模型输出为空")
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"\s*```\s*$", "", s).strip()
    start = s.find("{")
    if start < 0:
        raise ValueError("模型输出中未找到 JSON 对象")
    obj, _end = json.JSONDecoder().raw_decode(s[start:])
    if not isinstance(obj, dict):
        raise TypeError("JSON 根须为对象")
    return obj


def _ensure_chat_agents() -> None:
    """初始化 /chat 所需 Agent。"""
    ensure_agents(allow_rag=True)
    ensure_summary_agent()


def _get_work_reply_agent(state: RuntimeState):
    agent = (
        state.agent_rag
        if (state.rag_enabled and state.agent_rag is not None)
        else state.agent_plain
    )
    if agent is None:
        raise HTTPException(status_code=500, detail="WorkReply Agent 初始化失败")
    if state.work_reply_runner is None:
        raise HTTPException(status_code=500, detail="WorkReply runner 未初始化")
    return agent


def _sse_bytes(payload: Dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _scoped_session_id(session_id: Optional[str], intent: str) -> Optional[str]:
    base = str(session_id or "").strip()
    if not base:
        return None
    suffix = f":{intent}"
    if base.endswith(suffix):
        return base
    return f"{base}{suffix}"


def _log_chat_completion(
    *,
    endpoint: str,
    intent: str,
    total_ms: int,
    kb_hit_docs: int,
    final_sources: int,
    fallback_used: bool,
    tool_audit: Optional[Dict[str, Any]] = None,
) -> None:
    stats = get_request_stats()
    audit = tool_audit or {}
    logger.info(
        "[请求完成] %s\n"
        "intent: %s\n"
        "总耗时: %sms\n"
        "LLM调用: %s\n"
        "Embedding调用: %s\n"
        "Telemetry调用: %s\n"
        "命中文档数: %s\n"
        "最终sources数: %s\n"
        "工具可用数: %s\n"
        "工具调用次数: %s\n"
        "工具调用详情: %s\n"
        "未调用原因标签: %s\n"
        "fallback_used: %s",
        endpoint,
        intent,
        total_ms,
        stats.get("llm_calls", 0),
        stats.get("embedding_calls", 0),
        stats.get("telemetry_calls", 0),
        kb_hit_docs,
        final_sources,
        audit.get("available_tool_count", 0),
        audit.get("called_tool_count", 0),
        audit.get("called_tool_histogram", {}),
        audit.get("no_tool_reason", "unknown"),
        fallback_used,
    )


async def _handle_chat(chat_req: ChatRequest) -> Dict[str, Any]:
    started_at = time.time()
    _ensure_chat_agents()
    state = get_state()
    works = chat_req.works_info
    sid = (chat_req.session_id and str(chat_req.session_id).strip()) or None
    agent_sid = _scoped_session_id(sid, chat_req.intent)
    ctx_token = update_request_context(
        intent=chat_req.intent,
        ticket_id=works.ticket_id,
        session_id=sid or "",
    )
    logger.info(
        "收到 /chat intent=%s ticket_id=%s session_id=%s agent_session_id=%s",
        chat_req.intent,
        works.ticket_id,
        sid or "",
        agent_sid or "",
    )
    reset_knowledge_sources()
    try:
        # summary 意图
        if chat_req.intent == "summary":
            if state.agent_summary is None:
                raise HTTPException(status_code=500, detail="Summary Agent 初始化失败")

            summary_prompt = state.summary_runner.format_prompt(chat_req)
            raw, summary_kb, tool_audit = await agent_run(
                state.agent_summary,
                summary_prompt,
                session_id=agent_sid,
            )

            inner = _parse_model_json(raw)["summary"]
            if not isinstance(inner, dict):
                raise HTTPException(status_code=502, detail="摘要 JSON：summary 须为对象")
            summary = Summary(
                info_summary=inner["info_summary"],
                reviews=inner["reviews"],
                summary_sources=summary_kb,
            )
            _persist_chat_run_if_enabled(state, chat_req, summary=summary)
            _log_chat_completion(
                endpoint="/chat",
                intent=chat_req.intent,
                total_ms=int((time.time() - started_at) * 1000),
                kb_hit_docs=len(summary_kb),
                final_sources=len(summary_kb),
                fallback_used=False,
                tool_audit=tool_audit,
            )
            return {"summary": summary.model_dump()}

        if chat_req.intent == "query":
            user_query = chat_req.query_info.query
            if not user_query:
                raise HTTPException(status_code=400, detail="query_info.query 不能为空")
            query_agent = _get_work_reply_agent(state)
            query_prompt = state.work_reply_runner.format_query_prompt(chat_req)
            raw_answer, query_trace_sources, tool_audit = await agent_run(
                query_agent,
                query_prompt,
                session_id=agent_sid,
            )
            qobj = _parse_model_json(raw_answer)
            answer = str(qobj.get("answer", "")).strip()
            fallback_used = False
            if not answer:
                fallback_used = True
                answer = "知识库中暂未找到与当前问题直接相关的依据，建议补充更具体的业务关键词后再查询。"
            llm_sources = qobj.get("sources") or []
            if not isinstance(llm_sources, list):
                raise HTTPException(status_code=502, detail="query JSON：sources 须为数组")
            all_sources = merge_knowledge_source_names(query_trace_sources, llm_sources)

            qa = QueryAnswer(answer=answer, query_sources=all_sources)
            _persist_chat_run_if_enabled(state, chat_req, query_answer=qa)
            _log_chat_completion(
                endpoint="/chat",
                intent=chat_req.intent,
                total_ms=int((time.time() - started_at) * 1000),
                kb_hit_docs=len(query_trace_sources),
                final_sources=len(all_sources),
                fallback_used=fallback_used,
                tool_audit=tool_audit,
            )
            return qa.model_dump(by_alias=True)

        if chat_req.intent == "suggestion":
            work_reply_agent = _get_work_reply_agent(state)
            suggestion_prompt = state.work_reply_runner.format_prompt(chat_req)
            raw, sugg_kb, tool_audit = await agent_run(
                work_reply_agent,
                suggestion_prompt,
                session_id=agent_sid,
            )

            suggestion_content = _parse_model_json(raw).get("suggestion", "")
            fallback_used = False
            if not suggestion_content:
                fallback_used = True
                suggestion_content = "知识库中暂未检索到相关信息，建议补充订单/问题细节后再核实处理。"
            suggestion = Suggestion(content=suggestion_content, suggestion_sources=sugg_kb)
            _persist_chat_run_if_enabled(state, chat_req, suggestion=suggestion)
            _log_chat_completion(
                endpoint="/chat",
                intent=chat_req.intent,
                total_ms=int((time.time() - started_at) * 1000),
                kb_hit_docs=len(sugg_kb),
                final_sources=len(sugg_kb),
                fallback_used=fallback_used,
                tool_audit=tool_audit,
            )
            return suggestion.model_dump(by_alias=True)

        raise HTTPException(
            status_code=400,
            detail=f"不支持的 intent：{chat_req.intent!r}，应为 suggestion / summary / query",
        )
    finally:
        reset_request_context(ctx_token)


async def _handle_chat_stream(
    chat_req: ChatRequest,
    request_id: Optional[str] = None,
) -> AsyncIterator[bytes]:
    """SSE：delta/tool 事件，最后 event=done 携带与 /chat 一致的业务 JSON。"""
    started_at = time.time()
    current_ctx = get_request_context()
    local_trace_tokens = None
    if request_id and not str(current_ctx.get("request_id") or "").strip():
        local_trace_tokens = begin_request_trace(request_id)
    sid = (chat_req.session_id and str(chat_req.session_id).strip()) or None
    ctx_token = update_request_context(
        request_id=request_id,
        intent=chat_req.intent,
        ticket_id=chat_req.works_info.ticket_id,
        session_id=sid or "",
    )
    try:
        _ensure_chat_agents()
        state = get_state()
        agent_sid = _scoped_session_id(sid, chat_req.intent)
        logger.info(
            "收到 /chat/stream intent=%s ticket_id=%s session_id=%s agent_session_id=%s",
            chat_req.intent,
            chat_req.works_info.ticket_id,
            sid or "",
            agent_sid or "",
        )

        if chat_req.intent == "summary":
            if state.agent_summary is None:
                yield _sse_bytes({"event": "error", "detail": "Summary Agent 初始化失败"})
                return
            if state.summary_runner is None:
                yield _sse_bytes({"event": "error", "detail": "Summary runner 未初始化"})
                return
            prompt = state.summary_runner.format_prompt(chat_req)
            agent = state.agent_summary
        elif chat_req.intent == "query":
            user_query = chat_req.query_info.query
            if not user_query:
                yield _sse_bytes({"event": "error", "detail": "query_info.query 不能为空"})
                return
            try:
                agent = _get_work_reply_agent(state)
            except HTTPException as ex:
                yield _sse_bytes({"event": "error", "detail": str(ex.detail)})
                return
            prompt = state.work_reply_runner.format_query_prompt(chat_req)
        elif chat_req.intent == "suggestion":
            try:
                agent = _get_work_reply_agent(state)
            except HTTPException as ex:
                yield _sse_bytes({"event": "error", "detail": str(ex.detail)})
                return
            prompt = state.work_reply_runner.format_prompt(chat_req)
        else:
            yield _sse_bytes(
                {
                    "event": "error",
                    "detail": f"不支持的 intent：{chat_req.intent!r}",
                }
            )
            return

        reset_knowledge_sources()
        async for part in agent_run_stream_collect(agent, prompt, session_id=agent_sid):
            k = part.get("kind")
            if k == "delta":
                yield _sse_bytes({"event": "delta", "text": part.get("text", "")})
            elif k == "tool":
                yield _sse_bytes({"event": "tool", "name": part.get("event", "")})
            elif k == "error":
                yield _sse_bytes({"event": "error", "detail": part.get("message", "unknown")})
                return
            elif k == "complete":
                ro = part["run_output"]
                kb = part["kb"]
                audit = part.get("audit") or {}
                raw = str(ro.content).strip()
                try:
                    if chat_req.intent == "summary":
                        inner = _parse_model_json(raw)["summary"]
                        if not isinstance(inner, dict):
                            raise ValueError("summary 须为对象")
                        summary = Summary(
                            info_summary=inner["info_summary"],
                            reviews=inner["reviews"],
                            summary_sources=kb,
                        )
                        _persist_chat_run_if_enabled(state, chat_req, summary=summary)
                        _log_chat_completion(
                            endpoint="/chat/stream",
                            intent=chat_req.intent,
                            total_ms=int((time.time() - started_at) * 1000),
                            kb_hit_docs=len(kb),
                            final_sources=len(kb),
                            fallback_used=False,
                            tool_audit=audit,
                        )
                        yield _sse_bytes({"event": "done", "data": {"summary": summary.model_dump()}})
                    elif chat_req.intent == "query":
                        qobj = _parse_model_json(raw)
                        answer = str(qobj.get("answer", "")).strip()
                        fallback_used = False
                        if not answer:
                            fallback_used = True
                            answer = "知识库中暂未找到与当前问题直接相关的依据，建议补充更具体的业务关键词后再查询。"
                        llm_sources = qobj.get("sources") or []
                        if not isinstance(llm_sources, list):
                            raise ValueError("sources 须为数组")
                        all_sources = merge_knowledge_source_names(kb, llm_sources)
                        qa = QueryAnswer(answer=answer, query_sources=all_sources)
                        _persist_chat_run_if_enabled(state, chat_req, query_answer=qa)
                        _log_chat_completion(
                            endpoint="/chat/stream",
                            intent=chat_req.intent,
                            total_ms=int((time.time() - started_at) * 1000),
                            kb_hit_docs=len(kb),
                            final_sources=len(all_sources),
                            fallback_used=fallback_used,
                            tool_audit=audit,
                        )
                        yield _sse_bytes({"event": "done", "data": qa.model_dump(by_alias=True)})
                    else:
                        suggestion_content = str(_parse_model_json(raw).get("suggestion", "")).strip()
                        fallback_used = False
                        if not suggestion_content:
                            fallback_used = True
                            suggestion_content = (
                                "知识库中暂未检索到相关信息，建议补充订单/问题细节后再核实处理。"
                            )
                        sugg = Suggestion(content=suggestion_content, suggestion_sources=kb)
                        _persist_chat_run_if_enabled(state, chat_req, suggestion=sugg)
                        _log_chat_completion(
                            endpoint="/chat/stream",
                            intent=chat_req.intent,
                            total_ms=int((time.time() - started_at) * 1000),
                            kb_hit_docs=len(kb),
                            final_sources=len(kb),
                            fallback_used=fallback_used,
                            tool_audit=audit,
                        )
                        yield _sse_bytes(
                            {"event": "done", "data": sugg.model_dump(by_alias=True)}
                        )
                except Exception as ex:
                    logger.exception("流式 /chat/stream 解析或持久化失败")
                    yield _sse_bytes({"event": "error", "detail": str(ex)})
                return

    except Exception as e:
        logger.exception("流式 /chat/stream 失败")
        yield _sse_bytes({"event": "error", "detail": str(e)})
    finally:
        reset_request_context(ctx_token)
        if local_trace_tokens is not None:
            end_request_trace(local_trace_tokens)


@api_router.post("/chat")
async def unified_chat(request: ChatRequest):
    try:
        return await _handle_chat(request)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "/chat 处理失败\n错误类型: %s\n错误信息: %s",
            type(e).__name__,
            e,
        )
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/chat/stream")
async def unified_chat_stream(request: ChatRequest):
    req_ctx = get_request_context()
    request_id = str(req_ctx.get("request_id") or "").strip() or None
    return StreamingResponse(
        _handle_chat_stream(request, request_id=request_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# 兼容旧 URL：API_BASE 已是 …/cs_assist_ai 时仍拼接 /work_reply_ai/chat（曾导致 404）
@api_router.post("/work_reply_ai/chat")
async def unified_chat_legacy_path(request: ChatRequest):
    return await unified_chat(request)


@api_router.post("/work_reply_ai/chat/stream")
async def unified_chat_stream_legacy_path(request: ChatRequest):
    return await unified_chat_stream(request)


@api_router.get("/health")
async def health_check():
    state = get_state()
    cr = state.config.get_chat_run_persistence_config()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "Work reply AI Backend",
        "rag_enabled": bool(state.rag_enabled),
        "mysql_engine_ready": bool(state.mysql_engine is not None),
        "chat_run_persistence_enabled": bool(cr.get("enabled")),
        "chat_run_mysql_ready": bool(cr.get("enabled") and state.mysql_engine is not None),
    }


@app.get("/")
async def root():
    return {
        "service": "Work reply AI Backend",
        "version": "1.0",
        "endpoints": {
            "chat": "/cs_assist_ai/chat",
            "chat_stream": "/cs_assist_ai/chat/stream",
            "health": "/cs_assist_ai/health",
            "chat_legacy": "/cs_assist_ai/work_reply_ai/chat",
            "chat_stream_legacy": "/cs_assist_ai/work_reply_ai/chat/stream",
        },
    }


app.include_router(api_router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    request_id = make_request_id()
    trace_tokens = begin_request_trace(request_id)
    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        stats = get_request_stats()
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "HTTP请求完成\n方法: %s\n路径: %s\n状态码: %s\n耗时: %.3fs\nLLM调用: %s\nEmbedding调用: %s\nTelemetry调用: %s",
            request.method,
            request.url.path,
            getattr(response, "status_code", ""),
            process_time,
            stats.get("llm_calls", 0),
            stats.get("embedding_calls", 0),
            stats.get("telemetry_calls", 0),
        )
        return response
    except Exception as e:
        process_time = time.time() - start_time
        logger.error(
            "请求异常\nID: %s\n路径: %s %s\n错误: %s: %s\n耗时: %.3fs",
            request_id,
            request.method,
            request.url.path,
            type(e).__name__,
            e,
            process_time,
        )
        logger.error(traceback.format_exc())
        raise
    finally:
        end_request_trace(trace_tokens)
