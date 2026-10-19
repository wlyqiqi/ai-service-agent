import json

from langchain_core.messages import AIMessage, ToolMessage

from ai_service_agent.graph.messages import read_tool_error


def _tool(name: str, content):
    return ToolMessage(content=content, name=name, tool_call_id=f"call-{name}")


def test_returns_error_from_json_payload():
    """ToolNode 用 json.dumps 序列化 dict 输出，生产路径始终是 JSON 字符串。"""
    messages = [_tool("refund_apply", json.dumps({"error": "order_locked"}))]
    assert read_tool_error(messages, "refund_apply") == "order_locked"


def test_parses_json_string_payload():
    messages = [_tool("refund_apply", '{"error": "order_locked", "order_id": "10086"}')]
    assert read_tool_error(messages, "refund_apply") == "order_locked"


def test_success_payload_returns_none():
    assert read_tool_error([_tool("refund_apply", '{"ok": true}')], "refund_apply") is None
    assert read_tool_error([_tool("refund_apply", "done")], "refund_apply") is None


def test_non_json_string_returns_none():
    assert read_tool_error([_tool("refund_apply", "not-json")], "refund_apply") is None


def test_ignores_other_tools_and_non_tool_messages():
    messages = [
        AIMessage(content="thinking"),
        _tool("update_phone", '{"error": "phone_invalid"}'),
    ]
    assert read_tool_error(messages, "refund_apply") is None


def test_reads_latest_matching_tool_message():
    messages = [
        _tool("refund_apply", '{"error": "old_error"}'),
        AIMessage(content="retry"),
        _tool("refund_apply", '{"ok": true}'),
    ]
    assert read_tool_error(messages, "refund_apply") is None


def test_empty_or_none_messages():
    assert read_tool_error([], "refund_apply") is None
    assert read_tool_error(None, "refund_apply") is None
