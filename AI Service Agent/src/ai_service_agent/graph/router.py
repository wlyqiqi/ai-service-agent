import json
from typing import Callable, Literal

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from pydantic import BaseModel

from ai_service_agent.config import get_settings
from ai_service_agent.prompts import ROUTER_SYSTEM

class RouterSchema(BaseModel):
    capability: str
    risk_level: Literal["read", "mutation", "high_risk"]
    confidence: float

def _router_model(model):
    """json_mode 绑定（2026-09-15 阻断项修复）。

    真实模型绑定 response_format={"type": "json_object"}，让模型在消息 content 里直接
    返回 JSON 字符串；**不得**把 schema 交给 create_agent——那会走 ToolStrategy 工具包装，
    产出 "Returning structured response: ..." 的描述性 ToolMessage。该形状依赖结构化输出
    包装的实现细节，非稳定契约，模型版本升级可能再次破坏，且真实模型下解析 100% 失败。
    fake model 不绑定：testing.py 的 responses 即 json_mode 结构的 JSON 字符串
    （AIMessage(content=<json>)，与真实 json_object 输出结构一致），避免离线/真实结构漂移。
    """
    if isinstance(model, FakeMessagesListChatModel):
        return model
    return model.bind(response_format={"type": "json_object"})

def _parse_router_content(content) -> dict:
    """解析 Router 模型输出：只接受 JSON 字符串 / dict。

    ToolStrategy 的 "Returning structured response: ..." 描述性文本不是稳定契约，
    必须让 json.loads 显式失败（回归钉，见 test_router.py），不得新增解析分支。
    """
    if isinstance(content, str):
        return json.loads(content)
    if isinstance(content, dict):
        return content
    return RouterSchema.model_validate(content).model_dump()

def make_router(model) -> Callable[[dict], dict]:
    """生产用 create_agent + json_object 结构化输出；fake model 下解析其 JSON 文本。

    两条路径（真实 json_object / fake 直接 JSON 字符串）产出结构一致，均经 RouterSchema 校验。
    """
    agent = create_agent(_router_model(model), tools=[], system_prompt=ROUTER_SYSTEM)

    def route(state: dict) -> dict:
        result = agent.invoke({"messages": state["messages"]})
        payload = _parse_router_content(result["messages"][-1].content)
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
