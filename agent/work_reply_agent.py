from typing import Optional, List, Any
from agno.agent import Agent


from agno.models.dashscope import DashScope
from config.config_loader import ConfigLoader
from prompt.work_reply_agent_prompt import WORK_REPLY_AGENT_INSTRUCTIONS
from tools.milvus_tool import create_milvus_tools
from tools.rag_retrieval_tool import KnowledgeRetrievalToolkit, create_knowledge_retrieval_toolkit

class WorkReplyAgent(Agent):
    def __init__(
        self,
        config_loader: Optional[ConfigLoader] = None,
        toolkit: Optional[KnowledgeRetrievalToolkit] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_id: Optional[str] = None,
        auto_init_tools: bool = True,
    ):
        # 1. 初始化配置加载器
        self.config_loader = config_loader or ConfigLoader()

        # 2. 获取 LLM 配置 (优先使用传入参数，否则查配置)
        llm_config = self.config_loader.get_llm_config()
        api_key = api_key or llm_config.get("api_key")
        base_url = base_url or llm_config.get("base_url")
        model_id = model_id or llm_config.get("model_name")

        # 3. 准备工具列表
        agent_tools: List[Any] = []
        if toolkit:
            # 如果外部传入了 toolkit (例如 TracedKnowledgeRetrievalToolkit)，直接使用
            agent_tools = [toolkit]
        elif auto_init_tools:
            # 否则尝试根据配置自动初始化默认的 KnowledgeRetrievalToolkit
            try:
                milvus_config = self.config_loader.get_milvus_config()
                embedder_config = self.config_loader.get_embedding_config()
                if milvus_config and embedder_config:
                    milvus_tool = create_milvus_tools(milvus_config, embedder_config)
                    default_toolkit = create_knowledge_retrieval_toolkit(
                        milvus_tool=milvus_tool,
                        enable_search=True,
                        enable_prefetch=True
                    )
                    agent_tools = [default_toolkit]
            except Exception as e:
                print(f"[WorkReplyAgent] Warning: Failed to auto-initialize tools from config: {e}")
    
                agent_tools = []

        # 4. 初始化父类 Agent
        super().__init__(
            model=DashScope(id=model_id, api_key=api_key, base_url=base_url),
            tools=agent_tools,
            instructions=WORK_REPLY_AGENT_INSTRUCTIONS,
        )


def create_work_reply_agent(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model_id: Optional[str] = None,
    toolkit: Optional[KnowledgeRetrievalToolkit] = None,
    auto_init_tools: bool = True,
) -> Agent:
    """
    创建 WorkReplyAgent 实例
    """
    # 这里我们创建一个新的 ConfigLoader 实例（如果没有传入）
    # 在 agent_service 中调用此函数时，toolkit 通常是已经初始化好的 TracedToolkit
    return WorkReplyAgent(
        toolkit=toolkit,
        api_key=api_key,
        base_url=base_url,
        model_id=model_id,
        auto_init_tools=auto_init_tools
    )

