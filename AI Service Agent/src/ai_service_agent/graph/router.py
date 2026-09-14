import json
from typing import Callable, Literal

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from pydantic import BaseModel

from ai_service_agent.config import get_settings


class RouterSchema(BaseModel):
    capability: str
    risk_level: Literal["read", "mutation", "high_risk"]
    confidence: float


ROUTER_SYSTEM = (
    "你是客服意图路由器。只输出 JSON："
    '{"capability": "faq|order|account|smalltalk|unknown", '
    '"risk_level": "read|mutation|high_risk", "confidence": 0-1}。'
    "不要输出其它内容。"
)


def make_router(model) -> Callable[[dict], dict]:
    """生产用 create_agent + response_format；fake model 下解析其 JSON 文本。

    FakeMessagesListChatModel 无 bind_tools（会随 response_format 触发绑定失败），
    故仅对真实模型传 response_format；两条路径都经 RouterSchema 校验。
    """
    is_fake = isinstance(model, FakeMessagesListChatModel)
    kwargs = {} if is_fake else {"response_format": RouterSchema}
    agent = create_agent(model, tools=[], system_prompt=ROUTER_SYSTEM, **kwargs)

    def route(state: dict) -> dict:
        result = agent.invoke({"messages": state["messages"]})
        content = result["messages"][-1].content
        if isinstance(content, str):
            payload = json.loads(content)
        elif isinstance(content, dict):
            payload = content
        else:
            payload = RouterSchema.model_validate(content).model_dump()
        parsed = RouterSchema.model_validate(payload)
        conf = float(parsed.confidence)
        threshold = get_settings().router_conf_threshold
        return {
            "capability": parsed.capability if conf >= threshold else "unknown",
            "risk_level": parsed.risk_level,
            "confidence": conf,
            "execution_status": "routing",
        }

    return route
