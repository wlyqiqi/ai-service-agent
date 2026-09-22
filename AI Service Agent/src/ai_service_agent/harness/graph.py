from ai_service_agent.config import make_model
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.harness.tool_hooks import LoggingToolHook
from ai_service_agent.tools import DEFAULT_TOOL_REGISTRY


def make_ai_graph(model, checkpointer, registry=None):
    """/chat 与 /handoffs/{id}/resolve 共用的图构建入口（P2 一致性要求）。

    两处必须调用本函数，保证 build_graph 参数（model / tools / checkpointer / registry）完全一致。
    tools 按 read 档从 ToolRegistry 注入（tool-spec.md §1）：ReadGraph 只拿到
    min_risk_level == "read" 的工具；mutation / high_risk 档由 build_graph 内部按 registry
    派生（P3 起 registry 传给 build_graph，两处参数永不漂移）。
    """
    reg = registry if registry is not None else DEFAULT_TOOL_REGISTRY
    tools = reg.for_risk_level("read")
    return build_graph(model, tools=tools, checkpointer=checkpointer, registry=reg, tool_hooks=[LoggingToolHook()])


# langgraph CLI / platform 入口（langgraph.json -> harness/graph.py:app）。
# 复用 make_ai_graph：与 /chat 同一构建路径，自动带默认 LoggingToolHook。
app = make_ai_graph(make_model(), None)
