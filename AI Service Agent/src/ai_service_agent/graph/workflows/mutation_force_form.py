"""High-confidence Mutation Select binding that does not wait for a tool_call.

Used only after Select would Operation-Clarify (no tool). A unique phrase in the
current user turn, whose tool is on the allowlist, becomes pending_tool so Form
collects the missing fields. Zero hits, two or more hits, or an explicit
ambiguity phrase stay on Clarify. Fields are copied from the utterance only.
"""

from collections.abc import Collection
import re

# Negation immediately in front of the verb: 不改 / 不要改 / 不用改 / 别改.
_NOT_NEGATED = r"(?<!不)(?<!不要)(?<!不用)(?<!别)"

_ADDRESS_RE = re.compile(
    _NOT_NEGATED
    + r"(?:"
    r"(?:修改|更新|换成|改为|改成|更换|换个|换下|改一下|改下|改个|换|改)"
    r"(?:一下|下|个)?"
    r"(?:新)?"
    r"(?:的)?"
    r"(?:订单)?"
    r"(?:的)?"
    r"(?:收货)?"
    r"地址"
    r"|(?:收货|订单)?地址"
    r"(?:修改|更新|换成|改为|改成|更换|换一下|换下|换个|换|改一下|改下|改)"
    r")"
)
_PHONE_RE = re.compile(
    _NOT_NEGATED
    + r"(?:"
    r"(?:修改|更新|换成|改为|改成|更换|换绑|换个|换下|改一下|改下|改个|换|改)"
    r"(?:一下|下|个)?"
    r"(?:新)?"
    r"(?:的)?"
    r"(?:联系)?"
    r"(?:电话|手机号码|手机号)"
    r"|(?:联系)?(?:电话|手机号码|手机号)"
    r"(?:修改|更新|换成|改为|改成|更换|换绑|换一下|换下|换个|换|改一下|改下|改)"
    r")"
)
_NOTE_RE = re.compile(
    _NOT_NEGATED
    + r"(?:"
    r"(?:修改|更新|加上|添加|加个|加一下|加下|改成|改为|改一下|改下|改个|加|改)"
    r"(?:一下|下|个)?"
    r"(?:订单)?"
    r"备注"
    r"|(?:订单)?备注"
    r"(?:修改|更新|加上|添加|加一下|加下|改成|改为|改一下|改下|加|改)"
    r"|订单备注"
    r")"
)
# 开 + 可选量词/助词 + 发票：开发票、开个发票、开一张发票、开张发票、补开发票。
# 开具发票单列（「具」不是量词）。开票保持 (?<!离)，避免「离开票」。
_INVOICE_RE = re.compile(
    _NOT_NEGATED
    + r"(?:"
    r"开(?:一下|一张|一份|个|张|份|下)?发票"
    r"|开具发票"
    r"|要发票"
    r"|索要发票"
    r"|(?<!离)开票"
    r")"
)
_TICKET_RE = re.compile(
    _NOT_NEGATED + r"(?:申请售后|开售后工单|提交售后|发起售后|办理售后|办售后|售后工单|要售后)"
)

# First-version table. A hit counts only when the tool is on the current allowlist.
_FORCE_BINDINGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("update_address", _ADDRESS_RE),
    ("update_phone", _PHONE_RE),
    ("update_order_note", _NOTE_RE),
    ("create_invoice", _INVOICE_RE),
    ("create_service_ticket", _TICKET_RE),
)

_ORDER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(ORD-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)",
    re.IGNORECASE,
)
_LABELED_ORDER_RE = re.compile(
    r"(?:订单号|单号)\s*[:：是为]?\s*([A-Za-z0-9][A-Za-z0-9-]{2,})"
)
_MOBILE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_ADDRESS_INTRO_RE = re.compile(
    r"(?:收货地址|地址)\s*(?:改成|改为|换成|更新为|修改为|改到|是|为|:|：)\s*"
    r"([^，。,；;\n]{2,80})"
)
_ADDRESS_BARE_RE = re.compile(
    r"(?:改成|改为|换成|更新为|修改为|改到)\s*([^，。,；;\n]{2,80})"
)
_ADDRESS_HINT_RE = re.compile(r"[省市区县路街道路号栋室园苑层村镇巷弄楼]")
_ADDRESS_REJECT = frozenset(
    {
        "一下",
        "下",
        "个",
        "新",
        "新的",
        "新地址",
        "收货地址",
        "地址",
        "什么",
        "多少",
        "哪里",
        "哪儿",
        "哪个",
        "这个",
        "那个",
    }
)
_NOTE_VALUE_RE = re.compile(r"备注\s*[:：是为]\s*([^\n。；;]{1,120})")
_COMPANY_RE = re.compile(
    r"(?:发票抬头|抬头|公司名称)\s*[:：是为]?\s*([^，,。；;\s税]{2,40})"
)
_TAX_RE = re.compile(
    r"(?:纳税人识别号|统一社会信用代码|税号)\s*[:：是为]?\s*([0-9A-Za-z]{15,20})"
)
_AMOUNT_RE = re.compile(r"(?:开票金额|金额)\s*[:：为]?\s*(\d+(?:\.\d{1,2})?)\s*元?")
_REASON_RE = re.compile(
    r"(?:申请售后|开售后工单|售后工单|原因|问题是)\s*[:：，,]\s*([^\n。；;]{2,80})"
)
_TRAILING_PARTICLE_RE = re.compile(r"(?:谢谢|多谢|啦|吧|啊|呀|呢|哦|嘛)+$")


def unique_mutation_tool(user_text: str, allowed_tools: Collection[str]) -> str | None:
    """The one allowlisted tool this utterance names, or None when it is not unique."""
    text = (user_text or "").strip()
    if not text:
        return None
    allowed = {str(name) for name in allowed_tools}
    if _explicit_ambiguity(text, allowed):
        return None
    hits = [
        name
        for name, pattern in _FORCE_BINDINGS
        if name in allowed and pattern.search(text)
    ]
    if len(hits) == 1:
        return hits[0]
    return None


def partial_args_from_utterance(tool_name: str, user_text: str) -> dict:
    """Schema fields literally present in this turn. Missing fields stay absent."""
    text = user_text or ""
    order_id = _extract_order_id(text)
    if tool_name == "update_address":
        return _present(order_id=order_id, address=_extract_address(text))
    if tool_name == "update_phone":
        return _present(phone=_extract_phone(text))
    if tool_name == "update_order_note":
        return _present(order_id=order_id, note=_extract_note(text))
    if tool_name == "create_invoice":
        return _present(
            order_id=order_id,
            company=_extract_company(text),
            tax_no=_extract_tax_no(text),
            amount=_extract_amount(text),
        )
    if tool_name == "create_service_ticket":
        return _present(order_id=order_id, reason=_extract_reason(text))
    return {}


def _explicit_ambiguity(text: str, allowed: set[str]) -> bool:
    """Phrases that must stay on Clarify even if a nearby keyword also matches.

    「改联系方式」with both address and phone allowed, and no more specific
    noun, is the contact case. Invoice and note named together cannot be split.
    """
    if {"update_address", "update_phone"} <= allowed and "联系方式" in text:
        if re.search(r"地址|电话|手机号|手机号码", text) is None:
            return True
    if {"create_invoice", "update_order_note"} <= allowed:
        if re.search(r"开票|发票", text) and "备注" in text:
            return True
    return False


def _present(**fields) -> dict:
    return {key: value for key, value in fields.items() if value not in (None, "")}


def _clean(value: str) -> str:
    text = value.strip().strip("\"'“”‘’").strip()
    return _TRAILING_PARTICLE_RE.sub("", text).strip()


def _extract_order_id(text: str) -> str | None:
    token = _ORDER_TOKEN_RE.search(text)
    if token:
        return token.group(1)
    labeled = _LABELED_ORDER_RE.search(text)
    if labeled is None:
        return None
    value = labeled.group(1)
    if _MOBILE_RE.fullmatch(value) or re.search(r"\d", value) is None:
        return None
    return value


def _extract_address(text: str) -> str | None:
    introduced = _ADDRESS_INTRO_RE.search(text)
    if introduced:
        value = _accept_address(_clean(introduced.group(1)), require_hint=False)
        if value:
            return value
    bare = _ADDRESS_BARE_RE.search(text)
    if bare is None:
        return None
    return _accept_address(_clean(bare.group(1)), require_hint=True)


def _accept_address(value: str, *, require_hint: bool) -> str | None:
    if len(value) < 4 or value in _ADDRESS_REJECT:
        return None
    if require_hint and _ADDRESS_HINT_RE.search(value) is None:
        return None
    return value


def _extract_phone(text: str) -> str | None:
    match = _MOBILE_RE.search(text)
    return match.group(1) if match else None


def _extract_note(text: str) -> str | None:
    match = _NOTE_VALUE_RE.search(text)
    if match is None:
        return None
    value = _clean(match.group(1))
    if not value or value in _ADDRESS_REJECT:
        return None
    return value


def _extract_company(text: str) -> str | None:
    match = _COMPANY_RE.search(text)
    if match is None:
        return None
    value = _clean(match.group(1))
    return value or None


def _extract_tax_no(text: str) -> str | None:
    match = _TAX_RE.search(text)
    return match.group(1) if match else None


def _extract_amount(text: str) -> float | None:
    match = _AMOUNT_RE.search(text)
    if match is None:
        return None
    amount = float(match.group(1))
    if amount <= 0:
        return None
    return amount


def _extract_reason(text: str) -> str | None:
    match = _REASON_RE.search(text)
    if match is None:
        return None
    value = _clean(match.group(1))
    if len(value) < 2:
        return None
    return value
