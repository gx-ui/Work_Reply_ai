import os
from agno.models.dashscope import DashScope
from agno.os import AgentOS
from agno.team import Team
from agent.work_reply_agent import WorkReplyAgent
from agent.summary_agent import SummaryAgent
from config.config_loader import ConfigLoader



def build_agentos() -> AgentOS:
    """
    构建 AgentOS 实例。
    Team 作为统一入口，在建议 Agent 与摘要 Agent 间执行意图分发。
    """
    config_loader = ConfigLoader()
    llm_config = config_loader.get_llm_config()
    suggestion_agent = WorkReplyAgent(config_loader=config_loader)
    summary_agent = SummaryAgent(config_loader=config_loader)
    team_router = Team(
        name="work-reply-team-router",
        members=[suggestion_agent, summary_agent],
        model=DashScope(
            id=str(llm_config.get("model_name") or "qwen-plus"),
            api_key=str(llm_config.get("api_key") or ""),
            base_url=str(llm_config.get("base_url") or "").rstrip("/"),
        ),
        instructions=[
            "你是工单智能分发 Team 负责人。",
            "涉及总结、摘要、归纳时，将任务分配给 SummaryAgent。",
            "涉及回复建议、话术生成时，将任务分配给 WorkReplyAgent。",
            "输出最终结果时保持纯文本，不要添加代码块。",
        ],
        respond_directly=True,
        markdown=False,
    )
    return AgentOS(
        id="work-reply-ai-agentos",
        description="Work Reply AI AgentOS",
        agents=[suggestion_agent, summary_agent],
        teams=[team_router],
    )


agent_os = build_agentos()
app = agent_os.get_app()


if __name__ == "__main__":
    host = os.getenv("AGENTOS_HOST", "127.0.0.1")
    port = int(os.getenv("AGENTOS_PORT", "7777"))
    reload = os.getenv("AGENTOS_RELOAD", "").lower() in {"1", "true", "yes", "y", "on"}
    agent_os.serve(app=app, host=host, port=port, reload=reload)
