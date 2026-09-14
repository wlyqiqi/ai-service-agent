from langchain_core.tools import tool


@tool
def kb_search(text: str) -> str:
    """知识库检索（MVP stub）。"""
    return f"知识库检索结果：与「{text}」相关的内容（stub）。"
