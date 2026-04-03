import logging
from typing import Optional, List, Any
from agno.agent import Agent
from agno.models.dashscope import DashScope
from prompt.work_reply_agent_prompt import WORK_REPLY_AGENT_INSTRUCTIONS

logger = logging.getLogger("work_reply_agent")


class WorkReplyAgent(Agent):
    """工单回复建议 Agent"""

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
            id="work-reply-ai-suggestion-agent",
            model=DashScope(id=model_id, api_key=api_key, base_url=base_url),
            tools=list(tools or []),
            instructions=WORK_REPLY_AGENT_INSTRUCTIONS,
            db=db,
            read_chat_history=False,
            add_history_to_context=True,
            num_history_runs=num_history_runs,
        )
