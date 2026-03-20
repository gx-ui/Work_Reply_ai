"""
summary_rag_tools.py

为摘要 Agent 专用的两个独立 RAG 检索 Toolkit：

1. KefuShouhouToolkit
   - 对接 Milvus 集合：kefushouhou（客服售后知识库）
   - 无需 file_name 筛选，直接全库语义检索
   - 适用：售后流程、补发退款口径、物流处理等全局通用知识

2. ZhuyishixiangToolkit
   - 对接 Milvus 集合：zhuyishixiang1（注意事项知识库）
   - 必须先 file_name 筛选（两阶段检索），因注意事项与项目强绑定
   - 适用：项目/客户特定规范、特殊处理规则、投诉处理要求等

两个 Toolkit 均复用 KnowledgeRetrievalTool 内核，支持父子块检索与 rerank。
"""

import json
import time
import requests
import logging
import sys
from typing import Dict, Union, List, Optional, Any

from agno.tools import Toolkit
from config.config_loader import ConfigLoader
from tools.milvus_tool import create_milvus_tools, MilvusSearchTool
from utils.milvus_utils import clip_text
from utils.common import redact_sensitive


logger = logging.getLogger("summary_rag_tools")
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(asctime)s\n%(message)s\n' + '-' * 80))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ===========================================================================
# 内核：通用检索 + Rerank 逻辑（与 rag_retrieval_tool.py 的 KnowledgeRetrievalTool 相同规范）
# ===========================================================================

class SummaryRetrievalCore:
    """
    摘要 Agent RAG 检索内核。
    封装 Milvus 检索 + Rerank 逻辑，供两个专用 Toolkit 共享。
    与 KnowledgeRetrievalTool 规范相同，但独立部署，职责分离。
    """

    def __init__(
        self,
        milvus_tool: MilvusSearchTool,
        config_loader: Optional[ConfigLoader] = None,
        tool_name: str = "summary_retrieval_core",
    ):
        self.milvus_tool = milvus_tool
        self.tool_name = tool_name

        # Rerank 配置
        rerank_config = config_loader.get_rerank_config() if (config_loader and hasattr(config_loader, "get_rerank_config")) else {}
        self.rerank_enabled = rerank_config.get("enabled", False)
        if self.rerank_enabled:
            self.rerank_api_key = rerank_config.get("api_key", "")
            self.rerank_model = rerank_config.get("model_name", "gte-rerank-v2")
            self.rerank_top_k = rerank_config.get("top_k", 3)
            self.rerank_threshold = rerank_config.get("threshold", 0.1)
            self.rerank_url = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
            logger.info(f"✅ [{tool_name}] Rerank 已启用 - 模型：{self.rerank_model}, top_k: {self.rerank_top_k}")
        else:
            logger.info(f"ℹ️ [{tool_name}] Rerank 未启用")

    def search(
        self,
        query: str,
        limit: Optional[int] = None,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> Union[List[Dict[str, Any]], str]:
        """
        执行 Milvus 检索。

        Args:
            query: 检索意图文本
            limit: 返回结果数量
            file_name_filters: 文件名过滤条件（None 表示全库检索）

        Returns:
            List[dict]（含 file_name + text）或 "未找到相关结果"
        """
        logger.info(
            f"🔍 [{self.tool_name}] 检索开始\n"
            f"❓ 查询：'{query}'  📊 limit：{limit}  📁 过滤：{file_name_filters}"
        )

        items: List[Dict[str, Any]] = []
        strategy = "未知"

        if hasattr(self.milvus_tool, "search_with_metadata"):
            rows = []
            if file_name_filters:
                strategy = "file_name过滤搜索"
                rows = self.milvus_tool.search_with_metadata(
                    query=query, limit=limit, filter_str=file_name_filters
                )
                if not rows:
                    logger.info(f"⚠️ [{self.tool_name}] 过滤无结果，降级全量兜底")
            if not rows:
                strategy = "全量兜底"
                rows = self.milvus_tool.search_with_metadata(
                    query=query, limit=limit, filter_str=None
                )
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                text = str(r.get("text", "") or "")
                if not text:
                    continue
                items.append({"file_name": str(r.get("file_name", "") or ""), "text": text})
        else:
            chunks: List[str] = []
            if file_name_filters:
                strategy = "显式过滤"
                chunks = self.milvus_tool.search(
                    query=query, limit=limit, filter_str=file_name_filters
                )
            if not chunks:
                strategy = "全量兜底"
                chunks = self.milvus_tool.search(query=query, limit=limit, filter_str=None)
            for c in chunks or []:
                text = str(c or "")
                if text:
                    items.append({"file_name": "", "text": text})

        logger.info(
            f"✅ [{self.tool_name}] Milvus 完成 | 策略：{strategy} | 条数：{len(items)}"
        )

        if not items:
            return "未找到相关结果"

        if self.rerank_enabled:
            reranked = self._apply_rerank(query=query, items=items)
            if reranked:
                items = reranked
                logger.info(f"✅ [{self.tool_name}] Rerank 完成，保留 {len(items)} 条")
            else:
                logger.warning(f"⚠️ [{self.tool_name}] Rerank 失败，使用原始结果")

        return items

    def search_as_string(
        self,
        query: str,
        limit: Optional[int] = None,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> str:
        """将 search() 结果格式化为字符串，每条附带来源文件名。"""
        result = self.search(query, limit, file_name_filters=file_name_filters)
        if isinstance(result, str):
            return result
        lines = [f"检索到 {len(result)} 条结果：", ""]
        for i, item in enumerate(result, 1):
            text = str(item.get("text", "") or "") if isinstance(item, dict) else str(item or "")
            file_name = str(item.get("file_name", "") or "") if isinstance(item, dict) else ""
            safe_chunk = clip_text(redact_sensitive(text), 450)
            source_label = f"[来源: {file_name}] " if file_name else ""
            lines.append(f"【{i}】{source_label}{safe_chunk}")
            lines.append("")
        return "\n".join(lines)

    def list_chunks_metadata(
        self,
        include_content: bool = False,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> str:
        """获取集合中文件名元数据列表（JSON 字符串）。"""
        logger.info(f"📦 [{self.tool_name}] 获取元数据 | 过滤：{file_name_filters}")
        data = self.milvus_tool.list_chunks_metadata(
            include_content=include_content,
            include_fields=None,
            filter_str=file_name_filters,
        )
        return json.dumps(data, ensure_ascii=False)

    def _apply_rerank(
        self, query: str, items: List[Dict[str, Any]]
    ) -> Optional[List[Dict[str, Any]]]:
        """DashScope Rerank API 重排序，失败时返回 None。"""
        payload: Dict[str, Any] = {}
        try:
            t0 = time.time()
            documents = [item["text"] for item in items]
            headers = {
                "Authorization": f"Bearer {self.rerank_api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.rerank_model,
                "input": {"query": query, "documents": documents},
                "parameters": {"top_n": self.rerank_top_k, "return_documents": True},
            }
            resp = requests.post(self.rerank_url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            response_data = resp.json()

            results = response_data.get("output", {}).get("results", [])
            if not results:
                return None

            reranked_items = []
            for result in results:
                index = result.get("index", 0)
                score = result.get("relevance_score", 0.0)
                if score < self.rerank_threshold:
                    continue
                doc = result.get("document", {})
                text = doc.get("text", "") if isinstance(doc, dict) else ""
                original = items[index].copy()
                original["text"] = text
                original["rerank_score"] = score
                reranked_items.append(original)

            reranked_items.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)

            elapsed_ms = int((time.time() - t0) * 1000)
            top_score = f"{reranked_items[0]['rerank_score']:.4f}" if reranked_items else "N/A"
            usage = response_data.get("usage", {})
            logger.info(
                f"📊 [{self.tool_name}] Rerank 统计 | "
                f"耗时：{elapsed_ms}ms | "
                f"{len(items)} → {len(reranked_items)} 条 | "
                f"最高分：{top_score} | Token：{usage.get('total_tokens', 0)}"
            )
            return reranked_items

        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ [{self.tool_name}] Rerank HTTP 错误 {e.response.status_code}: {e.response.text}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ [{self.tool_name}] Rerank 请求错误 {type(e).__name__}: {e}")
            return None
        except Exception as e:
            import traceback
            logger.error(f"❌ [{self.tool_name}] Rerank 异常 {type(e).__name__}: {e}\n{traceback.format_exc()}")
            return None


# ===========================================================================
# Toolkit 1：客服售后知识检索（kefushouhou）
# 无需 file_name 筛选，直接全库语义检索
# ===========================================================================

class KefuShouhouToolkit(Toolkit):
    """
    客服售后知识库检索 Toolkit（对接 kefushouhou 集合）。

    设计决策：该集合存储全局通用的售后处理知识（补发流程、退款流程、物流异常处理等），
    与具体项目无强绑定，因此无需 file_name 筛选，直接全库语义检索即可获得高质量结果。
    """

    def __init__(
        self,
        milvus_tool: MilvusSearchTool,
        config_loader: Optional[ConfigLoader] = None,
        **kwargs,
    ):
        self._core = SummaryRetrievalCore(
            milvus_tool=milvus_tool,
            config_loader=config_loader,
            tool_name="KefuShouhou",
        )
        super().__init__(
            name="kefu_shouhou_toolkit",
            tools=[self.search_kefu_shouhou_knowledge],
            **kwargs,
        )

    def search_kefu_shouhou_knowledge(
        self,
        query: str,
        limit: Optional[int] = 5,
    ) -> str:
        """
        从客服售后知识库中语义检索与工单主诉相关的处理流程与口径。

        适用场景：
          - 需要查询通用售后处理流程：补发、退款、换货、少发、物流异常、时效承诺
          - 需要了解常见售后问题的标准应对话术或操作步骤
          - 工单描述涉及具体售后诉求但不确定具体项目规范时

        ⚠️ 本工具直接全库语义检索，无需预先筛选 file_name。
        调用本工具前无需调用文件名列表工具，可直接传入 query 检索。

        Args:
            query (str):
                检索意图文本，建议 5-20 字，聚焦售后业务关键词。
                构造原则：从工单 title/desc 中提取核心诉求词。
                示例：
                  - "少发 补发 处理流程"
                  - "7天无理由退货 退款流程"
                  - "物流停滞 催件处理"
                  - "质量问题 换货标准"

            limit (Optional[int]):
                返回结果数量，默认 5，建议范围 3-8。

        Returns:
            str:
                格式化检索结果，每条格式为：
                  【序号】[来源: <file_name>] <内容摘要>
                - 无结果时返回："未找到相关结果"
                - 结果按相关性降序排列（启用 rerank 时按 rerank 分数排序）
        """
        return self._core.search_as_string(query=query, limit=limit, file_name_filters=None)


# ===========================================================================
# Toolkit 2：注意事项检索（zhuyishixiang1）
# 必须先 file_name 筛选（两阶段检索），因注意事项与项目强绑定
# ===========================================================================

class ZhuyishixiangToolkit(Toolkit):
    """
    注意事项知识库检索 Toolkit（对接 zhuyishixiang1 集合）。

    设计决策：该集合存储项目/客户特定的注意事项与强制规则，与具体项目强绑定。
    若不先筛选 file_name，会召回无关项目的规则，造成错误引导。
    因此必须采用两阶段检索：先 list 文件名 → 按项目筛选 → 再语义检索。
    """

    def __init__(
        self,
        milvus_tool: MilvusSearchTool,
        config_loader: Optional[ConfigLoader] = None,
        **kwargs,
    ):
        self._core = SummaryRetrievalCore(
            milvus_tool=milvus_tool,
            config_loader=config_loader,
            tool_name="Zhuyishixiang",
        )
        super().__init__(
            name="zhuyishixiang_toolkit",
            tools=[
                self.list_zhuyishixiang_file_names,
                self.search_zhuyishixiang_knowledge,
            ],
            **kwargs,
        )

    def list_zhuyishixiang_file_names(
        self,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> str:
        """
        【第一步】获取注意事项知识库的全量文件名列表。

        ⚠️ 调用 search_zhuyishixiang_knowledge 之前必须先调用本工具。
        注意事项与具体项目强绑定，必须通过文件名筛选定位到正确项目的规则。

        调用后的筛选逻辑：
          1. 从返回的文件名列表中，筛选包含以下关键词的文件名：
             - CORE_INFO 中的 customer_name（客户名称）
             - CORE_INFO 中的 project_name（项目名称）
             - CORE_INFO 中的 mall_name（商城名称）
          2. 将筛选出的文件名列表传入 search_zhuyishixiang_knowledge 的 file_name_filters
          3. 若无法从文件名中匹配到项目关键词，可不传 file_name_filters 进行全库兜底

        Args:
            file_name_filters (Optional[Union[str, List[str]]]):
                可选，仅拉取指定文件名的元数据。
                第一次调用时通常不传，获取全量文件名列表。

        Returns:
            str:
                JSON 字符串，结构为：
                  {{"unique_total_entities": N, "fields_name_list": ["文件名1", "文件名2", ...]}}
                重点关注 fields_name_list，从中筛选与当前项目相关的文件名。
        """
        return self._core.list_chunks_metadata(
            include_content=False,
            file_name_filters=file_name_filters,
        )

    def search_zhuyishixiang_knowledge(
        self,
        query: str,
        limit: Optional[int] = 5,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> str:
        """
        【第二步】从注意事项知识库中检索当前项目的特定规则与注意事项。

        ⚠️ 强烈建议先调用 list_zhuyishixiang_file_names 获取文件名列表，
        再从中筛选与当前 customer_name/project_name 相关的文件名传入 file_name_filters。
        直接跳过文件名筛选会导致召回无关项目的规则。

        适用场景：
          - 需要确认当前项目的特殊处理规范（投诉处理流程、补偿标准、特殊时效要求等）
          - 需要了解供应商责任判定规则
          - 工单涉及某客户/项目/商城的特定业务规则

        Args:
            query (str):
                检索意图文本，建议 5-20 字，聚焦规则/注意事项关键词。
                构造原则：结合工单主诉 + 注意事项场景词。
                示例：
                  - "投诉工单 处理规范"
                  - "补偿标准 特殊规则"
                  - "供应商责任 判定标准"
                  - "退款时效 特殊要求"

            limit (Optional[int]):
                返回结果数量，默认 5，建议范围 3-8。

            file_name_filters (Optional[Union[str, List[str]]]):
                文件名过滤条件，从 list_zhuyishixiang_file_names 返回的列表中筛选。
                支持字符串（单个）或列表（多个文件名）。
                强烈建议传入，不传则全库检索（可能召回无关项目规则）。

        Returns:
            str:
                格式化检索结果，每条格式为：
                  【序号】[来源: <file_name>] <内容摘要>
                - 无结果时返回："未找到相关结果"
                - 结果按相关性降序排列（启用 rerank 时按 rerank 分数排序）
        """
        return self._core.search_as_string(
            query=query,
            limit=limit,
            file_name_filters=file_name_filters,
        )


# ===========================================================================
# 工厂函数
# ===========================================================================

def create_summary_rag_toolkits(
    config_loader: ConfigLoader,
) -> List[Toolkit]:
    """
    为摘要 Agent 创建两个专用 RAG Toolkit。

    Args:
        config_loader: 已初始化的 ConfigLoader 实例

    Returns:
        [KefuShouhouToolkit, ZhuyishixiangToolkit]
        任一初始化失败则跳过该 Toolkit，不影响另一个和整体服务。
    """
    embedder_config = config_loader.get_embedding_config()
    toolkits: List[Toolkit] = []

    # Toolkit 1：客服售后知识库
    try:
        milvus_cfg_ks = config_loader.get_milvus_config_by_key("milvus_kefu_shouhou")
        milvus_tool_ks = create_milvus_tools(milvus_cfg_ks, embedder_config)
        toolkits.append(KefuShouhouToolkit(
            milvus_tool=milvus_tool_ks,
            config_loader=config_loader,
        ))
        logger.info("✅ [Summary RAG] KefuShouhouToolkit 初始化成功 (kefushouhou)")
    except Exception as e:
        logger.warning(f"⚠️ [Summary RAG] KefuShouhouToolkit 初始化失败，跳过\n{type(e).__name__}: {e}")

    # Toolkit 2：注意事项知识库
    try:
        milvus_cfg_zyx = config_loader.get_milvus_config_by_key("milvus_zhuyishixiang")
        milvus_tool_zyx = create_milvus_tools(milvus_cfg_zyx, embedder_config)
        toolkits.append(ZhuyishixiangToolkit(
            milvus_tool=milvus_tool_zyx,
            config_loader=config_loader,
        ))
        logger.info("✅ [Summary RAG] ZhuyishixiangToolkit 初始化成功 (zhuyishixiang1)")
    except Exception as e:
        logger.warning(f"⚠️ [Summary RAG] ZhuyishixiangToolkit 初始化失败，跳过\n{type(e).__name__}: {e}")

    return toolkits
