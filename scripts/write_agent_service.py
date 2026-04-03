# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
dst = ROOT / "backend" / "services" / "agent_service.py"
dst.write_text(
    r"""import time
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
from agent.work_reply_agent import WorkReplyAgent
from agent.summary_agent import SummaryAgent
from agent.work_reply_team import build_work_reply_team_router
from db.mysql_store import init_mysql_for_agents_from_config
from utils.milvus_utils import clip_text
from utils.common import redact_sensitive
import asyncio
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("agent_service")

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
            for item in raw_result:
                if isinstance(item, dict):
                    fn = item.get("file_name")
                    if fn:
                        file_names.append(str(fn))
            append_knowledge_sources(file_names)

            lines = [f"检索到 {len(raw_result)} 条结果：", ""]
            for i, chunk in enumerate(raw_result, 1):
                content = chunk.get("content") or chunk.get("text") or ""
                safe_chunk = clip_text(redact_sensitive(str(content)), 450)
                lines.append(f"【{i}】{safe_chunk}")
                lines.append("")
            result_str = "\n".join(lines)
        else:
            result_str = raw_result

        logger.info(
            f"[知识库检索完成] search_knowledge_base\n"
            f"查询：'{redact_sensitive(str(query or '')[:200])}'\n"
            f"限制：{limit}\n"
            f"文件过滤：{file_name_filters}\n"
            f"返回条数：{len(file_names)}\n"
            f"耗时：{int((time.time() - t0) * 1000)}ms"
        )
        return result_str

    def list_knowledge_base_chunks_metadata(
        self,
        include_content: bool = False,
        file_name_filters: Any = None,
    ) -> str:
        t0 = time.time()
        raw = super().list_knowledge_base_chunks_metadata(
            include_content=include_content,
            file_name_filters=file_name_filters
        )

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
            f"[知识库检索完成] list_knowledge_base_chunks_metadata\n"
            f"参数：{{'include_content': {include_content}, 'file_name_filters': {file_name_filters}}}\n"
            f"摘要：{summary}\n"
            f"耗时：{int((time.time() - t0) * 1000)}ms"
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
    mysql_engine: Any = None
    db_work_reply: Any = None
    db_summary: Any = None
    mysql_persistence_ready: bool = False
    num_history_runs: int = 10


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
    sp = config.get_session_persistence_config()
    num_hist = int(sp.get("num_history_runs", 10)) if sp else 10

    _state = RuntimeState(
        config=config,
        llm_model=model,
        llm_base_url=base_url,
        llm_api_key=api_key,
        agent_rag=None,
        agent_plain=None,
        rag_enabled=False,
        num_history_runs=num_hist,
    )
    return _state


def get_state() -> RuntimeState:
    global _state
    if not _state:
        return init_state()
    return _state


def _ensure_mysql_stores_locked(state: RuntimeState) -> None:
    if state.mysql_engine is not None or state.db_work_reply is not None:
        return
    engine, db_wr, db_sum = init_mysql_for_agents_from_config(state.config)
    state.mysql_engine = engine
    state.db_work_reply = db_wr
    state.db_summary = db_sum
    state.mysql_persistence_ready = bool(db_wr is not None and db_sum is not None)


def ensure_agents(allow_rag: bool) -> None:
    state = get_state()
    if state.agent_plain is not None and (not allow_rag or state.agent_rag is not None or not state.rag_enabled):
        return
    with _STATE_LOCK:
        _ensure_mysql_stores_locked(state)
        db_wr = state.db_work_reply
        nh = state.num_history_runs
        if state.agent_plain is None:
            state.agent_plain = WorkReplyAgent(
                model_id=state.llm_model,
                api_key=state.llm_api_key,
                base_url=state.llm_base_url,
                agent_id="work-reply-plain",
                tools=[],
                db=db_wr,
                num_history_runs=nh,
            )
        if allow_rag and state.agent_rag is None:
            try:
                toolkit_rag = TracedKnowledgeRetrievalToolkit(
                    config_loader=state.config,
                    enable_search=True,
                    enable_prefetch=True,
                )
                state.agent_rag = WorkReplyAgent(
                    model_id=state.llm_model,
                    api_key=state.llm_api_key,
                    base_url=state.llm_base_url,
                    agent_id="work-reply-rag",
                    tools=[toolkit_rag],
                    db=db_wr,
                    num_history_runs=nh,
                )
                state.rag_enabled = True
            except Exception as e:
                logger.warning(
                    "[Agent 初始化警告] Agno RAG Agent 初始化失败，将降级为无工具模式\n"
                    "错误类型：%s\n错误信息：%s", type(e).__name__, e
                )
                state.agent_rag = None
                state.rag_enabled = False


def ensure_summary_agent() -> None:
    state = get_state()
    if state.agent_summary is not None:
        return
    with _STATE_LOCK:
        if state.agent_summary is not None:
            return
        _ensure_mysql_stores_locked(state)
        llm_cfg = state.config.get_llm_config()
        summary_model = llm_cfg.get("summary_model") or state.llm_model
        summary_toolkits = create_summary_rag_toolkits(state.config)
        state.agent_summary = SummaryAgent(
            model_id=summary_model,
            api_key=state.llm_api_key,
            base_url=state.llm_base_url,
            agent_id="work-order-summary",
            tools=summary_toolkits,
            db=state.db_summary,
            num_history_runs=state.num_history_runs,
        )


def ensure_agentos_runtime(allow_rag: bool) -> None:
    state = get_state()
    if state.agent_os is not None and state.team_router is not None:
        return
    with _STATE_LOCK:
        if state.agent_os is not None and state.team_router is not None:
            return
        ensure_agents(allow_rag=allow_rag)
        ensure_summary_agent()

        suggestion_agent = (
            state.agent_rag
            if (allow_rag and state.rag_enabled and state.agent_rag is not None)
            else state.agent_plain
        )
        if suggestion_agent is None or state.agent_summary is None:
            raise RuntimeError("AgentOS 初始化失败：缺少建议或摘要 Agent")

        state.team_router = build_work_reply_team_router(
            suggestion_agent,
            state.agent_summary,
            model_id=state.llm_model,
            api_key=state.llm_api_key,
            base_url=state.llm_base_url,
        )

        state.agent_os = AgentOS(
            id="work-reply-ai-agentos-runtime",
            description="Work Reply AI AgentOS Runtime",
            agents=[suggestion_agent, state.agent_summary],
            teams=[state.team_router],
        )
        state.agent_os_app = state.agent_os.get_app()


def get_agentos_app(allow_rag: bool = True) -> Optional[Any]:
    try:
        ensure_agentos_runtime(allow_rag=allow_rag)
    except Exception as e:
        logger.warning("[AgentOS] 子应用初始化失败，跳过挂载: %s", e)
        return None
    state = get_state()
    return state.agent_os_app


_EXECUTOR = ThreadPoolExecutor(max_workers=10)


async def agent_run(
    agent: Agent,
    prompt: str,
    *,
    session_id: Optional[str] = None,
) -> str:
    loop = asyncio.get_running_loop()
    ctx = copy_context()

    def _run():
        kw: Dict[str, Any] = {}
        if session_id:
            kw["session_id"] = session_id
        if kw:
            return agent.run(prompt, **kw)
        return agent.run(prompt)

    result = await loop.run_in_executor(_EXECUTOR, ctx.run, _run)
    return str(result.content).strip()


async def team_run(team: Team, prompt: str) -> str:
    loop = asyncio.get_running_loop()
    ctx = copy_context()
    result = await loop.run_in_executor(_EXECUTOR, ctx.run, team.run, prompt)
    return str(result.content).strip()
""",
    encoding="utf-8",
)
print("wrote agent_service.py")
