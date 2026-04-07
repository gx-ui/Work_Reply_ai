import time
import json
import re
import logging
import contextvars
import threading
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Sequence, Union, Tuple, AsyncIterator
from threading import RLock
from agno.agent import Agent

from config.config_loader import ConfigLoader
from tools.rag_retrieval_tool import KnowledgeRetrievalToolkit
from tools.summary_rag_tools import create_summary_rag_toolkits
from agent.work_reply_agent import WorkReplyAgent
from agent.summary_agent import SummaryAgent
from db.mysql_store import init_mysql_engine_from_config
from utils.milvus_utils import clip_text
from utils.common import redact_sensitive
logger = logging.getLogger("agent_service")

# Agno 异步 arun 用 asyncio.to_thread 执行工具，会复制 ContextVar 到工作线程；
# threading.local 导致主协程读不到工具里 append 的来源，suggestion_sources / summary_sources 常为空。
_knowledge_sources_cv: contextvars.ContextVar[Optional[List[str]]] = contextvars.ContextVar(
    "work_reply_ai_knowledge_sources", default=None
)
_STATE_LOCK = RLock()

_SOURCE_BRACKET_RE = re.compile(r"\[来源:\s*([^\]]+?)\s*\]")

__all__ = [
    "TracedKnowledgeRetrievalToolkit",
    "RuntimeState",
    "init_state",
    "get_state",
    "ensure_agents",
    "ensure_summary_agent",
    "agent_run",
    "agent_run_stream_collect",
    "reset_knowledge_sources",
    "append_knowledge_sources",
    "get_knowledge_sources",
    "merge_knowledge_source_names",
    "extract_sources_from_agno_run_output",
]


# ────────────────────────────────────────────────────────────────
# 知识库来源收集（线程局部）：RAG 工具与 agent.run 在同一线程内
# ────────────────────────────────────────────────────────────────
def reset_knowledge_sources() -> None:
    _knowledge_sources_cv.set([])


def append_knowledge_sources(items: Sequence[str]) -> None:
    cur = _knowledge_sources_cv.get()
    if cur is None:
        cur = []
        _knowledge_sources_cv.set(cur)
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
    cur = _knowledge_sources_cv.get()
    return list(cur or [])


def merge_knowledge_source_names(*lists: Sequence[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for seq in lists:
        for it in seq or []:
            v = str(it or "").strip()
            if not v or v in seen:
                continue
            seen.add(v)
            out.append(v)
    return out


def _extract_bracket_sources_from_text(text: str) -> List[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: List[str] = []
    for m in _SOURCE_BRACKET_RE.finditer(text):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _all_text_blobs_from_run_output(run_out: Any) -> List[str]:
    """从 Agent 单次 run 输出中收集可能含「[来源:…]」的文本（含子成员 run）。"""
    blobs: List[str] = []
    if run_out is None:
        return blobs
    c = getattr(run_out, "content", None)
    if c:
        blobs.append(str(c))
    for te in getattr(run_out, "tools", None) or []:
        r = getattr(te, "result", None)
        if r:
            blobs.append(str(r))
    for msg in getattr(run_out, "messages", None) or []:
        cont = getattr(msg, "content", None)
        if cont:
            blobs.append(str(cont))
    for m in getattr(run_out, "member_responses", None) or []:
        blobs.extend(_all_text_blobs_from_run_output(m))
    return blobs


def extract_sources_from_agno_run_output(run_out: Any) -> List[str]:
    """解析工具返回里格式化的 [来源: 文件名]，补足元数据字段缺失时的情况。"""
    names: List[str] = []
    for blob in _all_text_blobs_from_run_output(run_out):
        names.extend(_extract_bracket_sources_from_text(blob))
    return merge_knowledge_source_names(names)


# ────────────────────────────────────────────────────────────────
# 带溯源能力的 RAG Toolkit 包装
# ────────────────────────────────────────────────────────────────
class TracedKnowledgeRetrievalToolkit(KnowledgeRetrievalToolkit):
    def search_knowledge_base(
        self,
        query: str,
        limit: Optional[int] = 5,
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
                    fn = (
                        item.get("file_name")
                        or item.get("filename")
                        or item.get("source")
                        or item.get("doc_name")
                    )
                    if fn:
                        file_names.append(str(fn))
            append_knowledge_sources(file_names)

            lines = [f"检索到 {len(raw_result)} 条结果：", ""]
            for i, chunk in enumerate(raw_result, 1):
                content = chunk.get("content") or chunk.get("text") or ""
                safe_chunk = clip_text(redact_sensitive(str(content)), 450)
                fn_disp = (
                    chunk.get("file_name")
                    or chunk.get("filename")
                    or chunk.get("source")
                    or chunk.get("doc_name")
                )
                fn_disp = str(fn_disp).strip() if fn_disp else ""
                src = f"[来源: {fn_disp}] " if fn_disp else ""
                lines.append(f"【{i}】{src}{safe_chunk}")
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


# ────────────────────────────────────────────────────────────────
# 全局运行时状态
# ────────────────────────────────────────────────────────────────
@dataclass
class RuntimeState:
    config: ConfigLoader
    llm_model: str
    llm_base_url: str
    llm_api_key: str
    mysql_engine: Optional[Any] = None
    agent_rag: Optional[Agent] = None
    agent_plain: Optional[Agent] = None
    agent_summary: Optional[Agent] = None
    work_reply_runner: Optional[Any] = None
    summary_runner: Optional[Any] = None
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

    engine = init_mysql_engine_from_config(config)

    _state = RuntimeState(
        config=config,
        llm_model=model,
        llm_base_url=base_url,
        llm_api_key=api_key,
        mysql_engine=engine,
        agent_rag=None,
        agent_plain=None,
        agent_summary=None,
        work_reply_runner=None,
        summary_runner=None,
        rag_enabled=False,
    )
    return _state

def get_state() -> RuntimeState:
    global _state
    if not _state:
        return init_state()
    return _state


# ────────────────────────────────────────────────────────────────
# Agent 懒加载
# ────────────────────────────────────────────────────────────────
def ensure_agents(allow_rag: bool) -> None:
    """确保工单回复用 agno Agent（plain / rag）已初始化。"""
    state = get_state()
    if state.agent_plain is not None and (not allow_rag or state.agent_rag is not None or not state.rag_enabled):
        return
    with _STATE_LOCK:
        if state.work_reply_runner is None:
            state.work_reply_runner = WorkReplyAgent(state.config)
        if state.agent_plain is None:
            state.agent_plain = state.work_reply_runner._build_agent(tools=[])
        if allow_rag and state.agent_rag is None:
            try:
                toolkit_rag = TracedKnowledgeRetrievalToolkit(
                    config_loader=state.config,
                    enable_search=True,
                    enable_prefetch=True,
                )
                state.agent_rag = state.work_reply_runner._build_agent(tools=[toolkit_rag])
                state.rag_enabled = True
            except Exception as e:
                logger.warning(
                    "[Agent 初始化警告] Agno RAG Agent 初始化失败，将降级为无工具模式\n"
                    "错误类型：%s\n错误信息：%s", type(e).__name__, e
                )
                state.agent_rag = None
                state.rag_enabled = False


def ensure_summary_agent() -> None:
    """
    确保摘要 Agent 已初始化。
    优先使用 config 中的 summary_model，回退到主模型（在 SummaryAgent 封装内解析）。
    """
    state = get_state()
    if state.agent_summary is not None:
        return
    with _STATE_LOCK:
        if state.agent_summary is not None:
            return
        if state.summary_runner is None:
            state.summary_runner = SummaryAgent(state.config)
        summary_toolkits = create_summary_rag_toolkits(state.config)
        state.agent_summary = state.summary_runner._build_agent(tools=summary_toolkits)


# ────────────────────────────────────────────────────────────────
# Agent 运行辅助（agent_run：Agno Agent.arun）
# ────────────────────────────────────────────────────────────────


async def agent_run(
    agent: Agent,
    prompt: str,
    session_id: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """异步执行 Agent """
    reset_knowledge_sources()
    result = await agent.arun(prompt, session_id=session_id)
    text = str(result.content).strip()
    kb = merge_knowledge_source_names(
        get_knowledge_sources(),
        extract_sources_from_agno_run_output(result),
    )
    return text, kb


async def agent_run_stream_collect(
    agent: Agent,
    prompt: str,
    session_id: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """
    流式执行 Agent（Agno stream=True + yield_run_output=True）。
    产出 dict：kind=delta 时含 text；kind=tool 时含 event/name；kind=complete 时含 run_output、kb；
    kind=error 时含 message。
    """
    from agno.run.agent import RunErrorEvent, RunOutput, RunEvent

    reset_knowledge_sources()
    agen = agent.arun(
        prompt,
        session_id=session_id,
        stream=True,
        yield_run_output=True,
    )
    async for item in agen:
        if isinstance(item, RunOutput):
            kb = merge_knowledge_source_names(
                get_knowledge_sources(),
                extract_sources_from_agno_run_output(item),
            )
            yield {
                "kind": "complete",
                "run_output": item,
                "kb": kb,
            }
            return
        if isinstance(item, RunErrorEvent):
            msg = str(getattr(item, "content", None) or "Agent 运行错误")
            yield {"kind": "error", "message": msg}
            return
        ev = getattr(item, "event", None) or ""
        if ev == RunEvent.run_content.value:
            c = getattr(item, "content", None)
            if c:
                yield {"kind": "delta", "text": str(c)}
            continue
        if "ToolCall" in ev:
            yield {"kind": "tool", "event": ev}
    yield {"kind": "error", "message": "流式结束但未收到 RunOutput"}
