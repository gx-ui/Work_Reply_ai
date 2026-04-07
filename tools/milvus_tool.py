"""
Milvus 向量检索工具模块
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, List, Optional, Union
from openai import OpenAI
from pymilvus import connections, Collection, DataType
from utils.parent_child_retrieval import create_parent_child_retrieval
from utils.milvus_utils import build_filter_expr, default_query_expr

logger = logging.getLogger("milvus_tool")
DEFAULT_CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "config", "cache_file_name.json")


def _unique_milvus_alias(collection_name: str, instance_id: int) -> str:
    """每实例独立别名，避免多集合/多 host 时共用 milvus_tool 互相 disconnect。"""
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in str(collection_name))[:48] or "default"
    return f"milvus_{safe}_{instance_id:x}"


def _pick_entity_source_name(entity: Any, primary_field: str) -> str:
    """从 Milvus entity 中取文档来源展示名，兼容 file_name / filename / source 等字段。"""
    if not entity:
        return ""
    keys_ordered: List[str] = []
    for k in (primary_field, "file_name", "filename", "source", "doc_name", "title", "name"):
        if k and k not in keys_ordered:
            keys_ordered.append(k)
    for k in keys_ordered:
        try:
            v = entity.get(k)
        except Exception:
            continue
        if v is None:
            continue
        s = str(v).replace("\ufeff", "").strip()
        if s:
            return s
    return ""


class MilvusSearchTool:
    """Milvus 向量检索工具"""
    
    def __init__(self, milvus_config: Dict[str, Any], embedder_config: Dict[str, Any]):
        """
        初始化 Milvus 检索工具
        
        Args:
            milvus_config: Milvus 配置
            embedder_config: Embedding 配置
        """
        self.milvus_config = milvus_config
        self.embedder_config = embedder_config
        self._parent_child_enabled = bool(self.milvus_config.get("parent_child_enabled", True))
        self._parent_child_resolver = None
        self._openai_client = OpenAI(
            base_url=self.embedder_config.get("base_url"),
            api_key=self.embedder_config.get("api_key")
        )

        collection_name = self.milvus_config.get("collection_name") or "default"
        self._connection_alias = _unique_milvus_alias(collection_name, id(self))
        cache_dir = os.path.join(os.path.dirname(__file__), "..", "config")
        self._cache_file = os.path.join(cache_dir, f"cache_{collection_name}.json")
        logger.info(f"[MilvusSearchTool] 集合：{collection_name}  缓存文件：{self._cache_file}")
        
        # 初始化连接
        self._init_connection()
    
    def _init_connection(self) -> None:
        """在当前线程注册 Milvus 连接（线程池 / asyncio.to_thread 下每条工作线程需各自建连）。

        pymilvus 连接是线程本地的：get_connection_addr 只判断别名是否在全局注册过，
        无法反映「当前线程」是否已绑定 handler，会误早退；_fetch_handler 与 Collection
        内部取连逻辑一致，当前线程无连接时会抛 ConnectionNotExistException，再 connect。
        （依赖 pymilvus.orm.connections._fetch_handler，升级大版本时请留意行为变化。）
        """
        host = self.milvus_config.get("host")
        port = self.milvus_config.get("port")
        db_name = self.milvus_config.get("db_name") or "default"
        try:
            connections._fetch_handler(self._connection_alias)
            return
        except Exception:
            pass
        connections.connect(
            alias=self._connection_alias,
            host=host,
            port=str(port),
            db_name=db_name,
        )

    def _get_collection(self) -> Collection:
        """获取并加载 Milvus Collection 实例"""
        collection_name = self.milvus_config.get("collection_name")
        if not collection_name:
            raise ValueError("Milvus 配置缺少 collection_name")
        self._init_connection()
        collection = Collection(collection_name, using=self._connection_alias)
        collection.load()
        return collection
    
    def list_chunks_metadata(
        self,
        include_content: bool = False,
        include_fields: Optional[List[str]] = ["file_name"],
        filter_str: Optional[Union[str, List[str]]] = None,
        cache_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        查询并返回去重后的文件名列表。

        优先从与当前集合绑定的缓存文件读取（cache_<collection_name>.json）。
        若缓存不存在，自动从 Milvus 查询并写入缓存，供后续复用。

        Args:
            include_content: 是否返回文档内容（已废弃，仅保留兼容性）
            include_fields: 指定返回字段（已废弃）
            filter_str: 按文件名过滤（支持单个/多个）
            cache_file: 显式指定缓存路径，None 时使用与集合绑定的路径

        Returns:
            包含 unique_total_entities 和 fields_name_list 的字典
        """
        # 优先使用显式传入路径，否则使用与集合绑定的专属缓存路径
        resolved_cache = cache_file if cache_file is not None else self._cache_file
        cache_path = Path(resolved_cache)

        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                all_file_names = data.get("fields_name_list", [])
                filtered_names = all_file_names
                if filter_str:
                    if isinstance(filter_str, str):
                        filter_str = [filter_str]
                    filtered_names = [
                        name for name in all_file_names
                        if any(f in name for f in filter_str)
                    ]
                logger.info(f"从缓存文件读取 {len(filtered_names)} 个 file_name（集合：{self.milvus_config.get('collection_name')}）")
                return {
                    "unique_total_entities": len(filtered_names),
                    "fields_name_list": filtered_names
                }
            except Exception as e:
                logger.warning(f"⚠️ 读取缓存文件失败，回退到 Milvus 查询：{e}")

        # 缓存不存在或读取失败：从 Milvus 查询
        result = self._list_chunks_from_milvus(include_content, include_fields, filter_str)

        # 仅在无 filter_str 时写入全量缓存（过滤结果不缓存）
        if not filter_str:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                logger.info(f"✅ 已写入缓存文件：{resolved_cache}（{result.get('unique_total_entities', 0)} 条）")
            except Exception as e:
                logger.warning(f"⚠️ 写入缓存文件失败（不影响检索）：{e}")

        return result
    
    def _list_chunks_from_milvus(
        self,
        include_content: bool = False,
        include_fields: Optional[List[str]] = ["file_name"],
        filter_str: Optional[Union[str, List[str]]] = None,
    ) -> Dict[str, Any]:
        """从 Milvus 查询元数据列表（回退方案）"""
        collection = self._get_collection()
        schema_field_names = [field.name for field in collection.schema.fields]
        sparse_vector_type = getattr(DataType, "SPARSE_FLOAT_VECTOR", None)
        vector_field_names = {
            field.name
            for field in collection.schema.fields
            if field.dtype in (DataType.FLOAT_VECTOR, DataType.BINARY_VECTOR) or (sparse_vector_type and field.dtype == sparse_vector_type)
        }
        output_field = self.milvus_config.get("output_field", "content")
        filter_field = self.milvus_config.get("filter_field", "file_name")
        base_fields: List[str]
        if isinstance(include_fields, str):
            include_fields = [include_fields]
        if include_fields:
            base_fields = [f for f in include_fields if f in schema_field_names and f not in vector_field_names]
        else:
            base_fields = [f for f in schema_field_names if f not in vector_field_names]
        if not include_content and output_field in base_fields:
            base_fields = [f for f in base_fields if f != output_field]
        if filter_field in schema_field_names and filter_field not in base_fields:
            base_fields.append(filter_field)


        expr = build_filter_expr(filter_str, field_name=filter_field) or default_query_expr(collection)
        
        items: List[Dict[str, Any]] = []
        batch_limit = int(self.milvus_config.get("list_limit", 1000))
        current_offset = 0
        while True:
            results = collection.query(
                expr=expr,
                output_fields=base_fields,
                limit=batch_limit,
                offset=current_offset,
            )
            if not results:
                break
            for row in results:
                item = dict(row)
                if not include_content and output_field in item:
                    item.pop(output_field, None)
                items.append(item)
            current_offset += len(results)
            if len(results) < batch_limit:
                break

        if filter_field in base_fields:
            seen_values = set()
            unique_items: List[Dict[str, Any]] = []
            for item in items:
                value = item.get(filter_field)
                if value in seen_values:
                    continue
                seen_values.add(value)
                unique_items.append(item)
            items = unique_items

        file_names = [item.get(filter_field, "") for item in items if item.get(filter_field)]
        
        filtered_names = file_names
        if filter_str:
            if isinstance(filter_str, str):
                filter_str = [filter_str]
            filtered_names = [
                name for name in file_names 
                if any(f in name for f in filter_str)
            ]
        
        logger.info(f"从 Milvus 查询返回 {len(filtered_names)} 个file_name文件名")
        return {
            "unique_total_entities": len(filtered_names),
            "fields_name_list": filtered_names,
        }

    def search_with_metadata(
        self,
        query: str,
        limit: int = None,
        filter_str: Optional[Union[str, List[str]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        检索 Milvus 数据库，返回包含元数据的结果
        
        Args:
            query: 查询文本
            limit: 返回结果数量
            filter_str: 过滤字符串 
        Returns:
            包含 text, file_name 等信息的字典列表
        """
        if limit is None:
            limit = self.milvus_config.get("limit", 10)
        
        response = self._openai_client.embeddings.create(
            model=self.embedder_config.get("model_name"),
            input=query,
            dimensions=self.milvus_config.get("dim", 2048)
        )
        
        query_vector = response.data[0].embedding
        
        collection_name = self.milvus_config.get("collection_name")
        self._init_connection()
        collection = Collection(collection_name, using=self._connection_alias)
        collection.load()

        search_params_config = self.milvus_config.get("search_params", {})
        metric_type = search_params_config.get("metric_type", "COSINE")
        search_params = {
            "metric_type": metric_type,
            "params": search_params_config.get("params", {"ef": 10})
        }
        
        output_field = self.milvus_config.get("output_field", "content")
        filter_field = self.milvus_config.get("filter_field", "file_name")
        chunk_method_field = str(self.milvus_config.get("parent_child_chunk_method_field", "chunk_method"))
        pc_type_field = str(self.milvus_config.get("parent_child_type_field", "pc_type"))
        relation_field = str(self.milvus_config.get("parent_child_relation_field", "Column"))
        schema_field_names = {field.name for field in collection.schema.fields}

  
        expr = build_filter_expr(filter_str, field_name=filter_field)
            
        if expr:
            if filter_field not in schema_field_names:
                raise ValueError(f"Milvus collection schema 中不存在 {filter_field} 字段，无法进行过滤检索")

        output_fields = [output_field, filter_field]
        if self._parent_child_enabled:
            for field in [chunk_method_field, pc_type_field, relation_field]:
                if field in schema_field_names and field not in output_fields:
                    output_fields.append(field)

        search_kwargs = {
            "data": [query_vector],
            "anns_field": "embedding",
            "param": search_params,
            "limit": limit,
            "output_fields": output_fields,
        }
        if expr:
            search_kwargs["expr"] = expr

        search_results = collection.search(**search_kwargs)
        
        # 3. 提取结果
        rows = []
        if search_results and len(search_results) > 0:
            for hit in search_results[0]:
                entity = hit.entity
                item = {
                    "text": entity.get(output_field, "") if entity else "",
                    "file_name": _pick_entity_source_name(entity, filter_field) if entity else "",
                    "score": hit.score,
                    "id": hit.id
                }
                if entity:
                    item[chunk_method_field] = entity.get(chunk_method_field, "")
                    item[pc_type_field] = entity.get(pc_type_field, "")
                    item[relation_field] = entity.get(relation_field, "")
                rows.append(item)
        if self._parent_child_enabled and rows:
            before_total = len(rows)
            before_child = sum(1 for r in rows if str(r.get(pc_type_field, "")) == "child")
            before_parent = sum(1 for r in rows if str(r.get(pc_type_field, "")) == "parent")
            rows = self._resolve_parent_child_rows(
                collection=collection,
                rows=rows,
                output_field=output_field,
                filter_field=filter_field,
                chunk_method_field=chunk_method_field,
                pc_type_field=pc_type_field,
                relation_field=relation_field,
            )
            after_total = len(rows)
            after_parent = sum(1 for r in rows if str(r.get(pc_type_field, "")) == "parent")
            logger.info(
                f"✅ [父子召回完成]\n"
                f"🔄 启用：{self._parent_child_enabled}\n"
                f"📊 召回前：总数={before_total} (child={before_child}, parent={before_parent})\n"
                f"📈 召回后：总数={after_total} (parent={after_parent})"
            )
        return rows

    def _resolve_parent_child_rows(
        self,
        collection: Collection,
        rows: List[Dict[str, Any]],
        output_field: str,
        filter_field: str,
        chunk_method_field: str,
        pc_type_field: str,
        relation_field: str,
    ) -> List[Dict[str, Any]]:
        try:
            if self._parent_child_resolver is None:
                self._parent_child_resolver = create_parent_child_retrieval(collection)
            items: List[tuple[str, Dict[str, Any], float]] = []
            for row in rows:
                metadata = {
                    "id": row.get("id"),
                    filter_field: row.get("file_name", ""),
                    chunk_method_field: row.get(chunk_method_field, ""),
                    pc_type_field: row.get(pc_type_field, ""),
                    relation_field: row.get(relation_field, ""),
                    "chunk_method": row.get(chunk_method_field, ""),
                    "pc_type": row.get(pc_type_field, ""),
                    "Column": row.get(relation_field, ""),
                }
                items.append((str(row.get("text", "")), metadata, float(row.get("score", 0.0) or 0.0)))
            resolved_items = self._parent_child_resolver.resolve_multiple_items(items)
            resolved_rows: List[Dict[str, Any]] = []
            seen_parent_keys = set()
            resolved_child_count = 0
            dedup_parent_count = 0
            sample_pairs: List[Dict[str, str]] = []
            for content, metadata, score in resolved_items:
                parent_key = metadata.get("_parent_column")
                if parent_key and parent_key in seen_parent_keys:
                    dedup_parent_count += 1
                    continue
                if parent_key:
                    seen_parent_keys.add(parent_key)
                if metadata.get("_is_parent_resolved"):
                    resolved_child_count += 1
                    if len(sample_pairs) < 3:
                        sample_pairs.append(
                            {
                                "child_id": str(metadata.get("_child_id") or ""),
                                "parent_id": str(metadata.get("_parent_id") or ""),
                                "file_name": str(metadata.get(filter_field) or ""),
                            }
                        )
                resolved_rows.append(
                    {
                        "text": content,
                        "file_name": metadata.get(filter_field, ""),
                        "score": score,
                        "id": metadata.get("_parent_id") or metadata.get("id"),
                        chunk_method_field: metadata.get(chunk_method_field, metadata.get("chunk_method", "")),
                        pc_type_field: metadata.get(pc_type_field, metadata.get("pc_type", "")),
                        relation_field: metadata.get(relation_field, metadata.get("Column", "")),
                    }
                )
            logger.info(
                f" [父子召回解析]\n"
                f" 输入：{len(rows)}\n"
                f" 输出：{len(resolved_rows)}\n"
                f" child 替换：{resolved_child_count}\n"
                f" 去重跳过：{dedup_parent_count}\n"
                f" 示例：{sample_pairs}"
            )
            return resolved_rows
        except Exception:
            logger.exception("❌ [父子召回解析失败] 解析失败，返回原始结果")
            return rows

    def search(
        self,
        query: str,
        limit: int = None,
        filter_str: Optional[Union[str, List[str]]] = None,
    ) -> List[str]:
        """
        检索 Milvus 数据库
        
        Args:
            query: 查询文本
            limit: 返回结果数量，默认使用配置中的 limit
            filter_str: 按 file_name 过滤（支持单个/多个）

        Returns:
            检索到的文本内容列表
        """
        rows = self.search_with_metadata(
            query=query,
            limit=limit,
            filter_str=filter_str,

        )
        chunks = []
        for row in rows or []:
            text = str(row.get("text", "") or "")
            if text:
                chunks.append(text)
        
        return chunks
    
    def __del__(self):
        """仅断开本实例别名。"""
        try:
            connections.disconnect(self._connection_alias)
        except Exception:
            pass


def create_milvus_tools(milvus_config: Dict[str, Any], embedder_config: Dict[str, Any]) -> MilvusSearchTool:
    """
    创建 Milvus 检索工具实例
    
    Args:
        milvus_config: Milvus 配置字典
        embedder_config: Embedding 配置字典
    
    Returns:
        MilvusSearchTool 实例
    """
    return MilvusSearchTool(milvus_config, embedder_config)

