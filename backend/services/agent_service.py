import time
import json
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Sequence, Union
from threading import RLock
from contextvars import ContextVar, copy_context

from agno.agent import Agent
from agno.os import AgentOS
from agno.team import Team

from config.config_loader import ConfigLoader
from tools.rag_retrieval_tool import KnowledgeRetrievalToolkit
from tools.summary_rag_tools import create_summary_rag_toolkits
from agent.agent_initializer import AgentInitializer
from utils.milvus_utils import  clip_text
from utils.common import redact_sensitive
import asyncio
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("agent_service")
logger.propagate = False
_STATE = ContextVar("agent_state", default={})
_KNOWLEDGE_SOURCES = ContextVar("knowledge_sources", default=[])
_STATE_LOCK = RLock()

def reset_knowledge_sources() -> None:
    _KNOWLEDGE_SOURCES.set([]) 

def append_knowledge_sources(items: Sequence[str]) -> None:
    cur = _KNOWLEDGE_SOURCES.get()
    if cur is None:
        cur = []
        _KNOWLEDGE_SOURCES.set(cur)
    seen = set(cur)
    for it in items or []:
        v = str(it or "")
        v = v.replace("\ufeff", "")
        v = v.replace("\u200b", "").replace("\u200e", "").replace("\u200f", "")
        v = v.replace("\r", "").replace("\n", "").strip()
        if not v:
            continue
        if v in seen:
            continue
        seen.add(v)
        cur.append(v)

def get_knowledge_sources() -> List[str]:
    return list(_KNOWLEDGE_SOURCES.get() or [])

class TracedKnowledgeRetrievalToolkit(KnowledgeRetrievalToolkit):
    def search_knowledge_base(
        self,
        query: str,
        limit: Optional[int] = 5,
        tags: Optional[Union[str, List[str]]] = None,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> str:
        t0 = time.time()
        raw_result = self.retrieval_tool.search(
            query=query,
            limit=limit,
            file_name_filters=file_name_filters
        )
        
        file_names = []
        result_str = ""
        
        if isinstance(raw_result, list):
            # 提取 file_name 并构建返回字符串
            for item in raw_result:
                if isinstance(item, dict):
                    fn = item.get("file_name")
                    if fn:
                        file_names.append(str(fn))
            append_knowledge_sources(file_names)
            
            # 手动构建类似 rag_retrieval_tool.search_as_string 的输出
            lines = [f"检索到 {len(raw_result)} 条结果：", ""]
            for i, chunk in enumerate(raw_result, 1):
                content = chunk.get("content") or chunk.get("text") or ""
                safe_chunk = clip_text(redact_sensitive(str(content)), 450)
                lines.append(f"【{i}】{safe_chunk}")
                lines.append("")
            result_str = "\n".join(lines)
        else:
            # "未找到相关结果"
            result_str = raw_result

        logger.info(
            f"📚 [知识库检索完成] search_knowledge_base\n"
            f"❓ 查询：'{redact_sensitive(str(query or '')[:200])}'\n"
            f"📊 限制：{limit}\n"
            f"📁 文件过滤：{file_name_filters}\n"
            f"📦 返回条数：{len(file_names)}\n"
            f"⏱️ 耗时：{int((time.time() - t0) * 1000)}ms"
        )
        return result_str

    def list_knowledge_base_chunks_metadata(
        self,
        include_content: bool = False,
        file_name_filters: Any = None,
    ) -> str:
        t0 = time.time()
        # 直接调用父类逻辑
        raw = super().list_knowledge_base_chunks_metadata(
            include_content=include_content,
            file_name_filters=file_name_filters
        )
        
        # 解析结果仅为了日志统计
        summary = {}
        try:
            obj = json.loads(raw)
            items = obj.get("items") if isinstance(obj, dict) else None
            if isinstance(items, list):
                uniq = []
                seen = set()
                for it in items:
                    if isinstance(it, dict) and it.get("file_name"):
                        v = str(it.get("file_name")).strip()
                        if not v or v in seen:
                            continue
                        seen.add(v)
                        uniq.append(v)
                summary = {
                    "item_count": len(items),
                    "unique_file_name_count": len(uniq),
                    "unique_file_name_preview": uniq[:20],
                }
        except Exception:
            pass
            
        logger.info(
            f"📋 [知识库检索完成] list_knowledge_base_chunks_metadata\n"
            f"⚙️ 参数：{{'include_content': {include_content}, 'file_name_filters': {file_name_filters}}}\n"
            f"📊 摘要：{summary}\n"
            f"⏱️ 耗时：{int((time.time() - t0) * 1000)}ms"
        )
        return raw
        


@dataclass
class RuntimeState:
    config: ConfigLoader
    llm_model: str
    llm_base_url: str
    llm_api_key: str
    agent_rag: Optional[Agent] = None
    agent_plain: Optional[Agent] = None
    agent_summary: Optional[Agent] = None
    team_router: Optional[Team] = None
    agent_os: Optional[AgentOS] = None
    agent_os_app: Optional[Any] = None
    rag_enabled: bool = False

_state: Optional[RuntimeState] = None

def init_state() -> RuntimeState:
    global _state
    if _state:
        return _state
        
    config = ConfigLoader()
    llm = config.get_llm_config()

    model = str(llm.get("model_name") or "qwen-plus")
    base_url = str(llm.get("base_url") or "").rstrip("/")
    api_key = str(llm.get("api_key") or "")
    
    _state = RuntimeState(
        config=config, 
        llm_model=model, 
        llm_base_url=base_url, 
        llm_api_key=api_key, 
        agent_rag=None, 
        agent_plain=None, 
        rag_enabled=False
    )
    return _state

def get_state() -> RuntimeState:
    global _state
    if not _state:
        return init_state()
    return _state

def ensure_agents(allow_rag: bool) -> None:
    state = get_state()
    if state.agent_plain is not None and (not allow_rag or state.agent_rag is not None or not state.rag_enabled):
        return
    with _STATE_LOCK:
        initializer = AgentInitializer(
            model_id=state.llm_model,
            api_key=state.llm_api_key,
            base_url=state.llm_base_url,
        )
        if state.agent_plain is None:
            state.agent_plain = initializer.create_work_reply_agent(enable_tools=False)
        if allow_rag and state.agent_rag is None:
            try:
                toolkit_rag = TracedKnowledgeRetrievalToolkit(config_loader=state.config, enable_search=True, enable_prefetch=True)
                state.agent_rag = initializer.create_work_reply_agent(toolkit=toolkit_rag, enable_tools=True)
                state.rag_enabled = True
            except Exception as e:
                logger.warning(f"⚠️ [Agent 初始化警告] Agno RAG Agent 初始化失败，将降级为无工具模式\n"
                               f"⚡ 错误类型：{type(e).__name__}\n"
                               f"💥 错误信息：{e}")
                state.agent_rag = None
                state.rag_enabled = False


def ensure_summary_agent() -> None:
    """
    确保摘要 Agent 已初始化。
    延迟加载以降低服务启动时的初始化成本。
    挂载两个专用 RAG Toolkit（来自 summary_rag_tools.py）：
      - KefuShouhouToolkit：客服售后知识库，直接全库检索
      - ZhuyishixiangToolkit：注意事项知识库，两阶段检索
    """
    state = get_state()
    if state.agent_summary is not None:
        return
    with _STATE_LOCK:
        if state.agent_summary is not None:
            return
        initializer = AgentInitializer(
            model_id=state.llm_model,
            api_key=state.llm_api_key,
            base_url=state.llm_base_url,
        )
        summary_toolkits = create_summary_rag_toolkits(state.config)
        state.agent_summary = initializer.create_summary_agent(toolkits=summary_toolkits)


def ensure_agentos_runtime(allow_rag: bool) -> None:
    """
    构建 AgentOS 运行时并注册 Team 路由器。
    Team 负责在两个成员 Agent（建议、摘要）之间进行意图分发。
    """
    state = get_state()
    if state.agent_os is not None and state.team_router is not None:
        return
    with _STATE_LOCK:
        if state.agent_os is not None and state.team_router is not None:
            return
        ensure_agents(allow_rag=allow_rag)
        ensure_summary_agent()

        suggestion_agent = state.agent_rag if (allow_rag and state.rag_enabled and state.agent_rag is not None) else state.agent_plain
        if suggestion_agent is None or state.agent_summary is None:
            raise RuntimeError("AgentOS 初始化失败：缺少建议或摘要 Agent")

        initializer = AgentInitializer(
            model_id=state.llm_model,
            api_key=state.llm_api_key,
            base_url=state.llm_base_url,
        )
        state.team_router = initializer.create_team_router(suggestion_agent, state.agent_summary)
        state.agent_os = initializer.create_agentos(suggestion_agent, state.agent_summary, state.team_router)
        state.agent_os_app = state.agent_os.get_app()


def get_agentos_app(allow_rag: bool = True) -> Optional[Any]:
    """
    对外暴露 AgentOS FastAPI 子应用，供主服务挂载调试与统一运维入口。
    """
    try:
        ensure_agentos_runtime(allow_rag=allow_rag)
    except Exception as e:
        logger.warning(f"⚠️ [AgentOS] 子应用初始化失败，跳过挂载: {e}")
        return None
    state = get_state()
    return state.agent_os_app


_EXECUTOR = ThreadPoolExecutor(max_workers=10)




async def agent_run(agent: Agent, prompt: str) -> str:
    """
    在线程池中执行 Agent.run，避免阻塞事件循环。
    """
    loop = asyncio.get_running_loop()
    ctx = copy_context()
    result = await loop.run_in_executor(_EXECUTOR, ctx.run, agent.run, prompt)
    return str(result.content).strip()


async def team_run(team: Team, prompt: str) -> str:
    """
    在线程池中执行 Team.run，用于 Team 意图分发场景。
    """
    loop = asyncio.get_running_loop()
    ctx = copy_context()
    result = await loop.run_in_executor(_EXECUTOR, ctx.run, team.run, prompt)
    return str(result.content).strip()
