from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_openai import ChatOpenAI

from ai_service_agent.config import (
    get_settings,
    make_fallback_model,
    make_model,
    make_mutation_select_model,
    make_router_fallback_model,
    make_router_model,
    make_select_model,
)
from ai_service_agent.domain.orders import SQLiteOrderRepository
from ai_service_agent.graph.builder import build_graph
from ai_service_agent.harness.tool_hooks import LoggingToolHook
from ai_service_agent.tools import make_tool_registry


def make_ai_graph(model, checkpointer, registry=None, order_repository=None):
    """/chat 与 /handoffs/{id}/resolve 共用的图构建入口（P2 一致性要求）。

    两处必须调用本函数，保证 build_graph 参数（model / tools / checkpointer / registry）完全一致。
    tools 按 read 档从 ToolRegistry 注入（tool-spec.md §1）：ReadGraph 只拿到
    min_risk_level == "read" 的工具；mutation / high_risk 档由 build_graph 内部按 registry
    派生（P3 起 registry 传给 build_graph，两处参数永不漂移）。

    缺省 registry 经 make_tool_registry(order_repository) 构建，high-risk 写工具委托
    OrderRepository（ADR 2026-10-10）。

    Fake / 非 ChatOpenAI：四档共用传入 model（单测零改动）。
    生产 ChatOpenAI：router / select / fallback 走对应工厂；model 作 read。
    """
    repository = order_repository or SQLiteOrderRepository(get_settings().mock_ecommerce_db_path)
    reg = registry if registry is not None else make_tool_registry(repository)
    tools = reg.for_risk_level("read")
    use_profiles = isinstance(model, ChatOpenAI) and not isinstance(
        model, FakeMessagesListChatModel
    )
    return build_graph(
        model,
        tools=tools,
        checkpointer=checkpointer,
        registry=reg,
        tool_hooks=[LoggingToolHook()],
        order_repository=repository,
        router_model=make_router_model() if use_profiles else model,
        router_fallback_model=make_router_fallback_model() if use_profiles else None,
        select_model=make_select_model() if use_profiles else model,
        mutation_select_model=make_mutation_select_model() if use_profiles else model,
        fallback_model=make_fallback_model() if use_profiles else model,
    )


# langgraph CLI / platform 入口（langgraph.json -> harness/graph.py:app）。
# 复用 make_ai_graph：与 /chat 同一构建路径，自动带默认 LoggingToolHook。
app = make_ai_graph(make_model(), None)
