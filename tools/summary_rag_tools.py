"""
summary_rag_tools.py

为摘要 Agent 专用的两个独立 RAG 检索 Toolkit：

1. KefuShouhouToolkit  - 对接 kefushouhou，无需 file_name 筛选
2. ZhuyishixiangToolkit - 对接 zhuyishixiang1，必须先 file_name 筛选

不含 Rerank，链路精简，响应更快。
"""

import json
import logging
from typing import Dict, Union, List, Optional, Any

from agno.tools import Toolkit
from config.config_loader import ConfigLoader
from tools.milvus_tool import create_milvus_tools, MilvusSearchTool

from utils.log_utils import record_tool_invocation


logger = logging.getLogger("summary_rag_tools")


class SummaryRetrievalCore:
    """
    摘要 Agent RAG 检索内核。
    封装 Milvus 检索逻辑，供两个专用 Toolkit 共享。
    不含 Rerank，链路精简，响应更快。
    """

    def __init__(self, milvus_tool: MilvusSearchTool, tool_name: str = "summary_retrieval_core"):
        self.milvus_tool = milvus_tool
        self.tool_name = tool_name

    def search(
        self,
        query: str,
        limit: Optional[int] = None,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> Union[List[Dict[str, Any]], str]:
        logger.info(
            "[%s] 检索开始\n查询: %r\nlimit: %s\n过滤: %s",
            self.tool_name,
            query,
            limit,
            file_name_filters,
        )
        items: List[Dict[str, Any]] = []
        strategy = "未知"

        if hasattr(self.milvus_tool, "search_with_metadata"):
            rows = []
            if file_name_filters:
                strategy = "file_name过滤搜索"
                rows = self.milvus_tool.search_with_metadata(query=query, limit=limit, filter_str=file_name_filters)
                if not rows:
                    logger.info("[%s] 过滤无结果，降级全量兜底", self.tool_name)
            if not rows:
                strategy = "全量兜底"
                rows = self.milvus_tool.search_with_metadata(query=query, limit=limit, filter_str=None)
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
                chunks = self.milvus_tool.search(query=query, limit=limit, filter_str=file_name_filters)
            if not chunks:
                strategy = "全量兜底"
                chunks = self.milvus_tool.search(query=query, limit=limit, filter_str=None)
            for c in chunks or []:
                text = str(c or "")
                if text:
                    items.append({"file_name": "", "text": text})

        logger.info("[%s] Milvus 完成 | 策略: %s | 条数: %s", self.tool_name, strategy, len(items))
        if not items:
            return "未找到相关结果"
        try:
            from services.agent_service import append_knowledge_sources

            fn_list = [
                str(it.get("file_name") or "").strip()
                for it in items
                if isinstance(it, dict) and str(it.get("file_name") or "").strip()
            ]
            if fn_list:
                append_knowledge_sources(fn_list)
        except Exception as e:
            logger.warning("摘要 RAG 写入知识来源失败: %s", e)
        return items

    def search_as_string(
        self,
        query: str,
        limit: Optional[int] = None,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> str:
        result = self.search(query, limit, file_name_filters=file_name_filters)
        if isinstance(result, str):
            return result
        lines = [f"检索到 {len(result)} 条结果：", ""]
        for i, item in enumerate(result, 1):
            text = str(item.get("text", "") or "") if isinstance(item, dict) else str(item or "")
            file_name = str(item.get("file_name", "") or "") if isinstance(item, dict) else ""
            safe_chunk = text
            source_label = f"[来源: {file_name}] " if file_name else ""
            lines.append(f"【{i}】{source_label}{safe_chunk}")
            lines.append("")
        return "\n".join(lines)

    def list_chunks_metadata(
        self,
        include_content: bool = False,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> str:
        logger.info("[%s] 获取元数据 | 过滤: %s", self.tool_name, file_name_filters)
        data = self.milvus_tool.list_chunks_metadata(
            include_content=include_content,
            include_fields=None,
            filter_str=file_name_filters,
        )
        return json.dumps(data, ensure_ascii=False)


class KefuShouhouToolkit(Toolkit):
    """
    客服售后知识库检索 Toolkit（对接 kefushouhou 集合）。
    全局通用知识，无需 file_name 筛选，直接全库语义检索。
    """

    def __init__(self, milvus_tool: MilvusSearchTool, config_loader: Optional[ConfigLoader] = None, **kwargs):
        self._core = SummaryRetrievalCore(milvus_tool=milvus_tool, tool_name="KefuShouhou")
        super().__init__(name="kefu_shouhou_toolkit", tools=[self.search_kefu_shouhou_knowledge], **kwargs)

    def search_kefu_shouhou_knowledge(self, query: str, limit: Optional[int] = 5) -> str:
        """
        从客服售后知识库中语义检索与工单主诉相关的处理流程与口径。

        ⚠️ 本工具直接全库语义检索，无需预先筛选 file_name，直接传入 query 即可。

        适用场景：
          - 通用售后处理流程：补发、退款、换货、少发、物流异常、时效承诺
          - 常见售后问题的标准应对话术或操作步骤

        Args:
            query (str): 检索意图，建议 5-20 字，聚焦售后业务关键词。
                示例："少发 补发 处理流程" / "7天无理由退货 退款流程" / "物流停滞 催件处理"
            limit (Optional[int]): 返回数量，默认 10，建议 8-15。

        Returns:
            str: 格式化结果，每条格式：【序号】[来源: file_name] 内容摘要
        """
        record_tool_invocation("kefu_shouhou_toolkit.search_kefu_shouhou_knowledge")
        return self._core.search_as_string(query=query, limit=limit, file_name_filters=None)


class ZhuyishixiangToolkit(Toolkit):
    """
    注意事项知识库检索 Toolkit（对接 zhuyishixiang1 集合）。
    注意事项与项目强绑定，必须两阶段检索：先 list 文件名 → 筛选 → 再语义检索。
    """

    def __init__(self, milvus_tool: MilvusSearchTool, config_loader: Optional[ConfigLoader] = None, **kwargs):
        self._core = SummaryRetrievalCore(milvus_tool=milvus_tool, tool_name="Zhuyishixiang")
        super().__init__(
            name="zhuyishixiang_toolkit",
            tools=[self.list_zhuyishixiang_file_names, self.search_zhuyishixiang_knowledge],
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
          2. 将筛选出的文件名传入 search_zhuyishixiang_knowledge 的 file_name_filters
          3. 若无法匹配，可不传 file_name_filters 全库兜底

        Args:
            file_name_filters: 可选，仅拉取指定文件名的元数据，第一次调用通常不传。

        Returns:
            str: JSON 字符串 {{"unique_total_entities": N, "fields_name_list": [...]}}
        """
        record_tool_invocation("zhuyishixiang_toolkit.list_zhuyishixiang_file_names")
        return self._core.list_chunks_metadata(include_content=False, file_name_filters=file_name_filters)

    def search_zhuyishixiang_knowledge(
        self,
        query: str,
        limit: Optional[int] = 5,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> str:
        """
        【第二步】从注意事项知识库中检索当前项目的特定规则与注意事项。

        ⚠️ 强烈建议先调用 list_zhuyishixiang_file_names 获取文件名列表，
        筛选与当前 customer_name/project_name 相关的文件名后传入 file_name_filters。
        跳过筛选会导致召回无关项目的规则。

        适用场景：
          - 当前项目的特殊处理规范（投诉流程、补偿标准、特殊时效要求等）
          - 供应商责任判定规则
          - 某客户/项目/商城的特定业务规则

        Args:
            query (str): 检索意图，建议 5-20 字，聚焦规则/注意事项关键词。
                示例："投诉工单 处理规范" / "补偿标准 特殊规则" / "供应商责任 判定标准"
            limit (Optional[int]): 返回数量，默认 10，建议 8-15。
            file_name_filters: 从 list_zhuyishixiang_file_names 筛选的文件名，强烈建议传入。

        Returns:
            str: 格式化结果，每条格式：【序号】[来源: file_name] 内容摘要
        """
        record_tool_invocation("zhuyishixiang_toolkit.search_zhuyishixiang_knowledge")
        return self._core.search_as_string(query=query, limit=limit, file_name_filters=file_name_filters)


def create_summary_rag_toolkits(config_loader: ConfigLoader) -> List[Toolkit]:
    """
    为摘要 Agent 创建两个专用 RAG Toolkit。
    任一初始化失败则跳过，不影响另一个和整体服务。
    """
    embedder_config = config_loader.get_embedding_config()
    toolkits: List[Toolkit] = []

    try:
        milvus_cfg_ks = config_loader.get_milvus_config_by_key("milvus_kefu_shouhou")
        milvus_tool_ks = create_milvus_tools(milvus_cfg_ks, embedder_config)
        toolkits.append(KefuShouhouToolkit(milvus_tool=milvus_tool_ks, config_loader=config_loader))
        logger.info("[Summary RAG] KefuShouhouToolkit 初始化成功")
    except Exception as e:
        logger.warning("[Summary RAG] KefuShouhouToolkit 初始化失败，跳过\n%s: %s", type(e).__name__, e)

    try:
        milvus_cfg_zyx = config_loader.get_milvus_config_by_key("milvus_zhuyishixiang")
        milvus_tool_zyx = create_milvus_tools(milvus_cfg_zyx, embedder_config)
        toolkits.append(ZhuyishixiangToolkit(milvus_tool=milvus_tool_zyx, config_loader=config_loader))
        logger.info("[Summary RAG] ZhuyishixiangToolkit 初始化成功")
    except Exception as e:
        logger.warning("[Summary RAG] ZhuyishixiangToolkit 初始化失败，跳过\n%s: %s", type(e).__name__, e)

    return toolkits
