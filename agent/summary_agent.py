"""
工单摘要：配置在 __init__ 加载，_build_agent 返回 agno Agent 实例；
提示词拼装见 format_prompt。
"""
from __future__ import annotations

from typing import Any, List, Optional

from agno.agent import Agent
from agno.models.dashscope import DashScope

from config.config_loader import ConfigLoader
from entity.request import ChatRequest
from prompt.summary_agent_prompt import SUMMARY_AGENT_INSTRUCTIONS, SUMMARY_PROMPT_TEMPLATE


class SummaryAgent:
    """工单摘要封装。"""

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
    ) -> Agent:
        return Agent(
            id="work-reply-ai-summary-agent",
            model=DashScope(
                id=self._model_id,
                api_key=self._api_key,
                base_url=self._base_url,
            ),
            tools=list(tools or []),
            instructions=SUMMARY_AGENT_INSTRUCTIONS,
            markdown=False,
            db=db,
        )

    def format_prompt(self, request: ChatRequest) -> str:
        """拼装摘要任务用户提示。"""
        ticket = request.works_info
        core = request.core_info
        attention = request.attention_info

        title_text = str(ticket.title or "")
        desc_text = str(ticket.desc or "")
        status_text = str(ticket.status or "")
        priority_text = str(ticket.priority or "")

        customer_name_text = str(core.customer_name or "")
        project_name_text = str(core.project_name or "")
        mall_name_text = str(core.mall_name or "")

        project_attention_text = str(attention.project_attention or "")
        supplier_attention_text = str(attention.supplier_attention or "")

        history_lines: List[str] = []
        for item in (ticket.history or [])[:10]:
            if isinstance(item, dict):
                line = str(item.get("summary") or item.get("content") or "")
            else:
                line = str(item)
            if line:
                history_lines.append(line)
        history_text = "\n".join(f"{i}. {line}" for i, line in enumerate(history_lines, 1))

        return SUMMARY_PROMPT_TEMPLATE.format(
            title=title_text,
            desc=desc_text,
            status=status_text,
            priority=priority_text,
            customer_name=customer_name_text,
            project_name=project_name_text,
            mall_name=mall_name_text,
            project_attention=project_attention_text,
            supplier_attention=supplier_attention_text,
            history_items=history_text,
        )
