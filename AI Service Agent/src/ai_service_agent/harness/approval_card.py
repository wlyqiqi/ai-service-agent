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
    if local.year != today.year:
        return f"{local:%Y-%m-%d %H:%M}"
    delta = (today - local.date()).days
    if delta == 0:
        return f"今天 {local:%H:%M}"
    if delta == 1:
        return f"昨天 {local:%H:%M}"
    return f"{local:%m-%d %H:%M}"


def now_hm() -> str:
    """坐席留痕用的「17:52」（Asia/Shanghai）。"""
    return datetime.now(_CN_TZ).strftime("%H:%M")


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
    if ttl is None:
        ttl_text = "限时有效"
    elif ttl <= 0:
        # 渲染时就已经超时（重放旧消息、时钟偏差）：别写「限时有效」糊过去
        ttl_text = "已过期"
    else:
        ttl_text = f"{ttl} 分钟内有效"

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


# --- 决策确认页 ---------------------------------------------------------------
#
# 独立页面（不受 Chatwoot 的 DOMPurify 限制），所以可以用 grid / 自定义字体栈。
# 视觉延续卡片：米白纸感 + 陶土橙 + 细线，票据式的紧凑排版。

_PAGE_CSS = f"""
*,*::before,*::after{{box-sizing:border-box}}
html,body{{margin:0;padding:0}}
body{{
  background:#EFEDE9;color:{INK};
  font-family:{FONT_STACK};
  font-size:14px;line-height:1.6;
  -webkit-font-smoothing:antialiased;
  padding:clamp(20px,5vw,56px) 16px;
  display:flex;justify-content:center;align-items:flex-start;
  min-height:100vh;
}}
.sheet{{width:100%;max-width:520px}}
.brand{{
  display:flex;align-items:baseline;gap:8px;
  margin:0 0 14px;padding:0 2px;
  font-size:11px;font-weight:650;letter-spacing:.14em;text-transform:uppercase;
  color:{INK_4};
}}
.brand b{{color:{ACCENT_STRONG};font-weight:700;letter-spacing:.1em}}
.card{{
  background:{SURFACE};border:1px solid {LINE};border-radius:16px;
  overflow:hidden;box-shadow:0 1px 2px rgba(42,33,27,.05),0 18px 40px -28px rgba(42,33,27,.35);
}}
.stripe{{height:3px;background:{ACCENT}}}
.stripe.ok{{background:#2F7A57}}
.stripe.bad{{background:#B23A2E}}
.stripe.mute{{background:#C9BEB2}}
.body{{padding:clamp(18px,4vw,26px)}}
.eyebrow{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.tag{{
  display:inline-flex;align-items:center;
  font-size:11px;font-weight:650;letter-spacing:.02em;
  color:{ACCENT_STRONG};background:{ACCENT_SOFT};
  border:1px solid {ACCENT_LINE};border-radius:6px;padding:3px 8px;
}}
.ref{{font-size:11px;color:{INK_4};font-variant-numeric:tabular-nums}}
.valid{{margin-left:auto;font-size:11px;color:{INK_3};font-variant-numeric:tabular-nums}}
h1{{
  margin:14px 0 0;font-size:clamp(21px,4.6vw,25px);line-height:1.3;
  font-weight:700;letter-spacing:-.02em;
}}
h1 .amt{{font-variant-numeric:tabular-nums}}
.sub{{margin:6px 0 0;font-size:12.5px;line-height:1.6;color:{INK_3}}}
.facts{{
  margin:20px 0 0;padding:16px 0 0;border-top:1px solid {LINE_SOFT};
  display:grid;grid-template-columns:64px 1fr;row-gap:9px;column-gap:12px;
}}
.facts dt{{margin:0;font-size:11.5px;color:{INK_4};line-height:1.55}}
.facts dd{{margin:0;font-size:13px;color:{INK_2};line-height:1.55;font-variant-numeric:tabular-nums}}
.facts .dim{{color:{INK_4}}}
blockquote{{
  margin:18px 0 0;padding:10px 13px;
  background:{SURFACE_2};border-left:2px solid {ACCENT_LINE};
  border-radius:0 8px 8px 0;
  font-size:13px;line-height:1.65;color:{INK_2};
}}
blockquote cite{{display:block;margin-top:4px;font-size:11px;font-style:normal;color:{INK_4}}}
form{{margin:22px 0 0;padding-top:18px;border-top:1px solid {LINE_SOFT}}}
.choices{{display:grid;gap:9px;margin:0 0 14px}}
.choice{{
  display:flex;align-items:center;gap:10px;
  padding:12px 14px;border:1px solid #DDCDBA;border-radius:11px;
  background:#fff;cursor:pointer;
  transition:border-color .16s ease,background .16s ease;
}}
.choice:hover{{background:{SURFACE_2}}}
.choice input{{appearance:none;width:16px;height:16px;margin:0;flex:none;
  border:1.5px solid #C9BEB2;border-radius:50%;position:relative;cursor:pointer}}
.choice input:checked{{border-color:{ACCENT}}}
.choice input:checked::after{{
  content:"";position:absolute;inset:3px;border-radius:50%;background:{ACCENT};
}}
.choice:has(input:checked){{border-color:{ACCENT};background:{ACCENT_SOFT}}}
.choice span{{font-size:13px;font-weight:600;color:{INK_2}}}
.choice small{{margin-left:auto;font-size:11px;color:{INK_4};font-weight:400}}
.note{{margin:0 0 14px}}
.note[hidden]{{display:none}}
.note label{{display:block;font-size:11.5px;font-weight:600;color:{INK_3};margin-bottom:6px}}
.note textarea{{
  width:100%;min-height:78px;padding:10px 12px;
  font:inherit;font-size:13px;line-height:1.6;color:{INK};
  background:#fff;border:1px solid #DDCDBA;border-radius:10px;resize:vertical;
}}
.note textarea:focus{{
  outline:none;border-color:{ACCENT};box-shadow:0 0 0 3px {ACCENT_SOFT};
}}
button{{
  width:100%;height:48px;border:0;border-radius:11px;
  background:{ACCENT};color:#fff;
  font:inherit;font-size:14px;font-weight:650;letter-spacing:.01em;
  cursor:pointer;transition:background .16s ease,transform .06s ease;
}}
button:hover{{background:{ACCENT_STRONG}}}
button:active{{transform:translateY(.5px)}}
button:disabled{{opacity:.55;cursor:default}}
.foot{{margin:14px 0 0;font-size:11px;line-height:1.6;color:{INK_4}}}
.notice{{margin:16px 0 0;padding:12px 14px;border-radius:10px;font-size:13px;line-height:1.65}}
.notice.ok{{background:#EAF3EE;color:#235C41}}
.notice.bad{{background:#FBEDEA;color:#8A2B21}}
.notice.mute{{background:{SURFACE_2};color:{INK_2}}}
.detail{{margin:8px 0 0;font-size:11.5px;color:{INK_4};font-variant-numeric:tabular-nums}}
@media (max-width:420px){{
  .facts{{grid-template-columns:1fr;row-gap:2px}}
  .facts dt{{margin-top:8px}}
}}
"""


def _page(title: str, body: str, *, stripe: str = "") -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{_esc(title)}</title><style>{_PAGE_CSS}</style></head>"
        f'<body><main class="sheet">{body}</main></body></html>'
    )


def render_decision_page(
    facts: dict,
    *,
    approval_id: str,
    expires_at: str,
    token: str,
    form_action: str,
    now: datetime | None = None,
) -> str:
    """坐席确认页：GET 只渲染，决策走 POST（不留 CSRF 风险面）。

    与卡片同一套事实与视觉；差别是这里有完整的操作区（拒绝理由必填）。
    """
    facts = facts or {}
    tool = str(facts.get("tool") or "")
    action_label = str(facts.get("action_label") or "高风险操作")
    currency = str(facts.get("currency") or "CNY")
    amount_text = _money(facts.get("amount"), currency)
    title = f"{action_label} {amount_text}" if amount_text else action_label
    subtitle = _SUBTITLES.get(tool, _SUBTITLE_FALLBACK)
    approve_label = _APPROVE_LABELS.get(tool, _APPROVE_FALLBACK)

    rows: list[str] = []
    customer = facts.get("customer") or {}
    customer_name = str(customer.get("name") or "").strip()
    phone_tail = str(customer.get("phone_tail") or "").strip()
    if customer_name and phone_tail:
        rows.append(f"<dt>客户</dt><dd>{_esc(customer_name)} <span class=\"dim\">· 尾号 {_esc(phone_tail)}</span></dd>")
    elif customer_name or phone_tail:
        rows.append(f"<dt>客户</dt><dd>{_esc(customer_name or phone_tail)}</dd>")
    else:
        rows.append("<dt>客户</dt><dd>客户信息暂缺</dd>")
    if facts.get("order_id"):
        rows.append(f"<dt>订单</dt><dd>{_esc(facts['order_id'])}</dd>")
    product = facts.get("product_summary")
    rows.append(f"<dt>商品</dt><dd>{_esc(product) if product else '商品信息暂缺'}</dd>")
    if amount_text:
        rows.append(f"<dt>金额</dt><dd>{_esc(amount_text)}</dd>")
    if facts.get("order_status_label"):
        rows.append(f"<dt>状态</dt><dd>{_esc(facts['order_status_label'])}</dd>")
    requested = format_time(facts.get("requested_at"), now=now)
    if requested:
        rows.append(f"<dt>申请时间</dt><dd>{_esc(requested)}</dd>")

    quote = str(facts.get("customer_quote") or "").strip()
    quote_block = (
        f"<blockquote>「{_esc(quote)}」<cite>客户原话</cite></blockquote>" if quote else ""
    )

    ref = f"#{approval_id[:6]}" if approval_id else ""
    valid = format_time(expires_at, now=now)

    body = (
        f'<div class="brand"><b>澄元</b><span>坐席审批</span></div>'
        '<article class="card"><div class="stripe"></div><div class="body">'
        '<div class="eyebrow">'
        '<span class="tag">高风险审批</span>'
        f'<span class="ref">{_esc(ref)}</span>'
        + (f'<span class="valid">有效至 {_esc(valid)}</span>' if valid else "")
        + "</div>"
        f"<h1>{_esc(title)}</h1>"
        f'<p class="sub">{_esc(subtitle)}</p>'
        f'<dl class="facts">{"".join(rows)}</dl>'
        f"{quote_block}"
        f'<form method="post" action="{_esc(form_action)}">'
        f'<input type="hidden" name="t" value="{_esc(token)}">'
        '<fieldset class="choices" style="border:0;padding:0;margin:0 0 14px">'
        '<label class="choice"><input type="radio" name="action" value="approve" checked>'
        '<span>批准</span><small>立即执行，不可撤销</small></label>'
        '<label class="choice"><input type="radio" name="action" value="reject">'
        '<span>拒绝</span><small>客户会收到理由</small></label>'
        "</fieldset>"
        '<div class="note" id="note-box" hidden>'
        '<label for="note">拒绝理由（必填，客户可见）</label>'
        '<textarea id="note" name="note" maxlength="200" placeholder="例如：商品已发货，需走退货流程"></textarea>'
        "</div>"
        f'<button type="submit" id="submit">{_esc(approve_label)}</button>'
        "</form>"
        '<p class="foot">此链接 15 分钟内有效，只能使用一次。处理后客户立即收到结果，操作不可撤销。</p>'
        "</div></article>"
        "<script>"
        "(function(){"
        'var box=document.getElementById("note-box"),note=document.getElementById("note"),'
        'btn=document.getElementById("submit");'
        f'var labels={{"approve":{json.dumps(approve_label, ensure_ascii=False)},"reject":"确认拒绝"}};'
        'function sync(){'
        'var v=document.querySelector(\'input[name="action"]:checked\').value;'
        'var reject=v==="reject";'
        'box.hidden=!reject;note.required=reject;'
        'btn.textContent=labels[v];btn.style.background=reject?"#B23A2E":"' + ACCENT + '";'
        "}"
        'Array.prototype.forEach.call(document.querySelectorAll(\'input[name="action"]\'),'
        'function(r){r.addEventListener("change",sync)});'
        "sync();"
        "})();"
        "</script>"
    )
    return _page(f"审批 · {title}", body)


def render_decision_notice_page(
    *,
    title: str,
    message: str,
    tone: str = "mute",
    detail: str | None = None,
) -> str:
    """终态 / 错误提示页（已处理、已过期、链接无效）。不抛 JSON 给坐席看。"""
    stripe = {"ok": "ok", "bad": "bad"}.get(tone, "mute")
    detail_html = f'<p class="detail">{_esc(detail)}</p>' if detail else ""
    body = (
        '<div class="brand"><b>澄元</b><span>坐席审批</span></div>'
        f'<article class="card"><div class="stripe {stripe}"></div><div class="body">'
        f"<h1>{_esc(title)}</h1>"
        f'<div class="notice {_esc(tone)}">{_esc(message)}{detail_html}</div>'
        '<p class="foot">如需继续处理，请在 Chatwoot 会话里联系客户或重新发起申请。</p>'
        "</div></article>"
    )
    return _page(title, body)
