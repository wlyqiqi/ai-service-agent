from ai_service_agent.graph.builder import build_graph

# MVP：AI 路径暂不注入业务工具；后续 P3/P4 按 capability x risk 从 ToolRegistry 取
AI_GRAPH_TOOLS: list = []


def make_ai_graph(model, checkpointer):
    """/chat 与 /handoffs/{id}/resolve 共用的图构建入口。

    两处必须调用本函数，保证 build_graph 参数（model / tools / checkpointer）
    完全一致，避免后续 Mutation/HighRisk 注入后 resume 行为漂移。
    """
    return build_graph(model, tools=AI_GRAPH_TOOLS, checkpointer=checkpointer)
