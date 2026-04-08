import logging
import logging.handlers
import contextvars
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple


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
        ctx = get_request_context()
        record.trace_context = _format_trace_context(ctx)
        _capture_http_stats(record)
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
    ctx = {
        "request_id": str(request_id or "").strip(),
        "intent": str(intent or "").strip(),
        "ticket_id": str(ticket_id or "").strip(),
        "session_id": str(session_id or "").strip(),
    }
    ctx_token = _request_context_cv.set(ctx)
    stats_token = _request_stats_cv.set(
        {"http_calls": 0, "llm_calls": 0, "embedding_calls": 0, "telemetry_calls": 0}
    )
    tool_token = _tool_invocations_cv.set([])
    return ctx_token, stats_token, tool_token


def end_request_trace(tokens: Tuple[contextvars.Token, contextvars.Token, contextvars.Token]) -> None:
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
    current = get_request_context()
    incoming = {
        "request_id": request_id,
        "intent": intent,
        "ticket_id": ticket_id,
        "session_id": session_id,
    }
    for k, v in incoming.items():
        if v is None:
            continue
        current[k] = str(v).strip()
    return _request_context_cv.set(current)


def reset_request_context(token: contextvars.Token) -> None:
    _request_context_cv.reset(token)


def get_request_context() -> Dict[str, Any]:
    return dict(_request_context_cv.get() or {})


def get_request_stats() -> Dict[str, int]:
    raw = _request_stats_cv.get()
    if not isinstance(raw, dict):
        return {"http_calls": 0, "llm_calls": 0, "embedding_calls": 0, "telemetry_calls": 0}
    return {
        "http_calls": int(raw.get("http_calls", 0)),
        "llm_calls": int(raw.get("llm_calls", 0)),
        "embedding_calls": int(raw.get("embedding_calls", 0)),
        "telemetry_calls": int(raw.get("telemetry_calls", 0)),
    }


def reset_tool_invocations() -> None:
    _tool_invocations_cv.set([])


def record_tool_invocation(tool_name: str) -> None:
    name = str(tool_name or "").strip()
    if not name:
        return
    current = list(_tool_invocations_cv.get() or [])
    current.append(name)
    _tool_invocations_cv.set(current)


def get_tool_invocations() -> List[str]:
    return list(_tool_invocations_cv.get() or [])


def _format_trace_context(ctx: Dict[str, Any]) -> str:
    if not ctx:
        return "[rid=- intent=- ticket=- sid=-]"
    rid = str(ctx.get("request_id") or "-")
    intent = str(ctx.get("intent") or "-")
    ticket = str(ctx.get("ticket_id") or "-")
    sid = str(ctx.get("session_id") or "-")
    return f"[rid={rid} intent={intent} ticket={ticket} sid={sid}]"


def _capture_http_stats(record: logging.LogRecord) -> None:
    if record.name != "httpx":
        return
    stats = _request_stats_cv.get()
    if not isinstance(stats, dict):
        return
    try:
        msg = record.getMessage()
    except Exception:
        return
    m = _HTTPX_URL_RE.search(msg)
    if not m:
        return
    url = m.group(1)
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
