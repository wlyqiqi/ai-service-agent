"""PR-A 投诉硬闸 / 薄投诉面 / 询问待答。验收表 #1–#10 的离线硬门禁。"""

import importlib.util
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import _policy_gate, build_graph
from ai_service_agent.graph.complaint_rules import (
    classify_complaint_offer_reply,
    complaint_gate_applies,
    detect_complaint_intent,
    emotion_auto_handoff_allowed,
)
from ai_service_agent.graph.handoff_rules import detect_handoff_intent
from ai_service_agent.graph.route_policy import (
    ROUTE_POLICY,
    route_intent_is_confident,
)
from ai_service_agent.graph.workflows.copy import (
    complaint_empathy_templates,
    complaint_empathy_text,
    complaint_reconfirm_text,
)
from ai_service_agent.graph.workflows.fallback import check_handoff, emotion_handoff_reason
from ai_service_agent.harness.complaint_offer import clear_complaint_offer_flags
from ai_service_agent.harness.handoff_entry import (
    COMPLAINT_HANDOFF_REASON,
    EXPLICIT_HANDOFF_REASON,
    EntryDecision,
    decide_entry,
    entry_handoff_reason,
)
from ai_service_agent.prompts import (
    ROUTE_INTENT_CRITERIA,
    ROUTE_INTENT_FEW_SHOTS,
    ROUTE_INTENT_INSTRUCTIONS,
    ROUTE_INTENT_SYSTEM,
)
from ai_service_agent.testing import (
    ToolCallingFakeModel,
    route_intent_json_message,
    select_json_message,
)
from ai_service_agent.tools.kb import kb_search
from ai_service_agent.tools.orders import my_orders, order_lookup, shipment_query
from conftest import ainvoke_graph

COMPLAINT = "我要投诉你们，服务太差了，到底能不能解决啊"
INVOICE = "开个发票吧"
TICKET = "商品缺配件，帮我开售后"
MIXED = "服务太差，还是先帮我开发票"
CAPABILITY_WORDS = ("开票", "发票", "备注", "售后", "工单")


def _last_ai(out) -> str:
    for message in reversed(out["messages"]):
        if getattr(message, "type", "") == "ai" and message.content:
            return message.content if isinstance(message.content, str) else str(message.content)
    return ""


@pytest.fixture
def enabled():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def seeded_graph(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "seed_mock_db_complaint", root / "scripts" / "seed_mock_db.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "mock_ecommerce.db"
    module.build_database(str(db_path))
    from ai_service_agent.graph import builder as builder_mod

    class _Settings:
        def __init__(self, real):
            self.__dict__.update(real.__dict__)
            self.mock_ecommerce_db_path = str(db_path)

    real_settings = builder_mod.get_settings()
    monkeypatch.setattr(builder_mod, "get_settings", lambda: _Settings(real_settings))
    monkeypatch.setenv("JEV_ENABLED", "false")
    get_settings.cache_clear()

    def _build(responses):
        return build_graph(
            ToolCallingFakeModel(responses=responses),
            tools=[kb_search, order_lookup, my_orders, shipment_query],
            checkpointer=MemorySaver(),
        )

    yield _build
    get_settings.cache_clear()


def _turn(graph, text, thread_id, *, context=None):
    payload = {
        "messages": [HumanMessage(content=text)],
        "user_id": "u1",
        "session_id": thread_id,
    }
    if context is not None:
        payload["context"] = context
    return ainvoke_graph(graph, payload, {"configurable": {"thread_id": thread_id}})


def test_complaint_policy_is_not_an_approval_write_path():
    policy = ROUTE_POLICY["complaint"]
    assert policy.target_graph == "complaint_graph"
    assert policy.execution_class == "complaint"
    assert policy.allowed_tools == ()
    assert policy.requires_approval is False
    assert policy.execution_class != "high_risk"
    assert route_intent_is_confident("complaint", 0.80) is True
    assert route_intent_is_confident("complaint", 0.79) is False


def test_detect_complaint_intent_phrases_and_weak_emotion(enabled):
    assert detect_complaint_intent(COMPLAINT) is True
    assert detect_complaint_intent("态度太差，我要找消协") is True
    assert detect_complaint_intent("有点不满意") is False
    assert detect_complaint_intent(INVOICE) is False
    assert detect_complaint_intent(TICKET) is False
    assert detect_complaint_intent("") is False


def test_same_utterance_handoff_beats_complaint(enabled):
    text = "我要投诉你们，转人工"
    assert detect_handoff_intent(text) is True
    assert detect_complaint_intent(text) is True
    assert complaint_gate_applies(text) is False
    assert decide_entry(mode="ai", awaiting=False, text=text) is EntryDecision.REQUEST
    update = _policy_gate(
        {
            "route_intent": "order_mutation",
            "confidence": 0.9,
            "messages": [HumanMessage(content=text)],
        }
    )
    assert update["route_intent"] == "order_mutation"
    assert update["target_graph"] == "mutation_graph"


def test_policy_gate_forces_complaint_over_wrong_llm_label(enabled):
    update = _policy_gate(
        {
            "route_intent": "order_mutation",
            "confidence": 0.2,
            "messages": [HumanMessage(content=COMPLAINT)],
        }
    )
    assert update["route_intent"] == "complaint"
    assert update["confidence"] == 1.0
    assert update["target_graph"] == "complaint_graph"
    assert update["execution_class"] == "complaint"
    assert update["allowed_tools"] == []
    assert update["requires_approval"] is False


def test_complaint_phrases_are_configurable(monkeypatch):
    class _Settings:
        complaint_phrases = ["自定义投诉词"]
        complaint_offer_agree_phrases = ["要人工"]
        complaint_offer_reconfirm_exact = ["要"]

    monkeypatch.setattr(
        "ai_service_agent.graph.complaint_rules.get_settings", lambda: _Settings()
    )
    assert detect_complaint_intent("请处理自定义投诉词") is True
    assert detect_complaint_intent("我要投诉") is False
    assert classify_complaint_offer_reply("要人工") == "agree"
    assert classify_complaint_offer_reply("要") == "reconfirm"


def test_empathy_templates_do_not_list_mutation_capabilities():
    texts = (*complaint_empathy_templates(), complaint_reconfirm_text())
    for text in texts:
        assert "抱歉" in text or "对不起" in text or "确认" in text
        for word in CAPABILITY_WORDS:
            assert word not in text
    assert "转人工" in complaint_empathy_templates()[0]
    assert "转人工" in complaint_reconfirm_text()
    assert "业务" in complaint_reconfirm_text()


def test_route_prompt_has_five_families_and_few_shots():
    assert "投诉类" in ROUTE_INTENT_INSTRUCTIONS
    assert "complaint 优先于" in ROUTE_INTENT_INSTRUCTIONS
    assert "查询类" in ROUTE_INTENT_INSTRUCTIONS
    assert "complaint" in ROUTE_INTENT_CRITERIA
    assert "有点不满意" in ROUTE_INTENT_CRITERIA["complaint"]
    assert "不要归 order_mutation" in ROUTE_INTENT_CRITERIA["order_mutation"]
    assert len(ROUTE_INTENT_FEW_SHOTS) >= 5
    intents = {intent for _, intent, _ in ROUTE_INTENT_FEW_SHOTS}
    assert "complaint" in intents
    assert "order_mutation" in intents
    assert "faq" in intents
    joined = "\n".join(user for user, _, _ in ROUTE_INTENT_FEW_SHOTS)
    assert "有点不满意" in joined
    assert "售后" in joined
    assert "发票" in joined
    assert "有点不满意" in ROUTE_INTENT_SYSTEM
    assert ROUTE_INTENT_SYSTEM.count('{"route_intent":') >= 5


def test_handoff_entry_unchanged_for_explicit_transfer(enabled):
    assert decide_entry(mode="ai", awaiting=False, text="转人工") is EntryDecision.REQUEST
    assert (
        entry_handoff_reason(text="转人工", complaint_offer_pending=False)
        == EXPLICIT_HANDOFF_REASON
    )
    assert decide_entry(mode="ai", awaiting=False, text=COMPLAINT) is EntryDecision.AI
    assert decide_entry(mode="human", awaiting=False, text=COMPLAINT) is EntryDecision.HUMAN_LANE


@pytest.mark.parametrize("text", ["转人工", "要人工", "帮我转"])
def test_pending_narrow_agree_reuses_entry_request(enabled, text):
    assert (
        decide_entry(mode="ai", awaiting=False, text=text, complaint_offer_pending=True)
        is EntryDecision.REQUEST
    )
    assert (
        entry_handoff_reason(text=text, complaint_offer_pending=True)
        == COMPLAINT_HANDOFF_REASON
    )


def test_bare_yao_is_reconfirm_not_request(enabled):
    assert classify_complaint_offer_reply("要") == "reconfirm"
    assert classify_complaint_offer_reply("要。") == "reconfirm"
    assert (
        decide_entry(mode="ai", awaiting=False, text="要", complaint_offer_pending=True)
        is EntryDecision.AI
    )
    assert detect_handoff_intent("要人工") is False
    assert detect_handoff_intent("帮我转") is False


def test_emotion_rule_cannot_auto_handoff_while_offer_pending(monkeypatch):
    assert emotion_auto_handoff_allowed({"complaint_offer_pending": True}) is False
    assert emotion_auto_handoff_allowed({}) is True
    monkeypatch.setattr(
        "ai_service_agent.graph.workflows.fallback._emotion_signal", lambda text: True
    )
    pending = {
        "messages": [HumanMessage(content="服务太差，我很生气")],
        "context": {"complaint_offer_pending": True, "fallback_count": 0},
    }
    blocked = check_handoff(pending)
    assert not hasattr(blocked, "goto")
    assert blocked["handoff"] is False
    assert emotion_handoff_reason(pending) is None

    open_offer = {
        "messages": [HumanMessage(content="服务太差，我很生气")],
        "context": {"fallback_count": 0},
    }
    allowed = check_handoff(open_offer)
    assert allowed.goto == "pause"
    assert allowed.update["handoff_reason"] == "情绪信号"

    explicit = check_handoff(
        {
            "messages": [HumanMessage(content="转人工")],
            "context": {"complaint_offer_pending": True, "fallback_count": 0},
        }
    )
    assert explicit.goto == "pause"
    assert explicit.update["handoff_reason"] == "用户显式请求转人工"


def test_case_1_complaint_empathy_without_handoff_or_mutation(seeded_graph):
    graph = seeded_graph([route_intent_json_message("order_mutation", 0.9)])
    out = _turn(graph, COMPLAINT, "c1", context={})
    assert out["route_intent"] == "complaint"
    assert out["target_graph"] == "complaint_graph"
    assert out["execution_class"] == "complaint"
    assert out["requires_approval"] is False
    assert out["allowed_tools"] == []
    assert out["execution_status"] == "completed"
    assert not out.get("__interrupt__")
    assert out["context"]["complaint_offer_pending"] is True
    assert out["context"].get("fallback_count", 0) == 0
    assert "pending_tool" not in out["context"]
    reply = _last_ai(out)
    assert reply == complaint_empathy_text(COMPLAINT)
    for word in CAPABILITY_WORDS:
        assert word not in reply
    assert "转人工" in reply


def test_case_2_clean_invoice_still_reaches_mutation_form(seeded_graph):
    graph = seeded_graph(
        [
            route_intent_json_message("order_mutation", 0.94),
            select_json_message("create_invoice", {"order_id": "ORD-PAID-1"}),
        ]
    )
    out = _turn(graph, INVOICE, "c2", context={})
    assert out["route_intent"] == "order_mutation"
    assert out["target_graph"] == "mutation_graph"
    assert out.get("__interrupt__")
    assert out["__interrupt__"][0].value["tool"] == "create_invoice"
    assert out["context"].get("complaint_offer_pending") is not True


def test_case_3_missing_parts_still_service_ticket(seeded_graph):
    graph = seeded_graph(
        [
            route_intent_json_message("order_mutation", 0.93),
            select_json_message("create_service_ticket", {"order_id": "ORD-PAID-1"}),
        ]
    )
    out = _turn(graph, TICKET, "c3", context={})
    assert out["route_intent"] == "order_mutation"
    assert out["allowed_tools"] == [
        "update_order_note",
        "create_invoice",
        "create_service_ticket",
    ]
    assert out["__interrupt__"][0].value["tool"] == "create_service_ticket"


def test_case_4_mixed_complaint_does_not_enter_mutation_this_turn(seeded_graph):
    graph = seeded_graph([route_intent_json_message("order_mutation", 0.9)])
    out = _turn(graph, MIXED, "c4", context={})
    assert out["route_intent"] == "complaint"
    assert out["target_graph"] == "complaint_graph"
    assert not out.get("__interrupt__")
    assert "pending_tool" not in out["context"]
    reply = _last_ai(out)
    for word in CAPABILITY_WORDS:
        assert word not in reply


def test_case_6_graph_agree_sets_accept_without_second_interrupt(seeded_graph):
    graph = seeded_graph(
        [
            route_intent_json_message("order_mutation", 0.9),
            route_intent_json_message("unknown", 0.4),
        ]
    )
    first = _turn(graph, COMPLAINT, "c6", context={})
    assert first["context"]["complaint_offer_pending"] is True
    second = _turn(graph, "要人工", "c6")
    assert not second.get("__interrupt__")
    assert second["context"]["complaint_handoff_accept"] is True
    assert second["context"]["complaint_offer_pending"] is False
    assert second["target_graph"] == "complaint_graph"
    assert second["requires_approval"] is False
    assert "pending_tool" not in second["context"]
    assert second["execution_status"] == "completed"


def test_case_6_bare_yao_reconfirms_once(seeded_graph):
    graph = seeded_graph(
        [
            route_intent_json_message("order_mutation", 0.9),
            route_intent_json_message("smalltalk", 0.9),
        ]
    )
    _turn(graph, COMPLAINT, "c6b", context={})
    out = _turn(graph, "要", "c6b")
    assert not out.get("__interrupt__")
    assert out["context"]["complaint_offer_pending"] is True
    assert out["context"].get("complaint_handoff_accept") is not True
    assert _last_ai(out) == complaint_reconfirm_text()
    for word in CAPABILITY_WORDS:
        assert word not in _last_ai(out)


def test_case_7_decline_clears_pending_and_reaches_invoice_form(seeded_graph):
    graph = seeded_graph(
        [
            route_intent_json_message("order_mutation", 0.9),
            route_intent_json_message("order_mutation", 0.94),
            select_json_message("create_invoice", {}),
        ]
    )
    _turn(graph, COMPLAINT, "c7", context={})
    out = _turn(graph, "不用，帮我开票", "c7")
    assert out["context"].get("complaint_offer_pending") is not True
    assert out["context"].get("complaint_handoff_accept") is not True
    assert out["route_intent"] == "order_mutation"
    assert out["target_graph"] == "mutation_graph"
    assert out["__interrupt__"][0].value.get("type") != "handoff_request"
    assert out["__interrupt__"][0].value["tool"] == "create_invoice"


def test_case_8_smalltalk_does_not_burn_fallback(seeded_graph):
    graph = seeded_graph(
        [
            route_intent_json_message("smalltalk", 0.9),
            AIMessage(content="你好，我是澄元客服。"),
        ]
    )
    out = _turn(graph, "你好", "c8", context={})
    assert out["route_intent"] == "smalltalk"
    assert out["target_graph"] == "read_graph"
    assert out["context"].get("fallback_count", 0) == 0
    assert not out.get("__interrupt__")


def test_case_9_my_orders(seeded_graph):
    graph = seeded_graph(
        [
            route_intent_json_message("my_orders", 0.92),
            AIMessage(content="您名下有这些订单。"),
        ]
    )
    out = _turn(graph, "我有哪些订单", "c9", context={})
    assert out["route_intent"] == "my_orders"
    assert out["target_graph"] == "read_graph"
    assert out["allowed_tools"] == ["my_orders"]


def test_case_10_order_query(seeded_graph):
    graph = seeded_graph(
        [
            route_intent_json_message("order_query", 0.93),
            AIMessage(content="ORD-1 已发货。"),
        ]
    )
    out = _turn(graph, "查一下订单 ORD-1", "c10", context={})
    assert out["route_intent"] == "order_query"
    assert out["target_graph"] == "read_graph"
    assert out["allowed_tools"] == ["order_lookup"]


def test_clear_offer_flags_after_complaint_turn(seeded_graph):
    graph = seeded_graph([route_intent_json_message("complaint", 0.9)])
    _turn(graph, COMPLAINT, "c-clear", context={})
    cfg = {"configurable": {"thread_id": "c-clear"}}

    async def _clear():
        await clear_complaint_offer_flags(graph, cfg)
        return await graph.aget_state(cfg)

    import asyncio

    snapshot = asyncio.run(_clear())
    assert snapshot.values["context"]["complaint_offer_pending"] is False
