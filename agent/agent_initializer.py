from typing import Any, Dict, List, Optional

from agno.agent import Agent
from agno.models.dashscope import DashScope
from agno.os import AgentOS
from agno.team import Team

from agent.summary_agent import create_summary_agent
from agent.work_reply_agent import create_work_reply_agent


class AgentInitializer:
    RESPONSIBILITY_MATRIX: Dict[str, List[str]] = {
        "summary_agent": ["总结", "提炼", "归档"],
        "work_reply_agent": ["业务回复", "上下文衔接", "用户交互"],
    }

    TEAM_ROUTER_INSTRUCTIONS: List[str] = [
        "你是工单处理 Team 的路由负责人，只做任务分发，不自行编造业务结论。",
        "输入是统一请求 JSON，包含 intent、works_info、core_info、attention_info。",
        "当 intent=summary 时，必须转给摘要 Agent。",
        "当 intent=suggestion 时，必须转给建议 Agent。",
        "当 intent=auto 时，结合工单内容做意图识别并分发。",
        "只有 intent=auto 时才允许依据关键词识别意图。",
        "intent 非 auto 时禁止根据关键词改写路由结论。",
        "当 intent=auto 且用户请求包含：总结、摘要、归纳、复盘、summary，转给摘要 Agent。",
        "当 intent=auto 且用户请求包含：回复、建议、话术、怎么回、suggestion，转给建议 Agent。",
        "如果意图不明确，优先转给建议 Agent。",
        "输出必须是 JSON 字符串，不要输出 markdown。",
    ]

    def __init__(self, model_id: str, api_key: str, base_url: str):
        self.model_id = model_id
        self.api_key = api_key
        self.base_url = base_url

    def create_summary_agent(self, toolkits: Optional[List[Any]] = None) -> Agent:
        return create_summary_agent(
            api_key=self.api_key,
            base_url=self.base_url,
            model_id=self.model_id,
            toolkits=toolkits,
        )

    def create_work_reply_agent(self, toolkit: Any = None, enable_tools: bool = True) -> Agent:
        return create_work_reply_agent(
            api_key=self.api_key,
            base_url=self.base_url,
            model_id=self.model_id,
            toolkit=toolkit,
            auto_init_tools=enable_tools,
        )

    def create_team_router(self, suggestion_agent: Agent, summary_agent: Agent) -> Team:
        return Team(
            name="work-reply-team-router",
            members=[suggestion_agent, summary_agent],
            model=DashScope(id=self.model_id, api_key=self.api_key, base_url=self.base_url),
            instructions=self.TEAM_ROUTER_INSTRUCTIONS,
            markdown=False,
            respond_directly=True,
        )

    def create_agentos(self, suggestion_agent: Agent, summary_agent: Agent, team_router: Team) -> AgentOS:
        return AgentOS(
            id="work-reply-ai-agentos-runtime",
            description="Work Reply AI AgentOS Runtime",
            agents=[suggestion_agent, summary_agent],
            teams=[team_router],
        )
