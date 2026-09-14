from langchain.agents import create_agent
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from ai_service_agent.graph.state import CustomerServiceState

READ_SYSTEM_PROMPT = "你是客服智能助手。使用提供的信息回答用户问题。"


def make_read_graph(model, tools: list):
    """ReadGraph：create_agent + 只读工具集（graph-spec.md §6.1），无业务 interrupt。"""
    agent = create_agent(model, tools=tools, system_prompt=READ_SYSTEM_PROMPT)
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
