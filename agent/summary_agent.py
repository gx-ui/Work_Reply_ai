"""Summary agent builder and prompt formatters."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from agno.agent import Agent
from agno.models.dashscope import DashScope

from config.config_loader import ConfigLoader
from entity.request import ChatRequest
from prompt.summary_agent_prompt import (
    SUMMARY_INFO_AGENT_INSTRUCTIONS,
    SUMMARY_INFO_PROMPT_TEMPLATE,
    SUMMARY_REVIEWS_AGENT_INSTRUCTIONS,
    SUMMARY_REVIEWS_PROMPT_TEMPLATE,
)


class SummaryAgent:
    """Build summary agents and format prompts for summary stages."""

    def __init__(self, config_loader: Optional[ConfigLoader] = None) -> None:
        cfg = config_loader or ConfigLoader()
        llm = cfg.get_llm_config()
        main_model = str(llm.get("model_name") or "qwen3.5-flash")
        self._model_id = str(llm.get("summary_model") or main_model)
        self._api_key = str(llm.get("api_key") or "")
        self._base_url = str(llm.get("base_url") or "")

    def _build_agent(
        self,
        tools: Optional[List[Any]] = None,
        db: Optional[Any] = None,
        instructions: Optional[str] = None,
        agent_id: str = "work-reply-ai-summary-agent",
    ) -> Agent:
        return Agent(
            id=agent_id,
            model=DashScope(
                id=self._model_id,
                api_key=self._api_key,
                base_url=self._base_url,
            ),
            tools=list(tools or []),
            instructions=instructions or SUMMARY_INFO_AGENT_INSTRUCTIONS,
            markdown=False,
            db=db,
        )

    @staticmethod
    def _build_prompt_payload(request: ChatRequest) -> Dict[str, str]:
        ticket = request.works_info
        core = request.core_info
        attention = request.attention_info

        history_lines: List[str] = []
        for item in (ticket.history or [])[:10]:
            if isinstance(item, dict):
                line = str(item.get("summary") or item.get("content") or "")
            else:
                line = str(item)
            line = line.strip()
            if line:
                history_lines.append(line)

        history_text = "\n".join(f"{i}. {line}" for i, line in enumerate(history_lines, 1))

        return {
            "title": str(ticket.title or ""),
            "desc": str(ticket.desc or ""),
            "status": str(ticket.status or ""),
            "priority": str(ticket.priority or ""),
            "customer_name": str(core.customer_name or ""),
            "project_name": str(core.project_name or ""),
            "mall_name": str(core.mall_name or ""),
            "project_attention": str(attention.project_attention or ""),
            "supplier_attention": str(attention.supplier_attention or ""),
            "history_items": history_text,
        }

    def format_reviews_prompt(self, request: ChatRequest) -> str:
        payload = self._build_prompt_payload(request)
        return SUMMARY_REVIEWS_PROMPT_TEMPLATE.format(**payload)

    def format_info_summary_prompt(self, request: ChatRequest) -> str:
        payload = self._build_prompt_payload(request)
        return SUMMARY_INFO_PROMPT_TEMPLATE.format(**payload)

    # Backward-compatible alias if any caller still uses old method name.
    def format_prompt(self, request: ChatRequest) -> str:
        return self.format_info_summary_prompt(request)

    @staticmethod
    def reviews_instructions() -> str:
        return SUMMARY_REVIEWS_AGENT_INSTRUCTIONS

    @staticmethod
    def info_instructions() -> str:
        return SUMMARY_INFO_AGENT_INSTRUCTIONS
