import json
import time
import requests
from typing import Dict, Union, List, Optional, Any
from config.config_loader import ConfigLoader
from tools.milvus_tool import create_milvus_tools, MilvusSearchTool
from utils.milvus_utils import clip_text
from agno.tools import Toolkit
ContentDict = Dict[str, Union[str, Dict[str, str]]]
from utils.common import redact_sensitive
import logging
import sys

"""
⚠️ LLM 模型选择注意：
本文件将 RAG 链条封装为 Agno Toolkit 工具（KnowledgeRetrievalToolkit）。
Agent 自主决定是否调用工具，实测结论：
- qwen-plus / qwen-max 等全量模型：大概率会调用工具
- qwen-flash 等快速模型：几乎不调用工具（速度快但效果差）
建议生产环境使用全量模型以保证工具调用率。
"""


logger = logging.getLogger("rag_retrieval_tool")
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(asctime)s\n%(message)s\n' + '-' * 80))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class KnowledgeRetrievalTool:

    def __init__(
        self,
        milvus_tool: Optional[MilvusSearchTool] = None,
        config_loader: Optional[ConfigLoader] = None,
        config_path: Optional[str] = None
    ):
        """
        初始化预检索知识库工具

        Args:
            milvus_tool: 已创建的 MilvusSearchTool 实例，如果为 None 则自动创建
            config_loader: 已创建的 ConfigLoader 实例，如果为 None 则自动创建
            config_path: 配置文件路径，仅在 config_loader 为 None 时使用
        """
        if milvus_tool is None:
            if config_loader is None:
                if config_path is None:
                    config_loader = ConfigLoader()
                else:
                    config_loader = ConfigLoader(config_path)

            milvus_config = config_loader.get_milvus_config()
            embedder_config = config_loader.get_embedding_config()
            milvus_tool = create_milvus_tools(milvus_config, embedder_config)

        self.milvus_tool = milvus_tool
        self.config_loader = config_loader or (ConfigLoader(config_path) if config_path else None)

        # 加载 rerank 配置
        self.rerank_config = self.config_loader.get_rerank_config() if (self.config_loader and hasattr(self.config_loader, 'get_rerank_config')) else {}
        self.rerank_enabled = self.rerank_config.get("enabled", False)
        if self.rerank_enabled:
            self.rerank_api_key = self.rerank_config.get("api_key", "")
            self.rerank_model = self.rerank_config.get("model_name", "gte-rerank-v2")
            self.rerank_top_k = self.rerank_config.get("top_k", 3)
            self.rerank_threshold = self.rerank_config.get("threshold", 0.3)
            self.rerank_url = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
            logger.info(f"✅ [Rerank] 已启用 - 模型：{self.rerank_model}, top_k: {self.rerank_top_k}, threshold: {self.rerank_threshold}")
        else:
            logger.info("ℹ️ [Rerank] 未启用，将跳过 rerank 步骤")

    def search(
        self,
        query: str,
        limit: Optional[int] = None,
        file_name_filters: Optional[Union[str, List[str]]] = None,
        tags: Optional[Union[str, List[str]]] = None,
    ) -> Union[List[Dict[str, Any]], str]:
        """
        执行 Milvus 检索，两级兜底策略：
        1. 优先用 file_name_filters 做过滤语义检索
        2. 若无结果则降级为全库语义检索

        Returns:
            List[dict] 包含 file_name + text，或字符串 "未找到相关结果"
        """
        logger.info(
            f"🔍 [检索开始]\n"
            f"❓ 查询：'{query}'\n"
            f"📊 限制：{limit}\n"
            f"📁 文件过滤：{file_name_filters}\n"
            f"🏷️ 标签：{tags}"
        )

        items: List[Dict[str, Any]] = []
        strategy = "未知"

        if hasattr(self.milvus_tool, "search_with_metadata"):
            rows = []
            if file_name_filters:
                strategy = "file_name过滤搜索"
                rows = self.milvus_tool.search_with_metadata(query=query, limit=limit, filter_str=file_name_filters)
                if not rows:
                    logger.info(f"⚠️ [检索警告] file_name过滤无结果，降级全量兜底\n📁 过滤器：{file_name_filters}")
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

        preview = [
            {
                "file_name": str(it.get("file_name", "") or "")[:200],
                "text_preview": clip_text(redact_sensitive(str(it.get("text", "") or "")), 160),
            }
            for it in items[:5]
        ]
        logger.info(
            f"✅ [Milvus 检索完成]\n"
            f"📈 策略：{strategy}\n"
            f"📊 条数：{len(items)}\n"
            f"👀 预览：{preview}"
        )

        if not items:
            return "未找到相关结果"

        # Rerank 重排序
        if self.rerank_enabled:
            reranked = self._apply_rerank(query=query, items=items)
            if reranked:
                items = reranked
                logger.info(f"✅ [Rerank 完成] 重排序后保留 {len(items)} 条结果")
            else:
                logger.warning("⚠️ [Rerank 失败] 使用原始检索结果")

        return items

    def _apply_rerank(self, query: str, items: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        """
        使用 DashScope Rerank API 对检索结果进行重排序。
        失败时返回 None，调用方降级使用原始结果。
        """
        payload: Dict[str, Any] = {}
        try:
            t0 = time.time()
            documents = [item["text"] for item in items]
            logger.info(f"🔄 [Rerank 开始] 模型：{self.rerank_model}, 候选数：{len(items)}")

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
                logger.warning("⚠️ [Rerank 警告] 返回结果为空")
                return None

            reranked_items = []
            for result in results:
                index = result.get("index", 0)
                score = result.get("relevance_score", 0.0)
                if score < self.rerank_threshold:
                    logger.info(f"⚠️ [Rerank 过滤] 索引 {index}, 分数 {score:.4f} < 阈值 {self.rerank_threshold}")
                    continue
                doc = result.get("document", {})
                text = doc.get("text", "") if isinstance(doc, dict) else ""
                original = items[index].copy()
                original["text"] = text
                original["rerank_score"] = score
                reranked_items.append(original)

            reranked_items.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)

            elapsed_ms = int((time.time() - t0) * 1000)
            usage = response_data.get("usage", {})
            top_score = f"{reranked_items[0]['rerank_score']:.4f}" if reranked_items else "N/A"
            logger.info(
                f"📊 [Rerank 统计]\n"
                f"⏱️ 耗时：{elapsed_ms}ms\n"
                f"📈 输入：{len(items)} 条 → 输出：{len(reranked_items)} 条\n"
                f"💰 消耗 Token: {usage.get('total_tokens', 0)}\n"
                f"🏆 最高分：{top_score}"
            )
            return reranked_items

        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ [Rerank HTTP 错误] {e.response.status_code}: {e.response.text}")
            logger.error(f"请求 payload: {json.dumps(payload, ensure_ascii=False)}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ [Rerank 请求错误] {type(e).__name__}: {e}")
            return None
        except Exception as e:
            import traceback
            logger.error(f"❌ [Rerank 异常] {type(e).__name__}: {e}\n{traceback.format_exc()}")
            return None

    def search_as_string(
        self,
        query: str,
        limit: Optional[int] = None,
        file_name_filters: Optional[Union[str, List[str]]] = None,
        tags: Optional[Union[str, List[str]]] = None,
    ) -> str:
        """将 search() 结果格式化为字符串，每条结果附带来源文件名。"""
        logger.info(
            f"🤖 [Toolkit 调用] search_as_string\n"
            f"❓ 查询：'{query}'\n"
            f"📊 限制：{limit}\n"
            f"📁 文件过滤：{file_name_filters}"
        )
        result = self.search(query, limit, file_name_filters=file_name_filters, tags=tags)
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
        include_fields: Optional[List[str]] = None,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> str:
        logger.info(
            f"📦 [获取file_name元数据]\n"
            f"📝 包含内容：{include_content}\n"
            f"📋 包含字段：{include_fields}\n"
            f"📁 文件过滤：{file_name_filters}"
        )
        data = self.milvus_tool.list_chunks_metadata(
            include_content=include_content,
            include_fields=include_fields,
            filter_str=file_name_filters,
        )
        return json.dumps(data, ensure_ascii=False)


def create_knowledge_retrieval_tool(
    config_path: Optional[str] = None,
    milvus_tool: Optional[MilvusSearchTool] = None
) -> KnowledgeRetrievalTool:
    """创建 KnowledgeRetrievalTool 实例。"""
    if milvus_tool is not None:
        return KnowledgeRetrievalTool(milvus_tool=milvus_tool)
    return KnowledgeRetrievalTool(config_path=config_path)


class KnowledgeRetrievalToolkit(Toolkit):

    def __init__(
        self,
        config_path: Optional[str] = None,
        milvus_tool: Optional[MilvusSearchTool] = None,
        config_loader: Optional[ConfigLoader] = None,
        enable_search: bool = True,
        enable_prefetch: bool = True,
        **kwargs
    ):
        self.retrieval_tool = KnowledgeRetrievalTool(
            milvus_tool=milvus_tool,
            config_loader=config_loader,
            config_path=config_path
        )
        tools = []
        if enable_search:
            tools.append(self.search_knowledge_base)
        if enable_prefetch:
            tools.append(self.list_knowledge_base_chunks_metadata)
        super().__init__(name="knowledge_retrieval_toolkit", tools=tools, **kwargs)

    def search_knowledge_base(
        self,
        query: str,
        limit: Optional[int] = 5,
        tags: Optional[Union[str, List[str]]] = None,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> str:
        """
        【两阶段检索 - 第二步】从客服知识库语义检索与工单相关的知识片段，用于生成回复建议。

        ⚠️ 标准调用流程（两阶段）：
          1. 先调用 list_knowledge_base_chunks_metadata 获取知识库全量文件名列表
          2. 从列表中筛选 3-5 个最相关文件名传入 file_name_filters
             筛选优先级：
               a. 文件名含 CORE_INFO 中的 customer_name / project_name / mall_name 关键词
               b. 文件名含与工单主诉匹配的场景词（售后/补发/退款/质检/后台操作）
          3. 若无法筛选出明确文件，不传 file_name_filters，执行全库语义检索兜底

        ✅ 必须调用的场景：
          - 涉及业务流程：退款、补发、少发、换货、物流异常、质检标准、后台操作步骤
          - 需确认项目特定口径：某 customer_name / project_name / mall_name 的特殊处理规范
          - WORKS_INFO 的 desc 包含具体订单号/商品名称且需给出处理建议
          - CUSTOM_INPUT 中客服明确要求查询或核实某项信息

        ❌ 不需要调用的场景：
          - 纯告知类回复（如"已收到工单，请耐心等待"）
          - WORKS_INFO 的 title/desc 均为空或极度缺失，无法构造有效 query
          - HISTORY 已完整描述处理结论且当前无新诉求

        Args:
            query (str):
                检索意图文本，要求简短精确（建议 5-20 字）。
                构造原则：
                  - 主体：从 WORKS_INFO 的 title/desc 提取核心意图词
                    （少发/补发/退款/退换货/物流异常/质量问题/时效等）
                  - 补充：CUSTOM_INPUT 中的关键词
                  - 禁止：将 customer_name/project_name 放入 query
                    （这些应作为 file_name_filters 使用）
                示例：
                  - title="少发宝矿力" → query="少发 补发 处理流程"
                  - desc="7天无理由退货退款" → query="7天无理由退货 退款流程"
                  - desc="物流停滞超48小时" → query="物流异常 停滞 催件处理"

            limit (Optional[int]):
                返回结果数量，默认 5，建议范围 3-10。
                query 越具体可设小值（3-5），越泛可设大值（8-10）。

            file_name_filters (Optional[Union[str, List[str]]]):
                文件名过滤条件，从 list_knowledge_base_chunks_metadata 返回的文件名中筛选。
                支持字符串（单个）或列表（多个）。

        Returns:
            str:
                格式化检索结果，每条格式为：
                  【序号】[来源: <file_name>] <内容摘要>
                - 无结果时返回："未找到相关结果"
                - 结果按相关性降序排列（启用 rerank 时按 rerank 分数排序）
        """
        return self.retrieval_tool.search_as_string(query, limit, tags=tags, file_name_filters=file_name_filters)

    def list_knowledge_base_chunks_metadata(
        self,
        include_content: bool = False,
        file_name_filters: Optional[Union[str, List[str]]] = None,
    ) -> str:
        """
        【两阶段检索 - 第一步】获取知识库中所有文件名列表，用于决策 file_name_filters。

        这是两阶段检索的探路步骤：
          1. 调用本工具获取知识库全量文件名
          2. 根据 CORE_INFO（customer_name/project_name/mall_name）和主诉类型
             从文件名列表中筛选 3-5 个最相关的文件名
          3. 将筛选结果作为 file_name_filters 传入 search_knowledge_base

        筛选文件名的判断逻辑：
          - 优先选文件名包含项目/客户/商城名称关键词的文件
            （如含"南网""NFDW""工行""中信""TCL"等实体词）
          - 其次选文件名包含业务场景词的文件
            （如含"售后""补发""退款""质检""后台操作""常见问题"等）
          - 若无法判断，则不传 file_name_filters，由 search_knowledge_base 全库兜底

        Args:
            include_content (bool):
                是否在返回结果中包含 chunk 的完整文本内容，默认 False。
                通常不需要开启，文件名列表已足够用于筛选决策。
            file_name_filters (Optional[Union[str, List[str]]]):
                可选，仅拉取指定文件名的元数据（支持单个或多个文件名）。
                一般第一步调用时不传，获取全量文件名列表。

        Returns:
            str:
                JSON 字符串，结构为：
                  {"items": [{"file_name": "xxx", ...}, ...]}
                重点关注每条记录的 file_name 字段，用于后续筛选。
        """
        return self.retrieval_tool.list_chunks_metadata(
            include_content=include_content,
            include_fields=None,
            file_name_filters=file_name_filters,
        )


def create_knowledge_retrieval_toolkit(
    config_path: Optional[str] = None,
    milvus_tool: Optional[MilvusSearchTool] = None,
    enable_search: bool = True,
    enable_prefetch: bool = True,
    **kwargs
) -> KnowledgeRetrievalToolkit:
    """创建 KnowledgeRetrievalToolkit 实例（可直接挂载到 Agno Agent）。"""
    return KnowledgeRetrievalToolkit(
        milvus_tool=milvus_tool,
        config_path=config_path,
        enable_search=enable_search,
        enable_prefetch=enable_prefetch,
        **kwargs
    )
