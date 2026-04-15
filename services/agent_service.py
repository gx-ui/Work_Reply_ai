import time
import json
import re
import logging
import contextvars
from collections import Counter
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Sequence, Union, Tuple
from threading import RLock
from agno.agent import Agent

from config.config_loader import ConfigLoader
from tools.rag_retrieval_tool import KnowledgeRetrievalToolkit
from tools.summary_rag_tools import (
    create_summary_rag_toolkits,
)
from agent.work_reply_agent import WorkReplyAgent
from agent.summary_agent import SummaryAgent
from db.mysql_store import init_mysql_engine_from_config


from utils.log_utils import (
    record_tool_invocation,
    reset_tool_invocations,
    get_tool_invocations,
)
logger = logging.getLogger("agent_service")

# Agno 异步 arun 用 asyncio.to_thread 执行工具，会复制 ContextVar 到工作线程；
# threading.local 导致主协程读不到工具里 append 的来源，knowledge_sources 常为空。
_knowledge_sources_cv: contextvars.ContextVar[Optional[List[str]]] = contextvars.ContextVar(
    "work_reply_ai_knowledge_sources", default=None
)
_summary_knowledge_sources_cv: contextvars.ContextVar[Optional[Dict[str, List[str]]]] = contextvars.ContextVar(
    "work_reply_ai_summary_knowledge_sources", default=None
)
SUMMARY_SOURCE_BUCKET_INFO = "info"
SUMMARY_SOURCE_BUCKET_REVIEWS = "reviews"
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
    "reset_knowledge_sources",
    "append_knowledge_sources",
    "get_knowledge_sources",
    "get_summary_knowledge_sources",
    "SUMMARY_SOURCE_BUCKET_INFO",
    "SUMMARY_SOURCE_BUCKET_REVIEWS",
    "merge_knowledge_source_names",
    "extract_sources_from_agno_run_output",
]


# ────────────────────────────────────────────────────────────────
# 知识库来源收集（线程局部）：RAG 工具与 agent.run 在同一线程内
# ────────────────────────────────────────────────────────────────
def reset_knowledge_sources() -> None:
    _knowledge_sources_cv.set([])
    _summary_knowledge_sources_cv.set(
        {
            SUMMARY_SOURCE_BUCKET_INFO: [],
            SUMMARY_SOURCE_BUCKET_REVIEWS: [],
        }
    )


def _clean_source_name(raw: Any) -> str:
    v = str(raw or "")
    v = v.replace("\ufeff", "")
    v = v.replace("\u200b", "").replace("\u200e", "").replace("\u200f", "")
    v = v.replace("\r", "").replace("\n", "").strip()
    return v


def append_knowledge_sources(items: Sequence[str], bucket: Optional[str] = None) -> None:
    cur = _knowledge_sources_cv.get()
    if cur is None:
        cur = []
        _knowledge_sources_cv.set(cur)
    seen = set(cur)
    normalized_items: List[str] = []
    for it in items or []:
        v = _clean_source_name(it)
        if not v:
            continue
        normalized_items.append(v)
        if v in seen:
            continue
        seen.add(v)
        cur.append(v)

    if not bucket:
        return
    bucket_key = str(bucket or "").strip().lower()
    if bucket_key not in (SUMMARY_SOURCE_BUCKET_INFO, SUMMARY_SOURCE_BUCKET_REVIEWS):
        return
    bucket_map = _summary_knowledge_sources_cv.get()
    if bucket_map is None:
        bucket_map = {
            SUMMARY_SOURCE_BUCKET_INFO: [],
            SUMMARY_SOURCE_BUCKET_REVIEWS: [],
        }
        _summary_knowledge_sources_cv.set(bucket_map)
    bucket_items = bucket_map.get(bucket_key) or []
    bucket_seen = set(bucket_items)
    for v in normalized_items:
        if v in bucket_seen:
            continue
        bucket_seen.add(v)
        bucket_items.append(v)
    bucket_map[bucket_key] = bucket_items


def get_knowledge_sources(bucket: Optional[str] = None) -> List[str]:
    if bucket:
        return get_summary_knowledge_sources().get(str(bucket or "").strip().lower(), [])
    cur = _knowledge_sources_cv.get()
    return list(cur or [])


def get_summary_knowledge_sources() -> Dict[str, List[str]]:
    bucket_map = _summary_knowledge_sources_cv.get() or {}
    return {
        SUMMARY_SOURCE_BUCKET_INFO: list(bucket_map.get(SUMMARY_SOURCE_BUCKET_INFO) or []),
        SUMMARY_SOURCE_BUCKET_REVIEWS: list(bucket_map.get(SUMMARY_SOURCE_BUCKET_REVIEWS) or []),
    }


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


def _extract_agent_available_tool_names(agent: Agent) -> List[str]:
    names: List[str] = []
    seen: set[str] = set()
    for tool in getattr(agent, "tools", None) or []:
        if callable(tool):
            name = getattr(tool, "__name__", str(tool))
            if name and name not in seen:
                seen.add(name)
                names.append(name)
            continue
        toolkit_name = str(getattr(tool, "name", "") or "").strip()
        inner_tools = getattr(tool, "tools", None)
        if isinstance(inner_tools, list) and inner_tools:
            for fn in inner_tools:
                fn_name = str(getattr(fn, "__name__", str(fn)) or "").strip()
                display = f"{toolkit_name}.{fn_name}" if toolkit_name else fn_name
                if display and display not in seen:
                    seen.add(display)
                    names.append(display)
            continue
        if toolkit_name and toolkit_name not in seen:
            seen.add(toolkit_name)
            names.append(toolkit_name)
    return names


def _build_tool_decision_audit(
    agent: Agent,
    called_tools: Sequence[str],
    *,
    available_tools: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    available = list(available_tools) if available_tools is not None else _extract_agent_available_tool_names(agent)
    called_list = [str(x).strip() for x in called_tools if str(x).strip()]
    called_counter = Counter(called_list)
    called_names = list(called_counter.keys())
    called_total = int(sum(called_counter.values()))
    if not available:
        no_tool_reason = "no_tools_available"
    elif called_total <= 0:
        no_tool_reason = "model_direct_answer_or_prompt_judged_no_tool"
    else:
        no_tool_reason = "tools_called"
    return {
        "available_tools": available,
        "available_tool_count": len(available),
        "called_tools": called_names,
        "called_tool_count": called_total,
        "called_tool_histogram": dict(called_counter),
        "no_tool_reason": no_tool_reason,
    }


def _normalize_called_tool_name(raw_name: Any, available_tools: Sequence[str]) -> str:
    name = str(raw_name or "").strip()
    if not name:
        return ""
    if name in available_tools:
        return name
    suffix = f".{name}"
    matches = [tool for tool in available_tools if tool.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return sorted(matches)[0]
    return name


def _extract_tool_name_candidates(tool_event: Any) -> List[str]:
    candidates: List[str] = []
    if tool_event is None:
        return candidates
    if isinstance(tool_event, dict):
        for key in ("tool_name", "name", "function_name", "tool"):
            value = tool_event.get(key)
            if isinstance(value, dict):
                nested = value.get("name")
                if nested:
                    candidates.append(str(nested))
            elif value:
                candidates.append(str(value))
        return candidates

    for attr in ("tool_name", "name", "function_name", "tool"):
        value = getattr(tool_event, attr, None)
        if isinstance(value, dict):
            nested = value.get("name")
            if nested:
                candidates.append(str(nested))
            continue
        nested_name = getattr(value, "name", None) if value is not None else None
        if nested_name:
            candidates.append(str(nested_name))
            continue
        if value:
            candidates.append(str(value))
    return candidates


def _extract_called_tool_names_from_run_output(
    run_out: Any,
    available_tools: Sequence[str],
) -> List[str]:
    called: List[str] = []
    if run_out is None:
        return called
    for tool_event in getattr(run_out, "tools", None) or []:
        for candidate in _extract_tool_name_candidates(tool_event):
            normalized = _normalize_called_tool_name(candidate, available_tools)
            if normalized:
                called.append(normalized)
                break
    for member in getattr(run_out, "member_responses", None) or []:
        called.extend(_extract_called_tool_names_from_run_output(member, available_tools))
    return called


def _merge_called_tools(
    *,
    available_tools: Sequence[str],
    recorded_tools: Sequence[str],
    run_out: Any = None,
) -> List[str]:
    merged: List[str] = []
    for name in recorded_tools:
        normalized = _normalize_called_tool_name(name, available_tools)
        if normalized:
            merged.append(normalized)
    if run_out is not None:
        merged.extend(_extract_called_tool_names_from_run_output(run_out, available_tools))
    return merged


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
        record_tool_invocation("knowledge_retrieval_toolkit.search_knowledge_base")
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
                safe_chunk = str(content)
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
            f"查询：'{str(query or '')}'\n"
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
        record_tool_invocation("knowledge_retrieval_toolkit.list_knowledge_base_chunks_metadata")
        t0 = time.time()
        raw = super().list_knowledge_base_chunks_metadata(
            include_content=include_content,
            file_name_filters=file_name_filters
        )

        summary = {}
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                uniq = []
                seen = set()
                items = obj.get("items")
                if isinstance(items, list):
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
                else:
                    fields_name_list = obj.get("fields_name_list")
                    if isinstance(fields_name_list, list):
                        for it in fields_name_list:
                            v = str(it or "").strip()
                            if not v or v in seen:
                                continue
                            seen.add(v)
                            uniq.append(v)
                    summary = {
                        "unique_total_entities": int(obj.get("unique_total_entities", len(uniq))),
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
    if state.agent_plain is not None and (not allow_rag or state.agent_rag is not None):
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
) -> Tuple[str, List[str], Dict[str, Any]]:
    """异步执行 Agent """
    reset_knowledge_sources()
    reset_tool_invocations()
    available_tools = _extract_agent_available_tool_names(agent)
    result = await agent.arun(prompt, session_id=session_id)
    text = str(result.content).strip()
    kb = merge_knowledge_source_names(
        get_knowledge_sources(),
        extract_sources_from_agno_run_output(result),
    )
    called_tools = _merge_called_tools(
        available_tools=available_tools,
        recorded_tools=get_tool_invocations(),
        run_out=result,
    )
    audit = _build_tool_decision_audit(
        agent,
        called_tools,
        available_tools=available_tools,
    )
    tool_histogram = audit.get("called_tool_histogram", {})
    duplicates = {tool: count for tool, count in tool_histogram.items() if count > 1}

    logger.info(
        "[Agent工具决策审计]\n"
        "可用工具数: %s\n"
        "可用工具: %s\n"
        "实际调用次数: %s\n"
        "实际调用工具: %s\n"
        "工具调用详情: %s\n"
        "未调用原因标签: %s",
        len(available_tools),
        available_tools,
        audit.get("called_tool_count"),
        audit.get("called_tool_histogram"),
        get_tool_invocations(),  # 显示完整的工具调用序列
        audit.get("no_tool_reason"),
    )

    if duplicates:
        logger.warning(
            "[Agent告警] 检测到重复工具调用: %s\n"
            "可能导致: 成本增加、响应变慢、结果不一致\n"
            "建议: 检查提示词是否明确要求每个工具只需调用一次",
            duplicates,
        )

    return text, kb, audit
