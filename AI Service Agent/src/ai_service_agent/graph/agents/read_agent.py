from typing import Annotated, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from ai_service_agent.graph.state import CustomerServiceState
from ai_service_agent.graph.allowed_tools import AllowedToolsMiddleware
from ai_service_agent.graph.fault_tolerance import (
    make_subgraph_error_handler,
)
from ai_service_agent.harness.tool_hooks import ToolHookMiddleware
from ai_service_agent.prompts import READ_SYSTEM_PROMPT
from ai_service_agent.graph.workflows.copy import RUNTIME_ERROR_TEXT

READ_MODEL_CALL_LIMIT = 3

class ReadAgentState(TypedDict):
    """create_agent 子图状态：messages + user_id（D4 属主校验注入所需，safety-spec §1.4）。"""
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str
    allowed_tools: list[str]

def make_read_graph(model, tools: list, tool_hooks=None):
    """ReadGraph：create_agent + 只读工具集（graph-spec.md §6.1），无业务 interrupt。
    传入 state_schema 使 ToolNode 能注入 user_id（InjectedState），属主校验留在工具层。"""
    middleware = [AllowedToolsMiddleware(), ModelCallLimitMiddleware(run_limit=READ_MODEL_CALL_LIMIT, exit_behavior="end")]
    if tool_hooks:
        middleware.append(ToolHookMiddleware(tool_hooks))
    agent = create_agent(model, tools=tools, system_prompt=READ_SYSTEM_PROMPT, state_schema=ReadAgentState, middleware=middleware)
    tools_node = ToolNode(tools)

    def prepare(state: dict) -> dict:
        return {"user_id": state.get("user_id", "")}

    def finalize(state: dict) -> dict:
        return {"execution_status": "completed"}

    def finalize_failed(state: dict) -> dict:
        return {"messages": [AIMessage(content=RUNTIME_ERROR_TEXT)], "execution_status": "failed"}

    builder = StateGraph(CustomerServiceState).set_node_defaults(
        error_handler=make_subgraph_error_handler("finalize_failed"),
    )
    builder.add_node("prepare", prepare)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools_node)
    builder.add_node("finalize", finalize)
    builder.add_node("finalize_failed", finalize_failed)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "agent")
    builder.add_conditional_edges(
        "agent", tools_condition, {"tools": "tools", "__end__": "finalize"}
    )
    builder.add_edge("tools", "agent")
    builder.add_edge("finalize", END)
    builder.add_edge("finalize_failed", END)
    return builder.compile()
