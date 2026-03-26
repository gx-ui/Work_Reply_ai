from typing import Optional, List, Any
from agno.agent import Agent
from agno.models.dashscope import DashScope
from config.config_loader import ConfigLoader
from prompt.summary_agent_prompt import SUMMARY_AGENT_INSTRUCTIONS


class SummaryAgent(Agent):
    def __init__(
        self,
        config_loader: Optional[ConfigLoader] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_id: Optional[str] = None,
        toolkits: Optional[List[Any]] = None,
    ):
        self.config_loader = config_loader or ConfigLoader()
        llm_config = self.config_loader.get_llm_config()
        api_key = api_key or llm_config.get("api_key")
        base_url = base_url or llm_config.get("base_url")
        # 优先使用专属 summary_model，回退到主模型
        model_id = model_id or llm_config.get("summary_model") or llm_config.get("model_name")

        super().__init__(
            model=DashScope(id=model_id, api_key=api_key, base_url=base_url),
            tools=list(toolkits or []),
            instructions=SUMMARY_AGENT_INSTRUCTIONS,
            markdown=False,
        )


def create_summary_agent(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model_id: Optional[str] = None,
    toolkits: Optional[List[Any]] = None,
) -> Agent:
    return SummaryAgent(
        api_key=api_key,
        base_url=base_url,
        model_id=model_id,
        toolkits=toolkits,
    )
