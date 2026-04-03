from typing import List, Dict, Any, Optional, Literal
from pydantic import BaseModel, Field


class WorksInfo(BaseModel):
    """
    工单信息类：描述工单的上下文信息
    """
    title: str = Field(default="", description="工单标题")
    ticket_id: str = Field(default="", description="工单ID")
    desc: str = Field(..., description="工单直接描述的内容")
    tags: List[str] = Field(default_factory=list, description="工单标签列表")
    history: List[Dict[str, Any]] = Field(default_factory=list, description="工单历史交互信息")
    priority: Optional[str] = Field(default=None, description="优先级")
    status: Optional[str] = Field(default=None, description="工单状态")

class CoreInfo(BaseModel):
    """
    核心项目类：主要用于确定所属的项目
    通过此类以及+WorksInfo.title和+WorksInfo.desc确定所属的项目,以进行file_name过滤搜索知识库
    """
    customer_name: str = Field(default="", description="客户名称")
    project_name: str = Field(default="", description="项目名称")
    mall_name: str = Field(default="", description="商城名称")

class AttentionInfo(BaseModel):
    """
    注意事项类：描述项目和供应商的注意事项
    主要用于给客服反馈注意事项
    """
    project_attention: str = Field(default="", description="项目注意事项")
    supplier_attention: str = Field(default="", description="供应商注意事项")

class QueryInfo(BaseModel):
    """
    查询问题类：描述客服查询的问题
    """
    query: str = Field(default="", description="客服询问的问题")


class ChatRequest(BaseModel):
    intent: Literal["suggestion", "summary", "query", "auto"] = Field(default="auto", description="请求意图")
    session_id: Optional[str] = Field(default=None, description="Agno 会话 ID，多轮对话时保持稳定")
    works_info: WorksInfo = Field(..., description="工单信息")
    core_info: CoreInfo = Field(default_factory=CoreInfo, description="核心项目信息")
    attention_info: AttentionInfo = Field(default_factory=AttentionInfo, description="注意事项")
    query_info: QueryInfo = Field(default_factory=QueryInfo, description="客服查询问题")
