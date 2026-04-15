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
from prompt.summary_agent_prompt import (
    SUMMARY_AGENT_INSTRUCTIONS,
    SUMMARY_PROMPT_TEMPLATE,
)


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
        instructions: Optional[str] = None,
    ) -> Agent:
        return Agent(
            id="work-reply-ai-summary-agent",
            description="你是一个生成工单总结以及注意事项的专业助手",
            model=DashScope(
                id=self._model_id,
                api_key=self._api_key,
                base_url=self._base_url,
            ),
            tools=list(tools or []),
            instructions=str(instructions or SUMMARY_AGENT_INSTRUCTIONS),
            markdown=False,
            db=db,
            debug_mode=True,
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

    def _post_process_reviews(
        self,
        llm_generated_reviews: str,
        request: ChatRequest,
    ) -> str:
        """
        reviews 后处理：知识库检索失败时使用 project_attention

        优先级：
        1. LLM 知识库检索结果（如果有）
        2. project_attention（项目专属注意事项）
        3. "无"

        Args:
            llm_generated_reviews: LLM 生成的 reviews 内容
            request: 原始请求对象，用于获取 project_attention

        Returns:
            str: 处理后的 reviews 内容
        """
        import logging
        logger = logging.getLogger("summary_agent")

        project_attention = str(request.attention_info.project_attention or "").strip()
        reviews = str(llm_generated_reviews or "").strip()

        # 检查 LLM 是否检索到有效结果
        # "未找到相关结果" 或 "无" 表示检索失败
        retrieval_failed = (
            not reviews or
            reviews == "无" or
            reviews == "待确认" or
            "未找到相关结果" in reviews
        )

        if retrieval_failed:
            logger.warning("[Reviews后处理] 知识库检索失败 → 使用 project_attention")

            if project_attention:
                logger.info("[Reviews后处理] 使用 project_attention（长度: %d 字符）", len(project_attention))
                return project_attention

            logger.info("[Reviews后处理] project_attention 为空 → 返回'无'")
            return "无"

        # LLM 检索成功，使用检索结果
        logger.info("[Reviews后处理] 使用知识库检索结果（长度: %d 字符）", len(reviews))
        return reviews
