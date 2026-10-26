"""Mutation Form 的 Chatwoot 投影（items / title / 地址表单 / 薄发送）。

从 ``agent_bot.py`` 抽出：建造 form items、title、地址表单项，以及把
``form_required`` / ``form_rejected`` 投到 Chatwoot ``form``。投影与入站解析、
图 resume 同住适配器时，无法脱离 ``handle_agent_bot_message`` 单测。

本模块只做「form 事件载荷 → Chatwoot form items / 出站」。不含入站决策
（``decide_form_inbound`` / ``form_gate``）、不含 ``Command(resume=...)``、
不含图 invoke。Chatwoot HTTP 只出现在薄发送函数里，由调用方传入 client。
"""

from typing import Any

from ai_service_agent.graph.workflows.copy import (
    FIELD_LABELS,
    update_address_current_address_display,
    update_address_empty_state_text,
    update_address_form_footnote_text,
    update_address_form_intro_text,
    update_address_form_subtitle_text,
    update_address_form_title_text,
    update_address_new_address_placeholder,
)
from ai_service_agent.graph.workflows.form_resume import FORM_RESUME_SKIP_FIELDS
from ai_service_agent.harness.chatwoot import ChatwootClient

# Form Resume schema keys that are not Mutation Form input widgets live in
# graph/workflows/form_resume.py (single source of truth): FORM_RESUME_SKIP_FIELDS.

_MUTATION_FORM_ACTION_ITEM: dict[str, Any] = {
    "name": "action",
    "label": "确认或取消",
    "type": "select",
    "options": [
        {"label": "确认", "value": "confirm"},
        {"label": "取消", "value": "cancel"},
    ],
}


def _form_errors_map(data: dict[str, Any]) -> dict[str, str]:
    """Normalize ``form_rejected`` errors for Chatwoot copy (no native error field)."""
    raw = data.get("errors")
    if not isinstance(raw, dict) or not raw:
        return {}
    return {str(key): str(value) for key, value in raw.items() if value not in (None, "", [], {})}


def _is_update_address_form(data: dict[str, Any]) -> bool:
    return data.get("tool") == "update_address"


def mutation_form_title_from_form_data(data: dict[str, Any]) -> str:
    """Chatwoot form ``content``: summary plus form-level / leftover error copy."""
    if _is_update_address_form(data):
        # Title + limit subtitle in content; footnote is a trailing form item (§2.1).
        title = "\n".join(
            [
                update_address_form_title_text(),
                update_address_form_subtitle_text(),
            ]
        )
    else:
        title = data.get("summary") or "请填写表单"
    errors = _form_errors_map(data)
    if not errors:
        return title
    # Field errors are also painted onto matching items; ``_form`` and any
    # unmatched keys still need a visible place in the message copy.
    item_names = {"order_id", "action", "current_address", "address"}
    explicit = data.get("fields")
    if isinstance(explicit, list):
        for field in explicit:
            if isinstance(field, dict) and field.get("name"):
                item_names.add(str(field["name"]))
    schema = data.get("response_schema") if isinstance(data.get("response_schema"), dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    item_names.update(str(name) for name in properties)
    leftover = [
        message
        for key, message in errors.items()
        if key == "_form" or key not in item_names
    ]
    if not leftover:
        # Still surface the first field error in the title — Chatwoot select
        # widgets have no placeholder, and long labels may truncate.
        leftover = [next(iter(errors.values()))]
    return f"{title}\n{'；'.join(leftover)}"


def _apply_errors_to_mutation_form_items(
    items: list[dict[str, Any]],
    errors: dict[str, str],
) -> list[dict[str, Any]]:
    """Paint field errors onto Chatwoot items (label/placeholder — no error API)."""
    if not errors:
        return items
    for item in items:
        name = item.get("name")
        if not name or name not in errors:
            continue
        message = errors[name]
        if item.get("type") == "select":
            base = item.get("label") or str(name)
            item["label"] = f"{base}（{message}）"
        else:
            item["placeholder"] = message
    return items


def order_picker_options(candidates: Any) -> list[dict[str, str]]:
    """Order-select options: ``label`` and ``value`` are the order id only.

    Product and status text overflow the widget's native ``<select>`` menu.
    The order number alone stays inside the control, left-aligned.
    """
    options: list[dict[str, str]] = []
    if not isinstance(candidates, list):
        return options
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        order_id = candidate.get("order_id")
        if not order_id:
            continue
        order_id = str(order_id)
        options.append({"label": order_id, "value": order_id})
    return options


def _update_address_form_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Locked open-form / form_rejected IA for update_address (#71). No action select."""
    prefills = data.get("prefills") if isinstance(data.get("prefills"), dict) else {}
    items: list[dict[str, Any]] = [
        {
            "name": "current_address",
            "label": "当前收货地址（只读）",
            "type": "text",
            "default": update_address_current_address_display(data.get("current_address")),
        },
    ]
    options = order_picker_options(data.get("order_candidates"))
    order_item: dict[str, Any] = {
        "name": "order_id",
        "label": "选择订单",
        "type": "select",
        "options": options,
        "placeholder": "请选择要修改的订单",
    }
    # Open form: never auto-preselect. form_rejected: keep draft via prefills.
    if _form_errors_map(data) and prefills.get("order_id") is not None:
        order_item["default"] = prefills["order_id"]
    items.append(order_item)
    address_item: dict[str, Any] = {
        "name": "address",
        "label": "新收货地址",
        "type": "text",
        "placeholder": update_address_new_address_placeholder(),
    }
    if prefills.get("address") is not None:
        address_item["default"] = prefills["address"]
    items.append(address_item)
    items.append(
        {
            "name": "abandon_footnote",
            "label": "说明",
            "type": "text",
            "default": update_address_form_footnote_text(),
        }
    )
    return _apply_errors_to_mutation_form_items(items, _form_errors_map(data))


def mutation_form_items_from_form_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build Chatwoot ``form`` items from a ``form_required`` / ``form_rejected`` payload.

    ``update_address`` uses the locked four-state open-form layout (#71): read-only
    current address, paid Order Picker (no preselect), new address, footnote, no action select.
    Other mutation tools keep Order Picker + schema fields + Confirm/Cancel select.
    ``form_rejected`` ``errors`` are projected into item label/placeholder and
    title copy within what Chatwoot native forms allow.
    """
    if _is_update_address_form(data):
        return _update_address_form_items(data)

    prefills = data.get("prefills") if isinstance(data.get("prefills"), dict) else {}
    items: list[dict[str, Any]] = []

    options = order_picker_options(data.get("order_candidates"))
    order_item: dict[str, Any] = {
        "name": "order_id",
        "label": "选择订单",
        "type": "select",
        "options": options,
    }
    if prefills.get("order_id") is not None:
        order_item["default"] = prefills["order_id"]
    items.append(order_item)

    explicit_fields = data.get("fields")
    if isinstance(explicit_fields, list) and explicit_fields:
        for field in explicit_fields:
            if not isinstance(field, dict):
                continue
            name = field.get("name")
            if not name or name in FORM_RESUME_SKIP_FIELDS or name == "order_id":
                continue
            item = {
                "name": str(name),
                "label": field.get("label") or FIELD_LABELS.get(str(name), str(name)),
                "type": field.get("type") or "text",
            }
            if field.get("placeholder") is not None:
                item["placeholder"] = field["placeholder"]
            if field.get("options") is not None:
                item["options"] = field["options"]
            if prefills.get(name) is not None:
                item["default"] = prefills[name]
            elif field.get("default") is not None:
                item["default"] = field["default"]
            items.append(item)
    else:
        schema = data.get("response_schema") if isinstance(data.get("response_schema"), dict) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for name, prop in properties.items():
            if name in FORM_RESUME_SKIP_FIELDS or name == "order_id":
                continue
            label = FIELD_LABELS.get(name, name)
            item = {
                "name": name,
                "label": label,
                "type": "text",
                "placeholder": f"请输入{label}",
            }
            if isinstance(prop, dict) and prop.get("enum"):
                item["type"] = "select"
                item["options"] = [
                    {"label": str(value), "value": value} for value in prop["enum"]
                ]
                item.pop("placeholder", None)
            if prefills.get(name) is not None:
                item["default"] = prefills[name]
            items.append(item)

    items.append(dict(_MUTATION_FORM_ACTION_ITEM))
    return _apply_errors_to_mutation_form_items(items, _form_errors_map(data))


async def send_mutation_form(
    chatwoot: ChatwootClient,
    conversation_id: str,
    form_data: dict[str, Any],
) -> None:
    """Project Form Interrupt ``form_required`` / ``form_rejected`` to Chatwoot ``form``.

    ``update_address`` empty candidates → plain-text empty state only (#71).
    With candidates → intro bubble then locked form (no empty picker).
    """
    if _is_update_address_form(form_data):
        candidates = form_data.get("order_candidates") or []
        errors = form_data.get("errors")
        # Empty state only on first open (no errors). form_rejected must re-push form.
        if not candidates and errors in (None, {}, [], ""):
            await chatwoot.send_bot_message(
                conversation_id, update_address_empty_state_text()
            )
            return
        if errors in (None, {}, [], ""):
            await chatwoot.send_bot_message(
                conversation_id, update_address_form_intro_text()
            )
    title = mutation_form_title_from_form_data(form_data)
    items = mutation_form_items_from_form_data(form_data)
    await chatwoot.send_interactive_message(
        conversation_id,
        title,
        "form",
        {"items": items},
    )
