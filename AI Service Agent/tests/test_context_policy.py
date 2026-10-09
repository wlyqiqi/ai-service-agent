from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ai_service_agent.graph.context.builders import (
    build_read_context,
    build_recent_visible_context,
    strip_reasoning,
)


def test_strip_reasoning_removes_think_block():
    assert strip_reasoning("<think>secret</think>visible") == "visible"


def test_recent_visible_context_keeps_previous_turn_and_current_user():
    messages = [
        HumanMessage(content="我要修改地址"),
        AIMessage(content="请提供订单号和新地址"),
        HumanMessage(content="ORD-2026-10099"),
    ]

    actual = build_recent_visible_context(messages, previous_completed_turns=1)

    assert [message.content for message in actual] == [
        "我要修改地址",
        "请提供订单号和新地址",
        "ORD-2026-10099",
    ]


def test_recent_visible_context_excludes_tool_messages_and_tool_calls():
    messages = [
        HumanMessage(content="查询订单"),
        AIMessage(
            content="",
            tool_calls=[{"name": "order_lookup", "args": {}, "id": "call-1", "type": "tool_call"}],
        ),
        ToolMessage(content='{"order_id": "ORD-1"}', tool_call_id="call-1"),
        AIMessage(content="订单已发货"),
        HumanMessage(content="帮我取消"),
    ]

    actual = build_recent_visible_context(messages, previous_completed_turns=1)

    assert [(type(message).__name__, message.content) for message in actual] == [
        ("HumanMessage", "查询订单"),
        ("AIMessage", "订单已发货"),
        ("HumanMessage", "帮我取消"),
    ]


def test_read_context_preserves_current_turn_tool_chain():
    messages = [
        HumanMessage(content="上一轮"),
        AIMessage(content="上一轮回复"),
        HumanMessage(content="查询知识库"),
        AIMessage(
            content="",
            tool_calls=[{"name": "kb_search", "args": {}, "id": "call-read", "type": "tool_call"}],
        ),
        ToolMessage(content="knowledge base evidence", tool_call_id="call-read"),
    ]

    actual = build_read_context(messages, previous_completed_turns=1)

    assert actual[0].content == "上一轮"
    assert actual[1].content == "上一轮回复"
    assert actual[2].content == "查询知识库"
    assert actual[3].tool_calls[0]["id"] == "call-read"
    assert actual[4].content == "knowledge base evidence"


def test_recent_visible_context_keeps_all_ai_messages_in_a_turn():
    messages = [
        HumanMessage(content="我要转人工"),
        AIMessage(content="已转人工，坐席将尽快回复"),
        AIMessage(content="坐席已处理地址"),
        AIMessage(content="坐席已处理退款"),
        HumanMessage(content="你好"),
    ]

    actual = build_recent_visible_context(messages, previous_completed_turns=1)

    assert [message.content for message in actual] == [
        "我要转人工",
        "已转人工，坐席将尽快回复",
        "坐席已处理地址",
        "坐席已处理退款",
        "你好",
    ]
