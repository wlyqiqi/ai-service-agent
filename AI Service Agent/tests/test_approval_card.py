"""T4：坐席审批卡 HTML 渲染 + 签名决策链接。

卡片是发给 Chatwoot 的 HTML 字符串，会过 DOMPurify；这里锁住三件事：
内容可读、缺值不露馅、签名不可伪造。
"""

from datetime import datetime, timedelta, timezone

import pytest

from ai_service_agent.harness.approval_card import (
    TokenExpired,
    TokenInvalid,
    derive_decision_secret,
    format_time,
    render_approval_card,
    sign_decision_token,
    verify_decision_token,
)

SECRET = "test-secret"
FACTS = {
    "tool": "refund_apply",
    "action_label": "退款申请",
    "order_id": "ORD-2026-10059",
    "amount": 240.31,
    "currency": "CNY",
    "order_total": 240.31,
    "refunded": 0.0,
    "product_summary": "澄元智能破壁机 Pro Max 01 ×1",
    "order_status": "paid",
    "order_status_label": "已付款 · 未发货",
    "customer": {"name": "张伟", "phone_tail": "6375"},
    "customer_quote": "不想要了，麻烦帮我退掉",
    "requested_at": "2026-10-28T09:49:00+00:00",
    "expires_at": "2026-10-28T10:19:00+00:00",
}


def _render(facts=None, **kwargs):
    params = {
        "approval_id": "da8668-aaaa-bbbb",
        "expires_at": "2026-10-28T10:19:00+00:00",
        "base_url": "http://127.0.0.1:8766",
        "token": "signed-token",
    }
    params.update(kwargs)
    return render_approval_card(FACTS if facts is None else facts, **params)


# --- 内容 ---------------------------------------------------------------------


def test_card_carries_all_five_sections():
    html = _render()
    # 抬头 → 标题 → 事实 → 原话 → 动作（DOM 文本顺序线性可读，供列表预览拉平）
    order = ["高风险审批", "退款申请 ¥240.31", "张伟", "ORD-2026-10059", "不想要了", "批准并退款"]
    positions = [html.find(text) for text in order]
    assert all(pos >= 0 for pos in positions), positions
    assert positions == sorted(positions)


def test_card_shows_currency_and_order_status():
    html = _render()
    assert "¥240.31" in html
    assert "已付款 · 未发货" in html
    assert "尾号 6375" in html


def test_card_uses_action_specific_button_label():
    assert "批准并退款" in _render()
    assert "批准并取消订单" in _render({**FACTS, "tool": "cancel_order", "action_label": "订单取消"})


def test_card_omits_amount_row_when_unknown():
    """通用写操作（改地址等）没有金额，不能渲染一个空的「金额」行。"""
    html = _render({"tool": "update_address", "action_label": "收货地址修改", "order_id": "ORD-1"})
    assert "金额" not in html
    assert "收货地址修改" in html


def test_card_falls_back_for_missing_product_and_customer():
    html = _render({"tool": "refund_apply", "action_label": "退款申请", "amount": 10})
    assert "商品信息暂缺" in html
    assert "客户信息暂缺" in html
    assert "None" not in html
    assert "不可用" not in html


def test_card_never_leaks_iso_microseconds():
    """ISO 微秒串坐席读不懂；解析不了就不展示，绝不原样输出。"""
    now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    html = _render({**FACTS, "requested_at": "2026-10-28T09:49:28.287790+00:00"}, now=now)
    assert "287790" not in html
    assert "今天 17:49" in html


def test_card_escapes_dangerous_field_values():
    html = _render(
        {
            "tool": "refund_apply",
            "action_label": "<script>alert(1)</script>",
            "order_id": 'ORD-"1"&<b>',
            "customer_quote": "<img src=x onerror=alert(1)>",
            "customer": {"name": "<b>张伟</b>", "phone_tail": "6375"},
        }
    )
    assert "<script>" not in html
    # 转义后是文本节点，不是标签：浏览器不会执行，也不会产生属性
    assert "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;张伟&lt;/b&gt;" in html
    assert "ORD-&quot;1&quot;&amp;" in html


def test_card_links_point_to_runtime_decide_page():
    html = _render()
    assert 'href="http://127.0.0.1:8766/approvals/da8668-aaaa-bbbb/decide?t=signed-token&amp;action=approve"' in html
    assert 'data-approval-action="reject"' in html


def test_card_only_uses_dompurify_safe_tags():
    """``<style>`` / ``on*`` 会被 Chatwoot 剥掉——出现就说明样式会丢。"""
    html = _render()
    assert "<style" not in html
    assert "<script" not in html
    assert " onclick" not in html


def test_ttl_text_reflects_remaining_minutes():
    now = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
    html = _render(expires_at="2026-10-28T10:19:00+00:00", now=now)
    assert "19 分钟内有效" in html


# --- 时间格式化 ---------------------------------------------------------------


def test_format_time_uses_shanghai_clock():
    assert format_time("2026-10-28T09:49:00+00:00", now=datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)) == "今天 17:49"
    assert format_time("2026-10-27T09:49:00+00:00", now=datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)) == "昨天 17:49"
    assert format_time("2026-10-20T09:49:00+00:00", now=datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)) == "10-20 17:49"


def test_format_time_returns_none_for_garbage():
    assert format_time(None) is None
    assert format_time("") is None
    assert format_time("not-a-time") is None


# --- 签名 ---------------------------------------------------------------------


def test_token_roundtrip_carries_conversation_and_seat():
    token = sign_decision_token("A-1", "c1", "seat-7", secret=SECRET)
    payload = verify_decision_token(token, "A-1", secret=SECRET)
    assert payload["c"] == "c1"
    assert payload["s"] == "seat-7"


def test_token_rejects_tampered_body():
    token = sign_decision_token("A-1", "c1", "seat-7", secret=SECRET)
    body, _, signature = token.partition(".")
    tampered = ("A" if body[0] != "A" else "B") + body[1:] + "." + signature
    with pytest.raises(TokenInvalid):
        verify_decision_token(tampered, "A-1", secret=SECRET)


def test_token_rejects_wrong_secret():
    token = sign_decision_token("A-1", "c1", "seat-7", secret=SECRET)
    with pytest.raises(TokenInvalid):
        verify_decision_token(token, "A-1", secret="other-secret")


def test_token_is_bound_to_approval_id():
    token = sign_decision_token("A-1", "c1", "seat-7", secret=SECRET)
    with pytest.raises(TokenInvalid):
        verify_decision_token(token, "A-2", secret=SECRET)


def test_token_expires():
    issued = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
    token = sign_decision_token("A-1", "c1", "seat-7", secret=SECRET, ttl_seconds=60, now=issued)
    with pytest.raises(TokenExpired):
        verify_decision_token(token, "A-1", secret=SECRET, now=issued + timedelta(seconds=61))


def test_token_rejects_malformed_input():
    for bad in ("", "no-dot", "a.b.c"):
        with pytest.raises(TokenInvalid):
            verify_decision_token(bad, "A-1", secret=SECRET)


def test_secret_derives_from_seat_secret_without_new_env():
    derived = derive_decision_secret("seat-secret")
    assert derived == derive_decision_secret("seat-secret")
    assert derived != derive_decision_secret("other")
    assert derive_decision_secret("seat-secret", "explicit") == "explicit"


def test_expired_card_says_expired_not_limited():
    """渲染时已超时（重放旧消息 / 时钟偏差）不能糊成「限时有效」。"""
    now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    html = _render(expires_at="2026-10-28T10:19:00+00:00", now=now)
    assert "已过期" in html
    assert "限时有效" not in html
