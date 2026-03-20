import re
import json

from typing import List, Union, Optional


def redact_sensitive(text: str) -> str:
    """
    脱敏敏感信息（手机号、身份证、密码、群聊名称等）
    """
    value = str(text or "")
    value = re.sub(r"\b1\d{10}\b", "[手机号已脱敏]", value)
    value = re.sub(r"\b\d{15,20}\b", "[编号已脱敏]", value)
    value = re.sub(r"(密码|pass(?:word)?)[：:\s]*[^\s，。;；\n\r]{1,64}", r"\1：[已脱敏]", value, flags=re.IGNORECASE)
    value = re.sub(r"(账号|account)[：:\s]*[^\s，。;；\n\r]{1,64}", r"\1：[已脱敏]", value, flags=re.IGNORECASE)
    return value


def _extract_json_str(text: str) -> str:
    """
    从 LLM 原始输出中提取 JSON 字符串。
    兼容以下格式：
    - 纯 JSON：{"suggestion": "..."}
    - Markdown 代码块：```json\n{...}\n```
    - JSON 前后有多余文本
    """
    text = text.strip()
    # 去掉 markdown 代码块标记
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # 已经是纯 JSON
    if text.startswith("{"):
        return text
    # 从文本中提取第一个完整 JSON 对象
    m = re.search(r"(\{[\s\S]+\})", text)
    if m:
        return m.group(1).strip()
    return text


def parse_suggestion(raw: str) -> str:
    """
    解析 Agent 返回的建议内容。
    兼容：纯 JSON、markdown 代码块、JSON 前后有多余文本。
    """
    text = str(raw or "").strip()
    if not text:
        return ""

    json_str = _extract_json_str(text)

    # 尝试完整 JSON 解析
    try:
        obj = json.loads(json_str)
        if isinstance(obj, dict):
            val = obj.get("suggestion") or ""
            if val:
                return str(val).strip()
    except Exception:
        pass

    # 正则兜底：直接从原始文本提取 suggestion 字段值（处理转义字符）
    m = re.search(r'"suggestion"\s*:\s*"((?:[^"]|\\")+)"', text)
    if m:
        try:
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            return m.group(1).strip()

    # 最后兜底：返回第一行非 JSON 标记的文本
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("{") and not line.startswith("```"):
            return line
    return ""


def parse_summary(raw: str) -> dict:
    """
    解析 Agent 返回的摘要内容。
    兼容：纯 JSON、markdown 代码块、平铺结构、嵌套 summary 结构。
    字段：question / info_summary / reviews
    兼容旧字段 status（向后兼容）。
    """
    _default = {"question": "无", "info_summary": "待确认", "reviews": "无"}
    text = str(raw or "").strip()
    if not text:
        return _default

    json_str = _extract_json_str(text)

    # 尝试完整 JSON 解析
    try:
        obj = json.loads(json_str)
        if isinstance(obj, dict):
            # 格式1：{"summary": {"question": ..., "info_summary": ..., "reviews": ...}}
            summary = obj.get("summary")
            if isinstance(summary, dict):
                return {
                    "question":     str(summary.get("question")     or "").strip() or "无",
                    "info_summary": str(summary.get("info_summary") or summary.get("status") or "").strip() or "待确认",
                    "reviews":      str(summary.get("reviews") or summary.get("review") or "").strip() or "无",
                }
            # 格式2：平铺 {"question": ..., "info_summary": ..., "reviews": ...}
            if "question" in obj or "info_summary" in obj or "status" in obj:
                return {
                    "question":     str(obj.get("question")     or "").strip() or "无",
                    "info_summary": str(obj.get("info_summary") or obj.get("status") or "").strip() or "待确认",
                    "reviews":      str(obj.get("reviews") or obj.get("review") or "").strip() or "无",
                }
    except Exception:
        pass

    # 正则兜底：逐字段提取
    compact = text.replace("\n", " ").replace("\r", " ").strip()

    def _pick(key: str) -> str:
        pattern = r'"' + re.escape(key) + r'"\s*:\s*"([^"]*)"'
        m = re.search(pattern, compact)
        return (m.group(1) or "").strip() if m else ""

    return {
        "question":     _pick("question") or "无",
        "info_summary": _pick("info_summary") or _pick("status") or "待确认",
        "reviews":      _pick("reviews") or _pick("review") or "无",
    }
