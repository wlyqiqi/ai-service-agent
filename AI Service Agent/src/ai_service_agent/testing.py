import json

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """离线测试模型：FakeMessagesListChatModel 缺 bind_tools，
    create_agent 绑定工具时会失败，这里放行绑定。"""

    def bind_tools(self, tools, **kwargs):
        return self


def router_json_message(capability: str, risk_level: str, confidence: float) -> AIMessage:
    """json_mode 契约（2026-09-15）：Router 输入必须是与真实模型
    response_format={"type":"json_object"} 输出一致的 AIMessage JSON 字符串；
    ToolStrategy 的 "Returning structured response" 描述性文本不被支持（router.py）。"""
    return AIMessage(
        content=json.dumps(
            {"capability": capability, "risk_level": risk_level, "confidence": confidence},
            ensure_ascii=False,
        )
    )


def make_fake_model() -> ToolCallingFakeModel:
    """服务端 model='fake' 的默认离线模型（Router json_mode 结构 + FAQ 回复）。"""
    return ToolCallingFakeModel(
        responses=[
            router_json_message("faq", "read", 0.9),
            AIMessage(content="这是 FAQ 的离线回复（fake model）。"),
        ]
    )


def select_json_message(tool: str, args: dict) -> AIMessage:
    """json_mode 契约（OQ-2）：select 节点输入必须是与真实模型 response_format json_object
    输出一致的 AIMessage JSON 字符串（{"tool": ..., "args": {...}}）。"""
    return AIMessage(content=json.dumps({"tool": tool, "args": args}, ensure_ascii=False))
