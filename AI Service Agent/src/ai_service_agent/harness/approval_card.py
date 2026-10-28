"""坐席审批卡：HTML 渲染 + 一次性决策链接（ADR-0006）。

**为什么是 HTML 字符串**：Chatwoot 坐席端对 ``form`` 消息只渲染 ``content``
（且会过 DOMPurify），``items`` 与 ``cards`` 都不渲染 —— 想给坐席一张"有详情、
有按钮"的卡片，只能把 HTML 放进 ``content``。约束因此是硬的：

- 只用 DOMPurify 默认白名单内的标签（``div/span/table/tr/td/dl/h3/p/a``）与
  内联 ``style``；``<style>`` 与 ``on*`` 会被剥掉。
- 布局用 ``table`` 而不是 flex/grid：气泡里会继承 widget 的 CSS，table 最抗干扰。
- DOM 文本顺序必须线性可读（会话列表与通知栏会 ``getPlainText`` 拉平）。

**不做什么**：不决策、不落库、不发消息。投影在 ``approval_side_channel``，
决策在 ``ApprovalSideChannel.decide``；本模块只负责"画"和"签"。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
from datetime import datetime, timedelta, timezone

# --- 视觉 token（与 docs/ops/chatwoot/approval-card-mockup.html 同源） ---------

ACCENT = "#C0562B"          # 陶土橙：与客户 Widget 同一色板
ACCENT_STRONG = "#A2461F"
ACCENT_SOFT = "#FBEFE6"
ACCENT_LINE = "#EBD5C3"

INK = "#2A211B"
INK_2 = "#5A4A3D"
INK_3 = "#8A7C6D"
INK_4 = "#A9998A"

SURFACE = "#FFFDF8"
SURFACE_2 = "#FAF6F0"
LINE = "#E6D8C6"
LINE_SOFT = "#F0E6DA"

FONT_STACK = (
    '-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",'
    '"Hiragino Sans GB","Microsoft YaHei",sans-serif'
)

_CURRENCY_SYMBOLS = {"CNY": "¥", "RMB": "¥", "USD": "$", "SGD": "S$", "MYR": "RM", "THB": "฿"}
# 固定 +08:00：上海无夏令时，且不依赖 Windows 上的 IANA 时区数据库（tzdata）。
_CN_TZ = timezone(timedelta(hours=8))

#: 标题下的副标题：写"批准后会发生什么"，而不是复述标题。
_SUBTITLES = {
    "refund_apply": "客户申请退款，批准后立即打款，不可撤销",
    "cancel_order": "客户申请取消订单，批准后立即执行，不可撤销",
}
_SUBTITLE_FALLBACK = "该操作需你确认后才会执行，批准后不可撤销"

#: 主按钮文案写后果（"批准并退款"），不写泛泛的"批准"。
_APPROVE_LABELS = {
    "refund_apply": "批准并退款",
    "cancel_order": "批准并取消订单",
}
_APPROVE_FALLBACK = "批准并执行"

DEFAULT_TOKEN_TTL_SECONDS = 15 * 60


# --- 错误 ---------------------------------------------------------------------


class TokenInvalid(Exception):
    """签名不符 / 结构损坏 / 与审批单不匹配 → 400。"""


class TokenExpired(Exception):
    """超出 TTL → 410。"""


# --- 小工具 -------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _money(amount: object, currency: str = "CNY") -> str | None:
    if amount is None:
        return None
    try:
        text = f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        return None
    code = (currency or "CNY").upper()
    symbol = _CURRENCY_SYMBOLS.get(code)
    return f"{symbol}{text}" if symbol else f"{code} {text}"


def format_time(iso: object, *, now: datetime | None = None) -> str | None:
    """ISO 时间 → 坐席看得懂的「今天 17:49」。解析不了返回 None（不展示原始串）。"""
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(_CN_TZ)
    today = (now or datetime.now(_CN_TZ)).astimezone(_CN_TZ).date()
    delta = (today - local.date()).days
    if delta == 0:
        return f"今天 {local:%H:%M}"
    if delta == 1:
        return f"昨天 {local:%H:%M}"
    return f"{local:%m-%d %H:%M}"


def _ttl_minutes(expires_at: object, *, now: datetime | None = None) -> int | None:
    if not expires_at:
        return None
    try:
        moment = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    remaining = (moment - (now or datetime.now(timezone.utc))).total_seconds()
    if remaining <= 0:
        return 0
    return max(1, int(remaining // 60))


def _row(label: str, value: str) -> str:
    return (
        '<tr>'
        f'<td style="width:58px;padding:0 10px 7px 0;vertical-align:top;'
        f'font-size:11.5px;line-height:1.5;color:{INK_4}">{_esc(label)}</td>'
        f'<td style="padding:0 0 7px;vertical-align:top;'
        f'font-size:12.5px;line-height:1.5;color:{INK_2}">{value}</td>'
        "</tr>"
    )


# --- 签名 ---------------------------------------------------------------------


def derive_decision_secret(seat_secret: str, explicit: str | None = None) -> str:
    """决策链接的签名密钥：显式配置优先，否则从座位口令派生（不新增必填 env）。"""
    if explicit:
        return explicit
    return hmac.new(seat_secret.encode("utf-8"), b"approval-link", hashlib.sha256).hexdigest()


def default_base_url() -> str:
    """Runtime 自身对外地址：显式配置优先，否则按 ``AI_SERVICE_PORT`` 推本机地址。

    ``create_app`` 会显式注入，这里只是兜底——卡片按钮必须指回 Runtime，
    不能指向 Chatwoot（:3000）否则是死链。
    """
    import os

    from ai_service_agent.config import get_settings

    configured = get_settings().approval_link_base_url
    if configured:
        return configured.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('AI_SERVICE_PORT', '8000')}"


def sign_decision_token(
    approval_id: str,
    conversation_id: str,
    seat_id: str,
    *,
    secret: str,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    """``base64url(payload) + "." + hmac_sha256``。一次性由 ``approval_record.status`` 保证。"""
    issued = int((now or datetime.now(timezone.utc)).timestamp())
    payload = {
        "a": approval_id,
        "c": conversation_id,
        "s": seat_id,
        "i": issued,
        "e": issued + ttl_seconds,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_decision_token(
    token: str,
    approval_id: str,
    *,
    secret: str,
    now: datetime | None = None,
) -> dict:
    """校验签名、归属与有效期，返回 payload（含 ``c`` 会话与 ``s`` 坐席）。"""
    if not token or "." not in token:
        raise TokenInvalid("malformed token")
    body, _, signature = token.partition(".")
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise TokenInvalid("bad signature")
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError, binascii.Error):
        raise TokenInvalid("bad payload") from None
    if not isinstance(payload, dict):
        raise TokenInvalid("bad payload")
    if payload.get("a") != approval_id:
        raise TokenInvalid("approval mismatch")
    expires = payload.get("e")
    if not isinstance(expires, int):
        raise TokenInvalid("bad expiry")
    if int((now or datetime.now(timezone.utc)).timestamp()) >= expires:
        raise TokenExpired(approval_id)
    return payload


# --- 卡片 ---------------------------------------------------------------------


def render_approval_card(
    facts: dict,
    *,
    approval_id: str,
    expires_at: str,
    base_url: str,
    token: str,
    order_detail_url: str | None = None,
    now: datetime | None = None,
) -> str:
    """渲染坐席会话里的审批卡（HTML 字符串，全部内联样式）。

    信息架构五段：风险抬头 → 标题即结论 → 事实 → 客户原话 → 动作。
    缺值一律走兜底文案，绝不出现 ``None`` / ``u0034`` / ISO 微秒串。
    """
    facts = facts or {}
    tool = str(facts.get("tool") or "")
    action_label = str(facts.get("action_label") or "高风险操作")
    currency = str(facts.get("currency") or "CNY")
    amount_text = _money(facts.get("amount"), currency)

    title = f"{action_label} {amount_text}" if amount_text else action_label
    subtitle = _SUBTITLES.get(tool, _SUBTITLE_FALLBACK)

    # 事实：有值才出行，避免整片"暂缺"糊在坐席脸上。
    rows: list[str] = []
    customer = facts.get("customer") or {}
    customer_name = str(customer.get("name") or "").strip()
    phone_tail = str(customer.get("phone_tail") or "").strip()
    if customer_name and phone_tail:
        rows.append(_row("客户", f'{_esc(customer_name)} <span style="color:{INK_4}">· 尾号 {_esc(phone_tail)}</span>'))
    elif customer_name:
        rows.append(_row("客户", _esc(customer_name)))
    elif phone_tail:
        rows.append(_row("客户", f'尾号 {_esc(phone_tail)}'))
    else:
        rows.append(_row("客户", "客户信息暂缺"))

    if facts.get("order_id"):
        rows.append(_row("订单", _esc(facts["order_id"])))
    product = facts.get("product_summary")
    rows.append(_row("商品", _esc(product) if product else "商品信息暂缺，查看订单详情"))
    if amount_text:
        detail: list[str] = []
        order_total = _money(facts.get("order_total"), currency)
        if order_total and facts.get("order_total") != facts.get("amount"):
            detail.append(f"订单总额 {_esc(order_total)}")
        refunded = facts.get("refunded")
        if refunded:
            refunded_text = _money(refunded, currency)
            detail.append(f"已退 {_esc(refunded_text)}")
        suffix = (
            f' <span style="color:{INK_4}">· {"，".join(detail)}</span>' if detail else ""
        )
        rows.append(_row("金额", f"{_esc(amount_text)}{suffix}"))
    if facts.get("order_status_label"):
        rows.append(_row("状态", _esc(facts["order_status_label"])))
    requested = format_time(facts.get("requested_at"), now=now)
    if requested:
        rows.append(_row("申请时间", _esc(requested)))

    quote = str(facts.get("customer_quote") or "").strip()
    quote_block = (
        f'<div style="margin-top:12px;padding:8px 10px;background:{SURFACE_2};'
        f'border-left:2px solid {ACCENT_LINE};border-radius:0 6px 6px 0;'
        f'font-size:12.5px;line-height:1.6;color:{INK_2}">「{_esc(quote)}」'
        f'<span style="color:{INK_4};font-size:11px"> · 客户原话</span></div>'
        if quote
        else ""
    )

    ref = f"#{approval_id[:6]}" if approval_id else ""
    ttl = _ttl_minutes(expires_at, now=now)
    ttl_text = f"{ttl} 分钟内有效" if ttl else "限时有效"

    base = (base_url or "").rstrip("/")
    approve_href = f"{base}/approvals/{approval_id}/decide?t={token}&action=approve"
    reject_href = f"{base}/approvals/{approval_id}/decide?t={token}&action=reject"

    detail_cell = ""
    if order_detail_url:
        detail_cell = (
            f'<td style="text-align:right;vertical-align:middle">'
            f'<a href="{_esc(order_detail_url)}" style="font-size:12px;color:{ACCENT_STRONG};'
            f'text-decoration:none">查看订单详情</a></td>'
        )

    approve_label = _APPROVE_LABELS.get(tool, _APPROVE_FALLBACK)

    return (
        f'<div style="width:424px;max-width:100%;background:{SURFACE};border:1px solid {LINE};'
        f'border-radius:12px;overflow:hidden;font-family:{FONT_STACK};text-align:left">'
        f'<div style="height:3px;background:{ACCENT}"></div>'
        f'<div style="padding:14px 16px 15px">'
        # 抬头
        f'<table style="width:100%;border-collapse:collapse"><tr>'
        f'<td style="vertical-align:middle">'
        f'<span style="display:inline-block;font-size:11px;font-weight:650;letter-spacing:.02em;'
        f'color:{ACCENT_STRONG};background:{ACCENT_SOFT};border:1px solid {ACCENT_LINE};'
        f'border-radius:6px;padding:3px 7px">高风险审批</span>'
        f'<span style="margin-left:8px;font-size:11px;color:{INK_4}">{_esc(ref)}</span>'
        f"</td>"
        f'<td style="text-align:right;vertical-align:middle;font-size:11px;color:{INK_3}">'
        f"{_esc(ttl_text)}</td>"
        f"</tr></table>"
        # 标题
        f'<div style="margin:9px 0 0;font-size:17px;line-height:1.35;font-weight:650;'
        f'letter-spacing:-.01em;color:{INK}">{_esc(title)}</div>'
        f'<div style="margin:4px 0 0;font-size:12px;line-height:1.55;color:{INK_3}">'
        f"{_esc(subtitle)}</div>"
        # 事实
        f'<table style="width:100%;border-collapse:collapse;margin-top:12px">'
        f"{''.join(rows)}</table>"
        f"{quote_block}"
        f'<div style="height:1px;background:{LINE_SOFT};margin-top:13px"></div>'
        # 动作
        f'<table style="width:100%;border-collapse:collapse;margin-top:12px"><tr>'
        f'<td style="width:1%;white-space:nowrap">'
        f'<a href="{_esc(approve_href)}" data-approval-action="approve" '
        f'style="display:inline-block;height:38px;line-height:38px;padding:0 15px;'
        f'border-radius:9px;background:{ACCENT};color:#fff;font-size:12.5px;font-weight:650;'
        f'text-decoration:none">{_esc(approve_label)}</a></td>'
        f'<td style="width:1%;white-space:nowrap;padding-left:8px">'
        f'<a href="{_esc(reject_href)}" data-approval-action="reject" '
        f'style="display:inline-block;height:38px;line-height:38px;padding:0 14px;'
        f'border-radius:9px;background:#fff;color:{INK_2};border:1px solid #DDCDBA;'
        f'font-size:12.5px;font-weight:650;text-decoration:none">拒绝</a></td>'
        f"{detail_cell}"
        f"</tr></table>"
        # 脚注
        f'<div style="margin-top:10px;font-size:11px;line-height:1.5;color:{INK_4}">'
        f"{_esc(ttl_text)} · 处理后客户立即收到结果</div>"
        f"</div></div>"
    )
