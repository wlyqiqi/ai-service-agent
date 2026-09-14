from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """离线测试模型：FakeMessagesListChatModel 缺 bind_tools，
    create_agent 绑定工具 / response_format 时会失败，这里放行绑定。"""

    def bind_tools(self, tools, **kwargs):
        return self


def make_fake_model() -> ToolCallingFakeModel:
    """服务端 model='fake' 的默认离线模型（Router JSON + FAQ 回复）。"""
    return ToolCallingFakeModel(
        responses=[
            AIMessage(content='{"capability": "faq", "risk_level": "read", "confidence": 0.9}'),
            AIMessage(content="这是 FAQ 的离线回复（fake model）。"),
        ]
    )
