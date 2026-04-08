import logging
import logging.handlers
import contextvars
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "\uFE0F"
    "]+",
    flags=re.UNICODE,
)
_HTTPX_URL_RE = re.compile(r"HTTP Request:\s+[A-Z]+\s+(\S+)")

_REQUEST_CTX_FIELDS = ("request_id", "intent", "ticket_id", "session_id")
_request_context_cv: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "work_reply_ai_request_context",
    default={},
)
_request_stats_cv: contextvars.ContextVar[Dict[str, int] | None] = contextvars.ContextVar(
    "work_reply_ai_request_stats",
    default=None,
)
_tool_invocations_cv: contextvars.ContextVar[List[str] | None] = contextvars.ContextVar(
    "work_reply_ai_tool_invocations",
    default=None,
)
_trace_registry_lock = threading.RLock()
_trace_registry: Dict[str, Dict[str, Any]] = {}
_TRACE_TTL_SECONDS = 20 * 60
_TRACE_MAX_SIZE = 4096


def _blank_stats() -> Dict[str, int]:
    return {"http_calls": 0, "llm_calls": 0, "embedding_calls": 0, "telemetry_calls": 0}


def _sanitize_str(value: Any) -> str:
    return str(value or "").strip()


def _sanitize_request_id(value: Any) -> str:
    return _sanitize_str(value)


def _new_trace_state(
    request_id: str,
    *,
    intent: str = "",
    ticket_id: str = "",
    session_id: str = "",
) -> Dict[str, Any]:
    now_ts = time.time()
    return {
        "request_id": request_id,
        "intention": intent,
        "intent": intent,
        "ticket_id": ticket_id,
        "session_id": session_id,
        "http_calls": 0,
        "llm_calls": 0,
        "embedding_calls": 0,
        "telemetry_calls": 0,
        "tool_invocations": [],
        "started_at": now_ts,
        "ended_at": None,
        "closed": False,
        "last_seen": now_ts,
    }


def _cleanup_trace_registry_locked(now_ts: Optional[float] = None) -> None:
    now = now_ts if now_ts is not None else time.time()
    expired_ids = [
        rid
        for rid, state in _trace_registry.items()
        if (now - float(state.get("last_seen", 0.0) or 0.0)) > _TRACE_TTL_SECONDS
    ]
    for rid in expired_ids:
        _trace_registry.pop(rid, None)

    overflow = len(_trace_registry) - _TRACE_MAX_SIZE
    if overflow > 0:
        oldest = sorted(
            _trace_registry.items(),
            key=lambda kv: float(kv[1].get("last_seen", 0.0) or 0.0),
        )[:overflow]
        for rid, _state in oldest:
            _trace_registry.pop(rid, None)


def _set_trace_state(
    request_id: str,
    *,
    intent: str = "",
    ticket_id: str = "",
    session_id: str = "",
) -> None:
    rid = _sanitize_request_id(request_id)
    if not rid:
        return
    with _trace_registry_lock:
        _trace_registry[rid] = _new_trace_state(
            rid,
            intent=_sanitize_str(intent),
            ticket_id=_sanitize_str(ticket_id),
            session_id=_sanitize_str(session_id),
        )
        _cleanup_trace_registry_locked()


def _touch_trace_state(rid: str) -> None:
    with _trace_registry_lock:
        state = _trace_registry.get(rid)
        if state is None:
            return
        state["last_seen"] = time.time()


def _snapshot_trace_state(rid: str) -> Dict[str, Any]:
    with _trace_registry_lock:
        state = _trace_registry.get(rid)
        return dict(state or {})


def _update_trace_state(
    rid: str,
    *,
    intent: Optional[str] = None,
    ticket_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None:
    if not rid:
        return
    now_ts = time.time()
    with _trace_registry_lock:
        state = _trace_registry.get(rid)
        if state is None:
            state = _new_trace_state(rid)
            _trace_registry[rid] = state
        if intent is not None:
            val = _sanitize_str(intent)
            state["intent"] = val
            state["intention"] = val
        if ticket_id is not None:
            state["ticket_id"] = _sanitize_str(ticket_id)
        if session_id is not None:
            state["session_id"] = _sanitize_str(session_id)
        state["last_seen"] = now_ts


def _mark_trace_closed(rid: str) -> None:
    if not rid:
        return
    now_ts = time.time()
    with _trace_registry_lock:
        state = _trace_registry.get(rid)
        if state is not None:
            state["closed"] = True
            state["ended_at"] = now_ts
            state["last_seen"] = now_ts
        _cleanup_trace_registry_locked(now_ts=now_ts)


def _clear_tool_invocations_for_request(rid: str) -> None:
    if not rid:
        return
    with _trace_registry_lock:
        state = _trace_registry.get(rid)
        if state is None:
            return
        state["tool_invocations"] = []
        state["last_seen"] = time.time()


def _append_tool_invocation_for_request(rid: str, tool_name: str) -> None:
    if not rid:
        return
    name = _sanitize_str(tool_name)
    if not name:
        return
    with _trace_registry_lock:
        state = _trace_registry.get(rid)
        if state is None:
            state = _new_trace_state(rid)
            _trace_registry[rid] = state
        invocations = list(state.get("tool_invocations") or [])
        invocations.append(name)
        state["tool_invocations"] = invocations
        state["last_seen"] = time.time()


def _bump_http_stats_for_request(rid: str, *, url: str) -> None:
    if not rid:
        return
    with _trace_registry_lock:
        state = _trace_registry.get(rid)
        if state is None:
            state = _new_trace_state(rid)
            _trace_registry[rid] = state
        state["http_calls"] = int(state.get("http_calls", 0)) + 1
        if "/chat/completions" in url:
            state["llm_calls"] = int(state.get("llm_calls", 0)) + 1
        if "/embeddings" in url:
            state["embedding_calls"] = int(state.get("embedding_calls", 0)) + 1
        if "telemetry/runs" in url:
            state["telemetry_calls"] = int(state.get("telemetry_calls", 0)) + 1
        state["last_seen"] = time.time()


def _build_context_from_registry(ctx: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(ctx or {})
    rid = _sanitize_request_id(enriched.get("request_id"))
    if not rid:
        return enriched
    state = _snapshot_trace_state(rid)
    if not state:
        return enriched
    enriched["request_id"] = rid
    if not _sanitize_str(enriched.get("intent")):
        enriched["intent"] = _sanitize_str(state.get("intent"))
    if not _sanitize_str(enriched.get("ticket_id")):
        enriched["ticket_id"] = _sanitize_str(state.get("ticket_id"))
    if not _sanitize_str(enriched.get("session_id")):
        enriched["session_id"] = _sanitize_str(state.get("session_id"))
    return enriched


def _normalize_log_text(value: str) -> str:
    text = _ANSI_ESCAPE_RE.sub("", str(value or ""))
    text = _EMOJI_RE.sub("", text)
    return text


class PlainTextFormatter(logging.Formatter):
    """将日志格式化为纯文本，去除颜色控制符和 emoji。"""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return _normalize_log_text(rendered)


class TraceContextFilter(logging.Filter):
    """将请求追踪字段注入 LogRecord，并顺带累计请求内调用统计。"""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _build_context_from_registry(get_request_context())
        record.trace_context = _format_trace_context(ctx)
        _capture_http_stats(record, ctx)
        return True


def _build_log_file_path() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    log_dir = project_root / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "log.txt"


def _build_file_handler() -> logging.Handler:
    handler = logging.FileHandler(_build_log_file_path(), mode="a", encoding="utf-8")
    handler.addFilter(TraceContextFilter())
    handler.setFormatter(
        PlainTextFormatter(
            "%(asctime)s %(levelname)s %(name)s %(trace_context)s\n%(message)s\n" + "-" * 80
        )
    )
    return handler


def make_request_id() -> str:
    return f"req-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


def begin_request_trace(
    request_id: str,
    *,
    intent: str = "",
    ticket_id: str = "",
    session_id: str = "",
) -> Tuple[contextvars.Token, contextvars.Token, contextvars.Token]:
    """初始化当前请求上下文、统计计数、工具调用记录。"""
    rid = _sanitize_request_id(request_id)
    ctx = {
        "request_id": rid,
        "intent": _sanitize_str(intent),
        "ticket_id": _sanitize_str(ticket_id),
        "session_id": _sanitize_str(session_id),
    }
    ctx_token = _request_context_cv.set(ctx)
    stats_token = _request_stats_cv.set(_blank_stats())
    tool_token = _tool_invocations_cv.set([])
    if rid:
        _set_trace_state(
            rid,
            intent=ctx["intent"],
            ticket_id=ctx["ticket_id"],
            session_id=ctx["session_id"],
        )
    return ctx_token, stats_token, tool_token


def end_request_trace(tokens: Tuple[contextvars.Token, contextvars.Token, contextvars.Token]) -> None:
    rid = _sanitize_request_id(get_request_context().get("request_id"))
    if rid:
        _mark_trace_closed(rid)
    ctx_token, stats_token, tool_token = tokens
    _request_context_cv.reset(ctx_token)
    _request_stats_cv.reset(stats_token)
    _tool_invocations_cv.reset(tool_token)


def update_request_context(
    *,
    request_id: str | None = None,
    intent: str | None = None,
    ticket_id: str | None = None,
    session_id: str | None = None,
) -> contextvars.Token:
    current = _build_context_from_registry(get_request_context())
    incoming = {
        "request_id": request_id,
        "intent": intent,
        "ticket_id": ticket_id,
        "session_id": session_id,
    }
    for k, v in incoming.items():
        if v is None:
            continue
        current[k] = _sanitize_str(v)
    token = _request_context_cv.set(current)
    rid = _sanitize_request_id(current.get("request_id"))
    if rid:
        _update_trace_state(
            rid,
            intent=current.get("intent"),
            ticket_id=current.get("ticket_id"),
            session_id=current.get("session_id"),
        )
    return token


def reset_request_context(token: contextvars.Token) -> None:
    _request_context_cv.reset(token)


def get_request_context() -> Dict[str, Any]:
    ctx = dict(_request_context_cv.get() or {})
    return _build_context_from_registry(ctx)


def get_request_stats() -> Dict[str, int]:
    rid = _sanitize_request_id(get_request_context().get("request_id"))
    if rid:
        state = _snapshot_trace_state(rid)
        if state:
            return {
                "http_calls": int(state.get("http_calls", 0)),
                "llm_calls": int(state.get("llm_calls", 0)),
                "embedding_calls": int(state.get("embedding_calls", 0)),
                "telemetry_calls": int(state.get("telemetry_calls", 0)),
            }
    raw = _request_stats_cv.get()
    if not isinstance(raw, dict):
        return _blank_stats()
    return {
        "http_calls": int(raw.get("http_calls", 0)),
        "llm_calls": int(raw.get("llm_calls", 0)),
        "embedding_calls": int(raw.get("embedding_calls", 0)),
        "telemetry_calls": int(raw.get("telemetry_calls", 0)),
    }


def reset_tool_invocations() -> None:
    rid = _sanitize_request_id(get_request_context().get("request_id"))
    if rid:
        _clear_tool_invocations_for_request(rid)
    _tool_invocations_cv.set([])


def record_tool_invocation(tool_name: str) -> None:
    name = _sanitize_str(tool_name)
    if not name:
        return
    rid = _sanitize_request_id(get_request_context().get("request_id"))
    if rid:
        _append_tool_invocation_for_request(rid, name)
    current = list(_tool_invocations_cv.get() or [])
    current.append(name)
    _tool_invocations_cv.set(current)


def get_tool_invocations() -> List[str]:
    rid = _sanitize_request_id(get_request_context().get("request_id"))
    if rid:
        state = _snapshot_trace_state(rid)
        invocations = list(state.get("tool_invocations") or [])
        if invocations:
            return invocations
    return list(_tool_invocations_cv.get() or [])


def _format_trace_context(ctx: Dict[str, Any]) -> str:
    if not ctx:
        return "[rid=- intent=- ticket=- sid=-]"
    rid = _sanitize_str(ctx.get("request_id")) or "-"
    intent = _sanitize_str(ctx.get("intent")) or "-"
    ticket = _sanitize_str(ctx.get("ticket_id")) or "-"
    sid = _sanitize_str(ctx.get("session_id")) or "-"
    return f"[rid={rid} intent={intent} ticket={ticket} sid={sid}]"


def _capture_http_stats(record: logging.LogRecord, ctx: Optional[Dict[str, Any]] = None) -> None:
    if record.name != "httpx":
        return
    try:
        msg = record.getMessage()
    except Exception:
        return
    m = _HTTPX_URL_RE.search(msg)
    if not m:
        return
    url = m.group(1)
    trace_ctx = _build_context_from_registry(ctx if isinstance(ctx, dict) else get_request_context())
    rid = _sanitize_request_id(trace_ctx.get("request_id"))
    if rid:
        _bump_http_stats_for_request(rid, url=url)
        _touch_trace_state(rid)
        return

    stats = _request_stats_cv.get()
    if not isinstance(stats, dict):
        return
    next_stats = dict(stats)
    next_stats["http_calls"] = int(next_stats.get("http_calls", 0)) + 1
    if "/chat/completions" in url:
        next_stats["llm_calls"] = int(next_stats.get("llm_calls", 0)) + 1
    if "/embeddings" in url:
        next_stats["embedding_calls"] = int(next_stats.get("embedding_calls", 0)) + 1
    if "telemetry/runs" in url:
        next_stats["telemetry_calls"] = int(next_stats.get("telemetry_calls", 0)) + 1
    _request_stats_cv.set(next_stats)


def configure_logging() -> None:
    file_handler = _build_file_handler()
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler],
        force=True,
    )
    logging.captureWarnings(True)

    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
        "asyncio",
    ):
        target_logger = logging.getLogger(logger_name)
        target_logger.handlers.clear()
        target_logger.propagate = True
