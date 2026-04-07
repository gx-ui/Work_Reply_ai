from typing import List

from pydantic import BaseModel, ConfigDict, Field


class ResponseBaseModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class Suggestion(ResponseBaseModel):
    """工单回复建议（序列化字段名与 /chat 接口及扩展一致：suggestion、knowledge_sources）"""
    content: str = Field(default="", description="建议内容", serialization_alias="suggestion")
    suggestion_sources: List[str] = Field(
        default_factory=list,
        description="建议来源",
        serialization_alias="knowledge_sources",
    )


class Summary(ResponseBaseModel):
    """工单内容总结"""
    info_summary: str = Field(default="待确认", description="工单信息总结")
    reviews: str = Field(default="无", description="注意事项罗列")
    summary_sources: List[str] = Field(default_factory=list, description="总结来源")


class QueryAnswer(ResponseBaseModel):
    """查询结果（序列化字段名与扩展一致：answer、sources）"""
    answer: str = Field(default="", description="查询结果")
    query_sources: List[str] = Field(
        default_factory=list,
        description="查询来源",
        serialization_alias="sources",
    )
