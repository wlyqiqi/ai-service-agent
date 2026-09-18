import pytest

from ai_service_agent.graph.handoff_rules import detect_handoff_intent

TRUE_CASES = [
    "我要转人工",
    "转人工",
    "我不想跟机器人聊，叫个真人来",
    "找个人客服",
    "我要找客服",
    "麻烦帮我转人工客服",   # CTRL-0061
    "联系人工客服",
    "我要人工",
    "我要找人工",
    "给我接人工",
    "找客服",
    "真人客服",
    "转接人工",
    "请安排人工客服处理退款",
]

FALSE_CASES = [
    "人工呼吸",
    "人工智能是什么？",
    "人工审核需要多久？",
    "人工成本怎么算？",
    "人工客服培训",
    "随便聊聊",
    "查一下订单 ORD-1",
    "帮我看看人工智能的文章",
    "帮我把人工审核加快",   # 话题性（负向后缀排除）
    "麻烦转人工呼吸机的售后",
]


@pytest.mark.parametrize("text", TRUE_CASES)
def test_explicit_handoff_intent_detected(text):
    assert detect_handoff_intent(text) is True


@pytest.mark.parametrize("text", FALSE_CASES)
def test_non_handoff_intent_not_detected(text):
    assert detect_handoff_intent(text) is False


def test_blank_input_is_false():
    assert detect_handoff_intent("") is False
    assert detect_handoff_intent("   ") is False
