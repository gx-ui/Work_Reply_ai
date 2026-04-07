from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class RequestBaseModel(BaseModel):
    """
    统一请求模型基类：
    1. 所有字符串字段自动去首尾空格
    """
    model_config = ConfigDict(str_strip_whitespace=True)


class WorksInfo(RequestBaseModel):
    """
    工单信息类：描述工单的上下文信息
    """
    title: str = Field(default="", description="工单标题")
    ticket_id: str = Field(default="", description="工单ID")
    desc: str = Field(..., description="工单直接描述的内容")
    history: List[Dict[str, Any]] = Field(default_factory=list, description="工单历史交互信息")
    priority: Optional[str] = Field(default=None, description="优先级")
    status: Optional[str] = Field(default=None, description="工单状态")




class CoreInfo(RequestBaseModel):
    """
    核心项目类：主要用于确定所属的项目
    通过此类以及+WorksInfo.title和+WorksInfo.desc确定所属的项目,以进行file_name过滤搜索知识库
    """
    customer_name: str = Field(default="", description="客户名称")
    project_name: str = Field(default="", description="项目名称")
    mall_name: str = Field(default="", description="商城名称")

class AttentionInfo(RequestBaseModel):
    """
    注意事项类：描述项目和供应商的注意事项
    主要用于给客服反馈注意事项
    """
    project_attention: str = Field(default="", description="项目注意事项")
    supplier_attention: str = Field(default="", description="供应商注意事项")

class QueryInfo(RequestBaseModel):
    """
    查询问题类：描述客服查询的问题
    """
    query: str = Field(default="", description="客服询问的问题")


class ChatRequest(RequestBaseModel):
    """
    聊天请求类：描述聊天请求的意图、会话ID、工单信息、核心项目信息、注意事项、客服查询问题
    """
    intent: Literal["suggestion", "summary", "query"] = Field(
        default="suggestion",
        description="请求意图：suggestion=回复建议，summary=工单摘要，query=知识库问答",
    )
    session_id: Optional[str] = Field(default=None, description="Agno 会话 ID，多轮对话时保持稳定")
    works_info: WorksInfo = Field(..., description="工单信息")
    core_info: CoreInfo = Field(default_factory=CoreInfo, description="核心项目信息")
    attention_info: AttentionInfo = Field(default_factory=AttentionInfo, description="注意事项")
    query_info: QueryInfo = Field(default_factory=QueryInfo, description="客服查询问题")

