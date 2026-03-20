"""
Parent-Child 召回逻辑处理
当检索到 child 类型的文档时，找到对应的 parent 并返回 parent 的内容
"""
import logging
from typing import Dict, Any, List, Optional, Union
from pymilvus import Collection

logger = logging.getLogger(__name__)


class ParentChildRetrieval:
    """Parent-Child 召回处理器"""
    
    def __init__(self, collection_or_name: Union[Collection, str]):
        """
        初始化
        
        Args:
            collection_or_name: Milvus Collection 对象或集合名称
        """
        if isinstance(collection_or_name, str):
            # 如果是字符串，通过名称获取 Collection 对象
            from pymilvus import Collection
            self.collection = Collection(collection_or_name)
            self.collection.load()
        else:
            self.collection = collection_or_name
        self._parent_cache: Dict[str, Dict[str, Any]] = {} 
    
    def resolve_parent_content(
        self, 
        metadata: Dict[str, Any],
        current_content: str,
        current_id: Any
    ) -> tuple[str, Dict[str, Any]]:
        """
        解析 parent 内容
        
        Args:
            metadata: 当前检索结果的元数据
            current_content: 当前检索到的内容
            current_id: 当前检索到的ID
            
        Returns:
            tuple: (content, updated_metadata)
            - content: 如果是 child，返回 parent 的 content；否则返回原始 content
            - updated_metadata: 更新后的元数据（添加 parent 相关信息）
        """
        chunk_method = metadata.get('chunk_method', '')
        pc_type = metadata.get('pc_type', '')
        column = metadata.get('Column', '')
        
        # 如果不是 parent_child_split 类型，直接返回原始内容
        if chunk_method != 'parent_child_split' or pc_type != 'child':
            return current_content, metadata
        
        # 如果 Column 为空或无效，无法找到 parent，返回原始内容
        if not column or column == '':
            logger.warning(f"⚠️ [Parent-Child 检索] ID {current_id} 是 child 类型但 Column 为空，返回原始内容")
            return current_content, metadata
        
        # 尝试从缓存获取 parent（使用 Column 作为缓存 key）
        parent_data: Optional[Dict[str, Any]] = None
        if column in self._parent_cache:
            parent_data = self._parent_cache[column]
            logger.debug(f"✅ [Parent-Child 检索] 从缓存获取 parent，Column: {column}")
        else:
            # 查询 parent（通过 Column 字段关联）
            parent_data = self._find_parent_by_column(column)
            if parent_data:
                self._parent_cache[column] = parent_data
        
        if parent_data:
            parent_content = parent_data.get('content', '')
            parent_id = parent_data.get('id', '')
            
            logger.debug(f"✅ [Parent-Child 检索] 找到 parent (ID: {parent_id}, Column: {column})，使用 parent 内容替换 child 内容")
            
            # 更新元数据，添加 parent 信息
            updated_metadata = metadata.copy()
            updated_metadata['_is_parent_resolved'] = True
            updated_metadata['_child_id'] = current_id
            updated_metadata['_parent_id'] = parent_id
            updated_metadata['_parent_column'] = column
            updated_metadata['_original_child_content'] = current_content
            
            return parent_content, updated_metadata
        else:
            logger.warning(f"⚠️ [Parent-Child 检索] 未找到 Column {column} 对应的 parent，返回原始 child 内容")
            return current_content, metadata
    
    def _find_parent_by_column(self, column: str) -> Optional[Dict[str, Any]]:
        """
        根据 Column 字段查找 parent
        
        Args:
            column: parent 和 child 关联的 Column 值（UUID字符串）
            
        Returns:
            parent 的数据字典，包含 id, content, file_name 等字段
        """
        try:
            # 确保 column 不为空
            if not column or column == '':
                logger.warning("⚠️ [Parent-Child 检索] Column 为空，无法查询 parent")
                return None
            
            # 构建查询表达式：通过 Column 字段关联 parent
            # Column 字段是 VARCHAR 类型，需要用引号包裹
            expr = f'chunk_method == "parent_child_split" and pc_type == "parent" and Column == "{column}"'
            
            results = self.collection.query(
                expr=expr,
                limit=1,  # 每个 Column 应该只有一个 parent
                output_fields=['id', 'content', 'file_name', 'index', 'Column', 'pc_type', 'chunk_method']
            )
            
            if results and len(results) > 0:
                parent = results[0]
                logger.debug(f"✅ [Parent-Child 检索] 查询到 parent: ID={parent.get('id')}, Column={column}")
                return parent
            else:
                logger.warning(f"⚠️ [Parent-Child 检索] 未找到 Column {column} 的 parent")
                return None
                
        except Exception as e:
            logger.error(f"❌ [Parent-Child 检索] 查询 parent 失败 (Column={column}): {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    def resolve_multiple_items(
        self, 
        items: List[tuple[str, Dict[str, Any], float]]
    ) -> List[tuple[str, Dict[str, Any], float]]:
        """
        批量处理多个检索结果，解析 parent 内容
        
        处理逻辑：
        - 如果检索到 child 类型：查找对应的 parent，返回 parent 的 content
        - 如果检索到 structure 类型：直接返回检索到的 content（不做处理）
        
        Args:
            items: 检索结果列表，每个元素是 (content, metadata, distance)
            
        Returns:
            处理后的结果列表，child 的 content 已被替换为 parent 的 content
        """
        resolved_items = []
        
        for content, metadata, distance in items:
            chunk_method = metadata.get('chunk_method', '')
            pc_type = metadata.get('pc_type', '')
            item_id = metadata.get('id') or metadata.get('_id', '')
            
            if chunk_method == 'parent_child_split' and pc_type == 'child':
                # child 类型：需要查找 parent，返回 parent 的 content
                resolved_content, updated_metadata = self.resolve_parent_content(
                    metadata, content, item_id
                )
                resolved_items.append((resolved_content, updated_metadata, distance))
            elif chunk_method == 'parent_child_split' and pc_type == 'parent':
                resolved_items.append((content, metadata, distance))
            elif chunk_method == 'structure':
                # structure 类型：直接返回检索到的 content，不做任何处理
                resolved_items.append((content, metadata, distance))
            else:
                # 其他类型（理论上不应该出现，因为向量搜索已过滤）
                # 但为了安全，也直接返回
                logger.warning(f"⚠️ [Parent-Child 召回] 未预期的类型：chunk_method={chunk_method}, pc_type={pc_type}，直接返回")
                resolved_items.append((content, metadata, distance))
        
        return resolved_items


def create_parent_child_retrieval(collection_or_name: Union[Collection, str]) -> ParentChildRetrieval:
    """
    创建 Parent-Child 召回处理器
    
    Args:
        collection_or_name: Milvus Collection 对象或集合名称
        
    Returns:
        ParentChildRetrieval 实例
    """
    return ParentChildRetrieval(collection_or_name)
