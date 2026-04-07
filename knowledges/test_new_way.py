# -*- coding: utf-8 -*-
"""
test_new_way.py
基于 Agno 框架参数的 summary 本地全流程测试（纯手动数据，无后端接口）

本文件重点演示：
1. Agent(knowledge_retriever=...)
2. knowledge_filters（run_context 传入）
3. add_knowledge_to_context（由 Agno 注入 references 到 user message）
4. enable_agentic_knowledge_filters（打开，便于后续 tool 方式扩展）
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agno.agent import Agent
from agno.agent import _messages as agno_messages
from agno.models.dashscope import DashScope
from agno.run.agent import RunOutput
from agno.run.base import RunContext
from config.config_loader import ConfigLoader


# ----------------------------
# 日志配置：输出到当前目录 log.txt
# ----------------------------
BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "log.txt"

logger = logging.getLogger("summary_new_way_test")
logger.setLevel(logging.DEBUG)
logger.handlers.clear()

file_handler = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


# ----------------------------
# 数据结构（手动构造请求体）
# ----------------------------
@dataclass
class WorksInfo:
    ticket_id: str
    title: str
    desc: str
    status: str
    priority: str
    history: List[Dict[str, Any]]


@dataclass
class CoreInfo:
    customer_name: str
    project_name: str
    mall_name: str


@dataclass
class AttentionInfo:
    project_attention: str
    supplier_attention: str


@dataclass
class SummaryRequest:
    works_info: WorksInfo
    core_info: CoreInfo
    attention_info: AttentionInfo


# ----------------------------
# 手动知识库（模拟 Milvus 检索结果）
# ----------------------------
ZHUYISHIXIANG_DOCS: List[Dict[str, Any]] = [
    {
        "file_name": "南网_客服项目_注意事项_v3",
        "content": "投诉升级工单需在2小时内首响；涉及补偿必须审批；禁止承诺超流程时效。",
        "tags": ["南网", "客服项目", "投诉", "补偿", "时效"],
    },
    {
        "file_name": "工行_商城_注意事项_v2",
        "content": "少发场景需先核单再补发，48小时内给出处理结论。",
        "tags": ["工行", "商城", "少发", "补发"],
    },
    {
        "file_name": "通用_升级投诉处理规范",
        "content": "升级投诉需记录证据链，优先安抚并同步下一步动作和完成时间。",
        "tags": ["通用", "投诉", "升级"],
    },
]

KEFU_SHOUHOU_DOCS: List[Dict[str, Any]] = [
    {
        "file_name": "售后SOP_少发补发",
        "content": "少发处理流程：核对订单与出库记录->确认责任->补发并同步物流单号。",
        "tags": ["少发", "补发", "流程"],
    },
    {
        "file_name": "售后SOP_投诉安抚",
        "content": "投诉场景需先道歉并给出明确时间承诺，避免模糊表述。",
        "tags": ["投诉", "安抚", "时效"],
    },
]


def extract_keywords(text: str) -> List[str]:
    """简化关键词提取（纯规则）。"""
    vocab = [
        "少发",
        "补发",
        "退款",
        "换货",
        "投诉",
        "物流",
        "超时",
        "升级",
        "补偿",
        "南网",
        "工行",
        "商城",
        "客服项目",
    ]
    hit = [w for w in vocab if w in text]
    logger.debug("extract_keywords | text=%s | hit=%s", text, hit)
    return hit


def score_doc(doc: Dict[str, Any], query_keywords: List[str], filter_keywords: List[str]) -> int:
    """简单打分：query 命中 +2，filter 命中 +3。"""
    tags = doc.get("tags", [])
    score = 0
    for word in query_keywords:
        if word in tags or word in doc.get("content", ""):
            score += 2
    for word in filter_keywords:
        if word in tags or word in doc.get("file_name", ""):
            score += 3
    return score


def build_knowledge_filters(core_info: CoreInfo) -> Dict[str, Any]:
    """构造 run 级别 knowledge_filters。"""
    filters = {
        "customer_name": core_info.customer_name,
        "project_name": core_info.project_name,
        "mall_name": core_info.mall_name,
    }
    logger.info("build_knowledge_filters | filters=%s", filters)
    return filters


def summary_knowledge_retriever(
    agent: Agent,
    query: str,
    num_documents: Optional[int] = None,
    filters: Optional[Dict[str, Any]] = None,
    run_context: Optional[RunContext] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """
    Agno 兼容签名的自定义 retriever：
    - 参数会被 Agno 按签名自动注入（agent/query/num_documents/filters/run_context）
    - 这里用手动知识数据模拟两库检索与合并
    """
    logger.info(
        "summary_knowledge_retriever start | agent_id=%s | query=%s | num_documents=%s | filters=%s",
        agent.id,
        query,
        num_documents,
        filters,
    )
    if run_context is not None:
        logger.debug("run_context.knowledge_filters=%s", run_context.knowledge_filters)

    query_keywords = extract_keywords(query)
    filters = filters or {}
    filter_keywords = [
        str(filters.get("customer_name", "")),
        str(filters.get("project_name", "")),
        str(filters.get("mall_name", "")),
    ]
    filter_keywords = [x for x in filter_keywords if x]

    # A. 注意事项库召回（优先）
    zyx_scored: List[Any] = []
    for doc in ZHUYISHIXIANG_DOCS:
        score = score_doc(doc, query_keywords, filter_keywords)
        if score > 0:
            zyx_scored.append((score, doc))
    zyx_scored.sort(key=lambda item: item[0], reverse=True)
    zyx_docs = [item[1] for item in zyx_scored[:3]]
    logger.info("zhuyishixiang recalled=%s", [d["file_name"] for d in zyx_docs])

    # B. 客服售后库召回（补充）
    ks_scored: List[Any] = []
    for doc in KEFU_SHOUHOU_DOCS:
        score = score_doc(doc, query_keywords, [])
        if score > 0:
            ks_scored.append((score, doc))
    ks_scored.sort(key=lambda item: item[0], reverse=True)
    ks_docs = [item[1] for item in ks_scored[:2]]
    logger.info("kefu_shouhou recalled=%s", [d["file_name"] for d in ks_docs])

    # C. 合并 references（注意事项优先）
    merged: List[Dict[str, Any]] = []
    for doc in zyx_docs:
        merged.append(
            {
                "source": doc["file_name"],
                "kb_type": "zhuyishixiang",
                "content": doc["content"],
            }
        )
    for doc in ks_docs:
        merged.append(
            {
                "source": doc["file_name"],
                "kb_type": "kefushouhou",
                "content": doc["content"],
            }
        )

    limit = num_documents if isinstance(num_documents, int) and num_documents > 0 else 5
    merged = merged[:limit]
    logger.info("merged references count=%s", len(merged))
    return merged


def create_agno_summary_agent(default_filters: Dict[str, Any]) -> Agent:
    """
    创建 Agno Agent（不调用模型，仅演示 Agno 知识链路）。
    """
    cfg = ConfigLoader()
    llm = cfg.get_llm_config()
    model_id = str(llm.get("summary_model") or llm.get("model_name") or "qwen3.5-flash")
    model = DashScope(
        id=model_id,
        api_key=str(llm.get("api_key") or ""),
        base_url=str(llm.get("base_url") or ""),
    )

    agent = Agent(
        id="summary-agent-test-local",
        name="Summary Test Agent",
        model=model,
        knowledge_retriever=summary_knowledge_retriever,
        knowledge_filters=default_filters,
        enable_agentic_knowledge_filters=True,
        add_knowledge_to_context=True,
        search_knowledge=True,
        references_format="json",
        markdown=False,
        debug_mode=True,
    )
    logger.info(
        "create_agno_summary_agent done | id=%s | model=%s | add_knowledge_to_context=%s | enable_agentic_knowledge_filters=%s",
        agent.id,
        model_id,
        agent.add_knowledge_to_context,
        agent.enable_agentic_knowledge_filters,
    )
    return agent


def run_agno_reference_injection(agent: Agent, query: str, runtime_filters: Dict[str, Any]) -> Dict[str, Any]:
    """
    使用 Agno 内部消息构建流程，触发 add_knowledge_to_context：
    - get_user_message() 内部会调用 get_relevant_docs_from_knowledge()
    - 因为 agent 配置了 knowledge_retriever，所以会走自定义 retriever
    """
    run_id = f"run-{int(time.time() * 1000)}"
    run_context = RunContext(
        run_id=run_id,
        session_id="session-local-test",
        user_id="user-local-test",
        knowledge_filters=runtime_filters,
    )
    run_output = RunOutput(
        run_id=run_id,
        agent_id=agent.id,
        agent_name=agent.name,
        session_id=run_context.session_id,
        user_id=run_context.user_id,
    )

    logger.info("run_agno_reference_injection start | run_id=%s", run_id)
    user_message = agno_messages.get_user_message(
        agent,
        run_response=run_output,
        run_context=run_context,
        input=query,
    )

    references: List[Dict[str, Any]] = []
    if run_output.references:
        for item in run_output.references:
            references.extend(item.references or [])
    logger.info(
        "agno injected references count=%s | user_message_len=%s",
        len(references),
        len(str(user_message.content)) if user_message else 0,
    )
    logger.debug("user_message.content=%s", str(user_message.content) if user_message else "")
    logger.debug("references=%s", json.dumps(references, ensure_ascii=False, indent=2))
    return {"user_message": user_message, "references": references, "run_output": run_output}


def generate_summary(req: SummaryRequest, refs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """模拟 summary 输出（非 LLM，仅规则拼装）。"""
    logger.info("generate_summary start")
    works = req.works_info
    core = req.core_info
    attention = req.attention_info

    ref_texts = [str(item.get("content", "")) for item in refs]
    ref_sources = [str(item.get("source", "")) for item in refs if str(item.get("source", ""))]
    history_lines = [str(item.get("summary", "")) for item in works.history if isinstance(item, dict)]

    info_summary = (
        f"工单【{works.title}】；状态【{works.status}】优先级【{works.priority}】。"
        f"客户/项目/商城为【{core.customer_name}/{core.project_name}/{core.mall_name}】。"
        f"主诉为：{works.desc}。"
        f"历史进展：{('；'.join(history_lines[:2]) if history_lines else '待确认')}。"
        f"结合知识库建议：{('；'.join(ref_texts[:2]) if ref_texts else '待确认')}。"
    )

    review_parts: List[str] = []
    zyx_rules = [str(item.get("content", "")) for item in refs if item.get("kb_type") == "zhuyishixiang"]
    if zyx_rules:
        review_parts.append(f"项目强规则：{zyx_rules[0]}")
    if attention.project_attention:
        review_parts.append(f"字段注意事项：{attention.project_attention}")
    if attention.supplier_attention:
        review_parts.append(f"供应商注意：{attention.supplier_attention}")
    if not review_parts:
        review_parts.append("无")

    result = {
        "summary": {
            "info_summary": info_summary,
            "reviews": "；".join(review_parts),
        },
        "summary_sources": ref_sources,
    }
    logger.info("generate_summary done | summary_sources=%s", ref_sources)
    return result


def main() -> None:
    logger.info("========== 测试开始（Agno 版本）==========")

    req = SummaryRequest(
        works_info=WorksInfo(
            ticket_id="T20260407001",
            title="用户投诉少发商品",
            desc="用户反馈订单少发一件，要求尽快补发并给出时效说明。",
            status="处理中",
            priority="P1",
            history=[
                {"summary": "客服已首次联系用户并收集订单号"},
                {"summary": "仓库侧待核对出库明细"},
            ],
        ),
        core_info=CoreInfo(
            customer_name="南网",
            project_name="客服项目",
            mall_name="商城",
        ),
        attention_info=AttentionInfo(
            project_attention="升级投诉必须2小时内首响，补偿需审批。",
            supplier_attention="供应商责任以出库记录与签收证据为准。",
        ),
    )
    logger.debug("request=%s", json.dumps(asdict(req), ensure_ascii=False, indent=2))

    query = f"{req.works_info.title} {req.works_info.desc}"
    logger.info("query=%s", query)

    # Agent 默认过滤器
    default_filters = build_knowledge_filters(req.core_info)
    agent = create_agno_summary_agent(default_filters=default_filters)

    # run 级过滤器（用于演示：run 参数可覆盖/补充 agent 默认过滤器）
    runtime_filters = {
        "customer_name": req.core_info.customer_name,
        "project_name": req.core_info.project_name,
        "mall_name": req.core_info.mall_name,
    }
    logger.info("runtime_filters=%s", runtime_filters)

    # 触发 Agno add_knowledge_to_context 链路，拿到 references
    agno_result = run_agno_reference_injection(
        agent=agent,
        query=query,
        runtime_filters=runtime_filters,
    )
    refs = agno_result["references"]

    # 基于 references 生成 summary（本地规则版）
    result = generate_summary(req, refs)
    logger.info("final_result=%s", json.dumps(result, ensure_ascii=False, indent=2))
    logger.info("========== 测试结束 ==========")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n日志已写入: {LOG_PATH}")


if __name__ == "__main__":
    main()