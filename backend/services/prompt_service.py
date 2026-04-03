from typing import List, Optional, Any
import logging
from backend.model.request_entity import ChatRequest
from utils.common import redact_sensitive

from prompt.summary_agent_prompt import SUMMARY_PROMPT_TEMPLATE
from prompt.work_reply_agent_prompt import WORK_REPLY_PROMPT_TEMPLATE

logger = logging.getLogger("prompt_service")


def _clip_text(value: str, max_len: int) -> str:
    s = str(value or "")
    if len(s) <= max_len:
        return s
    return s[:max_len]

def _format_history_items(items: List[Any], max_items: int = 5) -> List[str]:
    out: List[str] = []
    for item in items or []:
        summary = ""
        if isinstance(item, dict):
            summary = str(item.get("summary") or "").strip()
            if not summary:
                summary = str(item.get("content") or "").strip()
        else:
            summary = str(item or "").strip()
        summary = _clip_text(summary, 240).strip()
        if not summary:
            continue
        out.append(summary)
        if len(out) >= max_items:
            break
    return out


def build_agent_input(request: ChatRequest, works_tags: Optional[List[str]]) -> str:
    works = request.works_info
    core = request.core_info
    attention = request.attention_info

    # 合并并去重标签
    all_tags: List[str] = []
    for source in (works_tags or []):
        v = str(source or "").strip()
        if v:
            all_tags.append(v)
    for source in (works.tags or []):
        v = str(source or "").strip()
        if v:
            all_tags.append(v)
    seen = set()
    dedup_tags: List[str] = []
    for t in all_tags:
        if t in seen:
            continue
        seen.add(t)
        dedup_tags.append(t)

    # 各字段独立处理，直接对应 TEMPLATE 占位符
    title_text    = redact_sensitive(_clip_text(works.title.strip(), 300))   or "（无）"
    desc_text     = redact_sensitive(_clip_text(works.desc.strip(), 2000))   or "（无）"
    priority_text = redact_sensitive(_clip_text(str(works.priority or ""), 80))  or "（无）"
    status_text   = redact_sensitive(_clip_text(str(works.status or ""), 80))    or "（无）"
    tags_text     = redact_sensitive("、".join(dedup_tags[:20]))             or "（无）"

    customer_name_text    = redact_sensitive(_clip_text(core.customer_name or "", 120))          or "（无）"
    project_name_text     = redact_sensitive(_clip_text(core.project_name or "", 120))           or "（无）"
    mall_name_text        = redact_sensitive(_clip_text(core.mall_name or "", 120))              or "（无）"

    project_attention_text  = redact_sensitive(_clip_text(attention.project_attention or "", 500))  or "（无）"
    supplier_attention_text = redact_sensitive(_clip_text(attention.supplier_attention or "", 500)) or "（无）"

    history_lines = _format_history_items(list(works.history or []), max_items=5)
    history_text  = "\n".join([f"{i}. {redact_sensitive(line)}" for i, line in enumerate(history_lines, 1)]) if history_lines else "（无）"

    final_prompt = WORK_REPLY_PROMPT_TEMPLATE.format(
        title=title_text,
        desc=desc_text,
        priority=priority_text,
        status=status_text,
        tags=tags_text,
        customer_name=customer_name_text,
        project_name=project_name_text,
        mall_name=mall_name_text,
        project_attention=project_attention_text,
        supplier_attention=supplier_attention_text,
        history=history_text,
    )
    return final_prompt


def build_summary_input(request: ChatRequest) -> str:
    ticket = request.works_info
    core = request.core_info
    attention = request.attention_info

    title_text    = redact_sensitive(_clip_text(ticket.title or "", 300))   or "（无）"
    desc_text     = redact_sensitive(_clip_text(ticket.desc or "", 2500))   or "（无）"
    priority_text = redact_sensitive(_clip_text(ticket.priority or "", 80)) or "（无）"
    status_text   = redact_sensitive(_clip_text(ticket.status or "", 80))   or "（无）"

    customer_name_text = redact_sensitive(_clip_text(core.customer_name or "", 120)) or "（无）" if core else "（无）"
    project_name_text  = redact_sensitive(_clip_text(core.project_name or "", 120))  or "（无）" if core else "（无）"
    mall_name_text     = redact_sensitive(_clip_text(core.mall_name or "", 120))      or "（无）" if core else "（无）"

    project_attention_text  = redact_sensitive(_clip_text(attention.project_attention or "", 500))  or "（无）" if attention else "（无）"
    supplier_attention_text = redact_sensitive(_clip_text(attention.supplier_attention or "", 500)) or "（无）" if attention else "（无）"

    if ticket.tags:
        tags_list = [redact_sensitive(_clip_text(str(t), 40)) for t in ticket.tags[:20] if str(t or "").strip()]
        tags_text = "、".join(tags_list) or "（无）"
    else:
        tags_text = "（无）"

    history_lines: List[str] = []
    for item in (ticket.history or [])[:10]:
        if isinstance(item, dict):
            line = str(item.get("summary") or item.get("content") or "").strip()
        else:
            line = str(item or "").strip()
        line = redact_sensitive(_clip_text(line, 240))
        if line:
            history_lines.append(line)
    history_text = "\n".join([f"{i}. {line}" for i, line in enumerate(history_lines, 1)]) if history_lines else "（无）"

    return SUMMARY_PROMPT_TEMPLATE.format(
        title=title_text,
        desc=desc_text,
        priority=priority_text,
        status=status_text,
        tags=tags_text,
        customer_name=customer_name_text,
        project_name=project_name_text,
        mall_name=mall_name_text,
        project_attention=project_attention_text,
        supplier_attention=supplier_attention_text,
        history_items=history_text,
    )
