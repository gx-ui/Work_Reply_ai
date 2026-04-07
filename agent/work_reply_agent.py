"""
统一的工单助手 runner。

- `_build_agent()` 负责构建带或不带知识库工具的 Agno Agent
- `format_prompt()` 用于 suggestion
- `format_query_prompt()` 用于 query
"""
from __future__ import annotations

from typing import Any, List, Optional

from agno.agent import Agent
from agno.models.dashscope import DashScope

from config.config_loader import ConfigLoader
from entity.request import ChatRequest
from prompt.query_agent_prompt import QUERY_PROMPT_TEMPLATE
from prompt.work_reply_agent_prompt import (
    WORK_REPLY_AGENT_INSTRUCTIONS,
    WORK_REPLY_PROMPT_TEMPLATE,
)


class WorkReplyAgent:
    def __init__(self, config_loader: Optional[ConfigLoader] = None) -> None:
        cfg = config_loader or ConfigLoader()
        llm = cfg.get_llm_config()
        self._model_id = str(llm.get("model_name") or "qwen3.5-flash")
        self._api_key = str(llm.get("api_key") or "")
        self._base_url = str(llm.get("base_url") or "").rstrip("/")

    def _build_agent(
        self,
        tools: Optional[List[Any]] = None,
        db: Optional[Any] = None,
    ) -> Agent:
        return Agent(
            id="work-reply-ai-agent",
            model=DashScope(
                id=self._model_id,
                api_key=self._api_key,
                base_url=self._base_url,
            ),
            tools=list(tools or []),
            instructions=WORK_REPLY_AGENT_INSTRUCTIONS,
            db=db,
        )

    @staticmethod
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
            if not summary:
                continue
            out.append(summary)
            if len(out) >= max_items:
                break
        return out

    def _build_prompt_context(self, request: ChatRequest) -> dict:
        works = request.works_info
        core = request.core_info
        attention = request.attention_info

        history_lines = self._format_history_items(list(works.history or []), max_items=5)
        history_text = "\n".join(
            f"{i}. {line}" for i, line in enumerate(history_lines, 1)
        ) if history_lines else "（无）"

        return {
            "title": str(works.title or ""),
            "desc": str(works.desc or ""),
            "customer_name": str(core.customer_name or ""),
            "project_name": str(core.project_name or ""),
            "mall_name": str(core.mall_name or ""),
            "project_attention": str(attention.project_attention or ""),
            "supplier_attention": str(attention.supplier_attention or ""),
            "history": history_text,
        }

    def format_prompt(self, request: ChatRequest) -> str:
        return WORK_REPLY_PROMPT_TEMPLATE.format(**self._build_prompt_context(request))

    def format_query_prompt(self, request: ChatRequest) -> str:
        payload = self._build_prompt_context(request)
        payload["user_query"] = str(request.query_info.query or "")
        return QUERY_PROMPT_TEMPLATE.format(**payload)
