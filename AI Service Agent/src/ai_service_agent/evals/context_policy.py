"""Context policy eval runner for golden model-input cases."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from ai_service_agent.config import Settings, get_settings
from ai_service_agent.graph.context import ContextBudgetMiddleware, ContextPolicy
from ai_service_agent.graph.context.policy import (
    make_fallback_policy,
    make_read_policy,
    make_router_policy,
    make_select_policy,
)


_REQUIRED_KEYS = {
    "id",
    "policy",
    "previous_completed_turns",
    "budget_tokens",
    "input_messages",
    "expected_behavior",
    "category",
}
_PREVIOUS_COMPLETED_TURNS = {
    "router": 1,
    "fallback": 2,
    "read": 2,
    "select": 1,
}


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    cases = [case for line in Path(path).read_text(encoding="utf-8").splitlines() if (case := line.strip())]
    parsed: list[dict[str, Any]] = []
    for line_number, case in enumerate(cases, 1):
        row = json.loads(case)
        missing = _REQUIRED_KEYS - row.keys()
        if missing:
            raise ValueError(f"context policy eval row {line_number} missing keys: {sorted(missing)}")
        if row["policy"] not in _PREVIOUS_COMPLETED_TURNS:
            raise ValueError(f"context policy eval row {line_number} has unknown policy: {row['policy']}")
        if row["previous_completed_turns"] != _PREVIOUS_COMPLETED_TURNS[row["policy"]]:
            raise ValueError(
                f"context policy eval row {line_number} has invalid previous_completed_turns"
            )
        parsed.append(row)

    ids = [row["id"] for row in parsed]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate context policy eval id")
    return parsed


def _messages_from_case(messages: list[dict[str, Any]]) -> list[BaseMessage]:
    converted: list[BaseMessage] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "human":
            converted.append(HumanMessage(content=message["content"]))
        elif role == "ai":
            converted.append(AIMessage(
                content=message["content"],
                tool_calls=message.get("tool_calls", []),
            ))
        elif role == "tool":
            converted.append(ToolMessage(
                content=message["content"],
                tool_call_id=message["tool_call_id"],
            ))
        else:
            raise ValueError(f"input message {index} has unknown role: {role}")
    return converted


def _message_to_dict(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, HumanMessage):
        return {"role": "human", "content": message.content}
    if isinstance(message, AIMessage):
        row: dict[str, Any] = {"role": "ai", "content": message.content}
        if message.tool_calls:
            row["tool_calls"] = message.tool_calls
        return row
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "content": message.content,
            "tool_call_id": message.tool_call_id,
        }
    raise TypeError(f"unsupported eval message type: {type(message).__name__}")


def _policy_for_case(case: dict[str, Any], settings: Settings) -> ContextPolicy:
    policies = {
        "router": make_router_policy(settings),
        "fallback": make_fallback_policy(settings),
        "read": make_read_policy(settings),
        "select": make_select_policy(settings),
    }
    policy = policies[case["policy"]]
    if case["budget_tokens"] == policy.max_total_tokens:
        return policy
    return replace(policy, max_total_tokens=case["budget_tokens"])


def _prepared_request(case: dict[str, Any], policy: ContextPolicy) -> ModelRequest:
    request = ModelRequest(
        model=FakeMessagesListChatModel(responses=[]),
        messages=_messages_from_case(case["input_messages"]),
        system_message=SystemMessage(content=case["system_prompt"]) if case.get("system_prompt") else None,
        tools=case.get("tools", []),
        state={},
    )
    middleware = ContextBudgetMiddleware(policy)
    return middleware.wrap_model_call(request, lambda prepared: prepared)


def _assert_expected_context(case: dict[str, Any], actual: list[BaseMessage]) -> None:
    expected = case.get("expected_dynamic_context")
    if expected is None:
        return
    actual_dicts = [_message_to_dict(message) for message in actual]
    if actual_dicts != expected:
        raise AssertionError(
            f"{case['id']} dynamic context mismatch:\nexpected={expected}\nactual={actual_dicts}"
        )


def _assert_constraints(case: dict[str, Any], prepared: ModelRequest) -> None:
    constraints = case.get("expected_dynamic_context_constraints")
    if not constraints:
        return

    total_tokens = count_tokens_approximately(
        [*( [prepared.system_message] if prepared.system_message else [] )],
        tools=prepared.tools,
    ) + count_tokens_approximately(prepared.messages)
    if total_tokens > case["budget_tokens"]:
        raise AssertionError(
            f"{case['id']} context budget exceeded: {total_tokens} > {case['budget_tokens']}"
        )

    messages = prepared.messages
    if "must_include_current_human" in constraints:
        expected = constraints["must_include_current_human"]
        if not any(isinstance(message, HumanMessage) and message.content == expected for message in messages):
            raise AssertionError(f"{case['id']} did not include current human message: {expected}")

    if constraints.get("must_exclude_older_turn"):
        first_human = next(message.content for message in messages if isinstance(message, HumanMessage))
        if first_human == "older turn":
            raise AssertionError(f"{case['id']} kept the older turn")

    if constraints.get("must_exclude_prior_tool_results") and any(
        isinstance(message, ToolMessage) for message in messages
    ):
        raise AssertionError(f"{case['id']} kept a prior tool result")

    tool_call_ids = {
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    }
    tool_result_ids = {
        message.tool_call_id
        for message in messages
        if isinstance(message, ToolMessage)
    }
    if "must_include_tool_call_id" in constraints:
        expected = constraints["must_include_tool_call_id"]
        if expected not in tool_call_ids:
            raise AssertionError(f"{case['id']} did not include tool call {expected}")
    if "must_include_tool_result_id" in constraints:
        expected = constraints["must_include_tool_result_id"]
        if expected not in tool_result_ids:
            raise AssertionError(f"{case['id']} did not include tool result {expected}")
    if constraints.get("must_not_split_tool_call_and_result"):
        if tool_call_ids != tool_result_ids:
            raise AssertionError(
                f"{case['id']} split tool call/result: calls={tool_call_ids}, results={tool_result_ids}"
            )


def evaluate_cases(cases: list[dict[str, Any]], settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    results: list[dict[str, Any]] = []
    for case in cases:
        policy = _policy_for_case(case, settings)
        prepared = _prepared_request(case, policy)
        try:
            _assert_expected_context(case, prepared.messages)
            _assert_constraints(case, prepared)
            result = {"id": case["id"], "status": "pass"}
        except (AssertionError, ValueError, TypeError) as exc:
            result = {"id": case["id"], "status": "fail", "error": str(exc)}
        results.append(result)
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(results),
        "pass": sum(result["status"] == "pass" for result in results),
        "fail": sum(result["status"] == "fail" for result in results),
    }


def run_dataset(path: str | Path, settings: Settings | None = None) -> dict[str, Any]:
    cases = load_cases(path)
    results = evaluate_cases(cases, settings)
    return {
        "dataset": str(path),
        "summary": summarize(results),
        "results": results,
    }
