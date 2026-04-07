"""
单次 /chat 业务数据落库：works_info + core_info / attention_info / query_info + rely_info，与 Agno 会话表分离。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from entity.request import ChatRequest
from entity.response import QueryAnswer, Suggestion, Summary

logger = logging.getLogger("chat_run_store")


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def build_chat_run_payload(
    chat_req: ChatRequest,
    *,
    suggestion: Optional[Suggestion] = None,
    summary: Optional[Summary] = None,
    query_answer: Optional[QueryAnswer] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    返回 (core_info, attention_info, query_info, rely_info)，前三项按 intent 可能为 None；
    rely_info 始终为本次回复结构（含内容/依据字段）。
    """
    intent = chat_req.intent
    if intent == "suggestion":
        if suggestion is None:
            raise ValueError("intent=suggestion 时需要 suggestion")
        return None, None, None, suggestion.model_dump(mode="json")
    if intent == "summary":
        if summary is None:
            raise ValueError("intent=summary 时需要 summary")
        return (
            chat_req.core_info.model_dump(mode="json"),
            chat_req.attention_info.model_dump(mode="json"),
            None,
            summary.model_dump(mode="json"),
        )
    if intent == "query":
        if query_answer is None:
            raise ValueError("intent=query 时需要 query_answer")
        return (
            None,
            None,
            chat_req.query_info.model_dump(mode="json"),
            query_answer.model_dump(mode="json"),
        )
    raise ValueError(f"不支持的 intent: {intent!r}")


def insert_chat_run(
    engine: Engine,
    table: str,
    chat_req: ChatRequest,
    payload: Tuple[
        Optional[Dict[str, Any]],
        Optional[Dict[str, Any]],
        Optional[Dict[str, Any]],
        Dict[str, Any],
    ],
) -> None:
    """插入一行 chat run（created_at / updated_at 由表默认值维护）。"""
    core, attention, query_info, rely_info = payload
    works = chat_req.works_info.model_dump(mode="json")
    ticket_id = str(chat_req.works_info.ticket_id or "").strip()
    sid = (chat_req.session_id and str(chat_req.session_id).strip()) or None
    intent = chat_req.intent

    safe_table = "".join(c for c in table if c.isalnum() or c == "_")
    if not safe_table or safe_table != table:
        raise ValueError("非法的 chat_run 表名")

    columns = [
        "intent",
        "session_id",
        "ticket_id",
        "works_info",
        "core_info",
        "attention_info",
        "query_info",
        "rely_info",
    ]
    value_parts: list[str] = [
        ":intent",
        ":session_id",
        ":ticket_id",
        "CAST(:works_info AS JSON)",
    ]
    params: Dict[str, Any] = {
        "intent": intent,
        "session_id": sid,
        "ticket_id": ticket_id[:512],
        "works_info": _json_dumps(works),
    }

    for col, val in (
        ("core_info", core),
        ("attention_info", attention),
        ("query_info", query_info),
        ("rely_info", rely_info),
    ):
        if val is None:
            value_parts.append("NULL")
        else:
            value_parts.append(f"CAST(:{col} AS JSON)")
            params[col] = _json_dumps(val)

    cols_sql = ", ".join(f"`{c}`" for c in columns)
    vals_sql = ", ".join(value_parts)
    sql = text(f"INSERT INTO `{safe_table}` ({cols_sql}) VALUES ({vals_sql})")

    with engine.begin() as conn:
        conn.execute(sql, params)


def persist_chat_run_safe(
    engine: Optional[Engine],
    table: str,
    chat_req: ChatRequest,
    *,
    suggestion: Optional[Suggestion] = None,
    summary: Optional[Summary] = None,
    query_answer: Optional[QueryAnswer] = None,
) -> None:
    """写入失败仅打日志，不影响接口。"""
    if engine is None:
        return
    try:
        payload = build_chat_run_payload(
            chat_req,
            suggestion=suggestion,
            summary=summary,
            query_answer=query_answer,
        )
        insert_chat_run(engine, table, chat_req, payload)
    except Exception:
        logger.exception(
            "chat_run 持久化失败 intent=%s ticket_id=%s",
            chat_req.intent,
            getattr(chat_req.works_info, "ticket_id", ""),
        )
