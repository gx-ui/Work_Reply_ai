from pydantic import BaseModel, Field


class Suggestion(BaseModel):
    """工单回复建议"""
    content: str = Field(default="", description="建议内容")


class Summary(BaseModel):
    """工单内容总结"""
    question: str = Field(default="无", description="工单核心问题综述")
    info_summary: str = Field(default="待确认", description="工单信息总结")
    reviews: str = Field(default="无", description="注意事项罗列")
