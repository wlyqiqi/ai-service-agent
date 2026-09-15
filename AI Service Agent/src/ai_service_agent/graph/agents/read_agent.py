from typing import Annotated, TypedDict

from langchain.agents import create_agent
from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from ai_service_agent.graph.state import CustomerServiceState

READ_SYSTEM_PROMPT = "你是客服智能助手。使用提供的信息回答用户问题。"


class ReadAgentState(TypedDict):
    """create_agent 子图状态：messages + user_id（D4 属主校验注入所需，safety-spec §1.4）。"""
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str


def make_read_graph(model, tools: list):
    """ReadGraph：create_agent + 只读工具集（graph-spec.md §6.1），无业务 interrupt。
    传入 state_schema 使 ToolNode 能注入 user_id（InjectedState），属主校验留在工具层。"""
    agent = create_agent(model, tools=tools, system_prompt=READ_SYSTEM_PROMPT, state_schema=ReadAgentState)
    tools_node = ToolNode(tools)

    def finalize(state: dict) -> dict:
        return {"execution_status": "completed"}

    builder = StateGraph(CustomerServiceState)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools_node)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent", tools_condition, {"tools": "tools", "__end__": "finalize"}
    )
    builder.add_edge("tools", "agent")
    builder.add_edge("finalize", END)
    return builder.compile()
