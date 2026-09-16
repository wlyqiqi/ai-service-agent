from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict


class KbSearchArgs(BaseModel):
    """kb_search 的 select 侧契约（P5 B1）。"""
    model_config = ConfigDict(extra="forbid")
    text: str
    request_id: str


@tool
def kb_search(text: str) -> str:
    """知识库检索（MVP stub）。"""
    return f"知识库检索结果：与「{text}」相关的内容（stub）。"
