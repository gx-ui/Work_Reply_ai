import logging
from typing import Optional, List, Any
from agno.agent import Agent
from agno.models.dashscope import DashScope
from prompt.summary_agent_prompt import SUMMARY_AGENT_INSTRUCTIONS

logger = logging.getLogger("summary_agent")


class SummaryAgent(Agent):
    """工单摘要 Agent：可选 Agno MySQLDb 会话持久化。"""

    def __init__(
        self,
        model_id: str,
        api_key: str,
        base_url: str,
        tools: Optional[List[Any]] = None,
        db: Optional[Any] = None,
        num_history_runs: int = 10,

    ):

        super().__init__(
            id="work-reply-ai-summary-agent",
            model=DashScope(id=model_id, api_key=api_key, base_url=base_url),
            tools=list(tools or []),
            instructions=SUMMARY_AGENT_INSTRUCTIONS,
            markdown=False,
            db=db,
            read_chat_history=False,
            add_history_to_context=True,
            num_history_runs=num_history_runs,
        )
