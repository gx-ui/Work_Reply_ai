"""
Milvus 工具辅助函数
"""
import re
import json
from typing import Optional, Union, List, Tuple
from pymilvus import Collection, DataType

def extract_file_name_keywords(tags: Optional[Union[str, List[str]]]) -> List[str]:
    """从标签中提取文件名关键词"""
    parts: List[str] = []
    if tags:
        if isinstance(tags, str):
            parts.extend([t.strip() for t in tags.replace("、", ",").replace("，", ",").split(",") if t.strip()])
        else:
            parts.extend([str(t).strip() for t in tags if str(t).strip()])
    raw = " ".join(parts)
    if not raw:
        return []
    tokens = re.split(r"[\s,，、;；|/\\\n\r\t]+", raw)
    stop = {"工单", "标签", "项目", "系统", "群", "群聊", "交流群", "沟通群", "对接群", "客服", "售后", "反馈"}
    keywords: List[str] = []
    for tok in tokens:
        t = tok.strip()
        if not t:
            continue
        t = re.sub(r"[()（）\[\]【】{}<>《》\"'“”‘’]+", "", t).strip()
        if not t or t in stop:
            continue
        if len(t) == 1:
            continue
        if len(t) > 20:
            continue
        keywords.append(t)
    seen = set()
    dedup: List[str] = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            dedup.append(k)
    return dedup[:8]

def clip_text(text: str, max_len: int = 450) -> str:
    """截断文本"""
    value = str(text or "")
    if len(value) <= max_len:
        return value
    return value[:max_len] + "…"

def get_primary_key_field(collection: Collection) -> Optional[Tuple[str, DataType]]:
    """获取集合的主键字段"""
    for field in collection.schema.fields:
        if getattr(field, "is_primary", False):
            return field.name, field.dtype
    return None

def default_query_expr(collection: Collection) -> str:
    """生成默认查询表达式"""
    pk = get_primary_key_field(collection)
    if not pk:
        return "file_name != \"\""
    name, dtype = pk
    if dtype in (DataType.INT64, DataType.INT32, DataType.INT16, DataType.INT8):
        return f"{name} >= 0"
    return f"{name} != \"\""

def build_filter_expr(
    filter_str: Optional[Union[str, List[str]]],
    field_name: str = "file_name",
) -> Optional[str]:
    """构建模糊匹配过滤表达式（like）"""
    filter_values: List[str] = []
    if filter_str is None:
        filter_values = []
    elif isinstance(filter_str, str):
        value = filter_str.strip()
        if value:
            if value.startswith("[") and value.endswith("]"):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        filter_values = [str(item).strip() for item in parsed if str(item).strip()]
                    else:
                        filter_values = [value]
                except Exception:
                    filter_values = [value]
            else:
                filter_values = [value]
    else:
        filter_values = [str(item).strip() for item in filter_str if str(item).strip()]

    if not filter_values:
        return None

    expr_parts: List[str] = []
    for value in filter_values:
        v = value.strip()
        if (v.startswith("\"") and v.endswith("\"")) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1].strip()
        if not v:
            continue
        pattern = f"%{v}%"
        expr_parts.append(f"{field_name} like {json.dumps(pattern, ensure_ascii=False)}")

    if len(expr_parts) == 1:
        return expr_parts[0]
    return " or ".join(f"({part})" for part in expr_parts)
