"""知识库检索（read 档）：调用 LightRAG-chengyuan /query 获取检索上下文与引用来源。

OQ 定案：mode=naive（低延迟）；回复文本内引用（不改 event-spec）；不加缓存；不做自动同步。
降级：LightRAG 不可达 -> 明确告知；无结果 -> 如实告知。确定性、只读、无副作用。
"""

from typing import Annotated

import logging

import ai_service_agent.tools.lightrag as _lightrag
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class KbSearchArgs(BaseModel):
    """kb_search 的 select 侧契约（P5 B1）。"""
    model_config = ConfigDict(extra="forbid")
    text: str
    request_id: str


@tool
def kb_search(text: Annotated[str, "用户的完整问题原文，保留原始表述，不要改写或摘要"]) -> str:
    """检索澄元智能家电企业知识库（产品参数、使用方法、售后与政策等）。
    仅依据返回内容作答，不得编造；有参考来源时在回答中标注来源文件名。
    参数：text = 用户问题原文（勿改写摘要）。
    """

    result = _lightrag.get_lightrag_client().query_context(text)
    if metadata := result.get("metadata"):
        logger.info("kb_search retrieval metadata: %s", metadata)
    if "error" in result:
        return "知识库暂不可用，请稍后再试或联系人工客服。"
    if not result.get("context"):
        return "未查询到相关知识，请换个说法或联系人工客服。"
    sources = "、".join(r["file_path"] for r in (result.get("references") or []) if r.get("file_path"))
    if sources:
        return f"检索结果：\n{result['context']}\n\n参考来源：{sources}"
    return f"检索结果：\n{result['context']}"
