from typing import List

from pydantic import BaseModel, Field


class Suggestion(BaseModel):
    """工单回复建议"""
    content: str = Field(default="", description="建议内容")
    suggestion_sources: List[str] = Field(default_factory=list, description="建议来源")


class Summary(BaseModel):
    """工单内容总结"""
    info_summary: str = Field(default="待确认", description="工单信息总结")
    reviews: str = Field(default="无", description="注意事项罗列")
    summary_sources: List[str] = Field(default_factory=list, description="总结来源")


class QueryAnswer(BaseModel):
    """查询结果"""
    answer: str = Field(default="", description="查询结果")
    query_sources: List[str] = Field(default_factory=list, description="查询来源")