import logging

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import count_tokens_approximately, trim_messages

from ai_service_agent.graph.context.policy import ContextPolicy

logger = logging.getLogger(__name__)


class ContextBudgetMiddleware(AgentMiddleware):
    def __init__(self, policy: ContextPolicy):
        self.policy = policy

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        fixed_messages = (
            [request.system_message]
            if isinstance(request.system_message, SystemMessage)
            else []
        )
        fixed_tokens = count_tokens_approximately(fixed_messages, tools=request.tools)
        dynamic_budget = max(1, self.policy.max_total_tokens - fixed_tokens)
        messages = self.policy.build_messages(request.messages)
        selected = trim_messages(
            messages,
            strategy="last",
            max_tokens=dynamic_budget,
            token_counter=count_tokens_approximately,
            start_on="human",
            end_on=("human", "tool"),
            allow_partial=False,
        )
        selected_tokens = count_tokens_approximately(selected)
        logger.info(
            "context_policy_applied",
            extra={
                "policy": self.policy.name,
                "raw_messages": len(request.messages),
                "selected_messages": len(selected),
                "fixed_tokens": fixed_tokens,
                "dynamic_budget": dynamic_budget,
                "selected_tokens_estimated": selected_tokens,
                "trimmed": len(selected) < len(messages),
            },
        )
        return request.override(messages=selected)

    def wrap_model_call(self, request: ModelRequest, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request: ModelRequest, handler):
        return await handler(self._prepare(request))
