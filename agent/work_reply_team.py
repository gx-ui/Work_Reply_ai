"""
工单 Team 路由：路由指令、职责矩阵与 Team 构造集中于此。
Team 仅做意图分发，不传 db，不做 Team 级持久化。
"""
from __future__ import annotations
from typing import List
from agno.agent import Agent
from agno.models.dashscope import DashScope
from agno.team import Team


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

RESPONSIBILITY_MATRIX = {
    "summary_agent": ["总结", "提炼", "归档"],
    "work_reply_agent": ["业务回复", "上下文衔接", "用户交互"],
}


def build_work_reply_team_router(
    suggestion_agent: Agent,
    summary_agent: Agent,
    *,
    model_id: str,
    api_key: str,
    base_url: str,
) -> Team:
    """意图分发 Team：成员为建议 Agent 与摘要 Agent。"""
    return Team(
        name="work-reply-team-router",
        members=[suggestion_agent, summary_agent],
        model=DashScope(id=model_id, api_key=api_key, base_url=base_url),
        instructions=TEAM_ROUTER_INSTRUCTIONS,
        markdown=False,
        respond_directly=True,
    )
