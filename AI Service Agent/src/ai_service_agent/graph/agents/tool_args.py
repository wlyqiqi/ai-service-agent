from langchain.agents.middleware import AgentMiddleware

from ai_service_agent.tools.params import normalize_absent_args


class ToolArgumentNormalizationMiddleware(AgentMiddleware):
    """把模型输出的占位哨兵（"null" 等）剥离，使「不筛选」等价于「不传该参数」。"""

    name = "tool_argument_normalization"

    def wrap_tool_call(self, request, handler):
        args = dict(request.tool_call.get("args") or {})
        new_args = normalize_absent_args(args)
        if new_args != args:
            return handler(request.override(tool_call={**request.tool_call, "args": new_args}))
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        args = dict(request.tool_call.get("args") or {})
        new_args = normalize_absent_args(args)
        if new_args != args:
            return await handler(request.override(tool_call={**request.tool_call, "args": new_args}))
        return await handler(request)
