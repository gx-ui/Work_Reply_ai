"""
独立 AgentOS 启动入口（仅用于调试/独立部署）。
生产环境请使用 backend/app.py 中通过 agent_service.py 初始化的 AgentOS。
"""
import os
from agno.os import AgentOS
from agent.work_reply_agent import WorkReplyAgent
from agent.summary_agent import SummaryAgent
from agent.work_reply_team import build_work_reply_team_router
from config.config_loader import ConfigLoader
from db.mysql_store import init_mysql_for_agents_from_config


def build_agentos() -> AgentOS:
    config_loader = ConfigLoader()
    llm_config = config_loader.get_llm_config()
    persist = config_loader.get_session_persistence_config()

    model_id = str(llm_config.get("model_name") or "qwen-plus")
    summary_model = str(llm_config.get("summary_model") or model_id)
    api_key = str(llm_config.get("api_key") or "")
    base_url = str(llm_config.get("base_url") or "").rstrip("/")
    num_history_runs = int(persist.get("num_history_runs", 10))

    _engine, db_work, db_summary = init_mysql_for_agents_from_config(config_loader)
    wr_kw = {"db": db_work, "num_history_runs": num_history_runs} if db_work else {}
    sum_kw = {"db": db_summary, "num_history_runs": num_history_runs} if db_summary else {}

    suggestion_agent = WorkReplyAgent(
        model_id=model_id,
        api_key=api_key,
        base_url=base_url,
        **wr_kw,
    )
    summary_agent = SummaryAgent(
        model_id=summary_model,
        api_key=api_key,
        base_url=base_url,
        **sum_kw,
    )
    team_router = build_work_reply_team_router(
        suggestion_agent,
        summary_agent,
        model_id=model_id,
        api_key=api_key,
        base_url=base_url,
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
