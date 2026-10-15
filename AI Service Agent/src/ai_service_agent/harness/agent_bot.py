"""Chatwoot AgentBot webhook handler（Transport/harness 层）。"""

import uuid
from typing import Any

from langchain_core.messages import HumanMessage

from ai_service_agent.harness.chatwoot import ChatwootClient, chatwoot_message_id
from ai_service_agent.graph.handoff_rules import detect_handoff_cancel, detect_handoff_intent
from ai_service_agent.graph.workflows.copy import (
    FIELD_LABELS,
    handoff_cancel_confirm_text,
    handoff_request_confirm_text,
    handoff_request_private_note_text,
    human_mode_tip_text,
    update_address_current_address_display,
    update_address_empty_state_text,
    update_address_form_footnote_text,
    update_address_form_intro_text,
    update_address_form_subtitle_text,
    update_address_form_title_text,
    update_address_new_address_placeholder,
    update_address_success_card_description,
    update_address_success_card_title_text,
    update_address_success_followup_text,
)
from ai_service_agent.harness.form_events import is_form_interrupt
from ai_service_agent.harness.resume import approval_dispatch, resolve_resume_payload
from ai_service_agent.harness.streaming import event_stream

# Form Resume schema keys that are not Mutation Form input widgets.
# ``action`` is appended explicitly as Confirm/Cancel control (not from schema loop).
# ``current_address`` is read-only projection for update_address (#71).
_FORM_SCHEMA_SKIP_FIELDS = frozenset(
    {"action", "request_id", "current_address", "abandon_footnote"}
)

_MUTATION_FORM_ACTION_ITEM: dict[str, Any] = {
    "name": "action",
    "label": "确认或取消",
    "type": "select",
    "options": [
        {"label": "确认", "value": "confirm"},
        {"label": "取消", "value": "cancel"},
    ],
}

# Seat Approver Approval Side-Channel form fields (ADR-0002 / #64).
# Customer Widget must never treat these as Mutation Form Resume.
_APPROVAL_FORM_ID_FIELD = "approval_id"
_APPROVAL_FORM_DECISION_FIELD = "decision"
_APPROVAL_FORM_NOTE_FIELD = "note"
_APPROVAL_FORM_DECISION_ITEM: dict[str, Any] = {
    "name": _APPROVAL_FORM_DECISION_FIELD,
    "label": "审批决定",
    "type": "select",
    "options": [
        {"label": "批准", "value": "approve"},
        {"label": "拒绝", "value": "reject"},
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


def _update_address_order_option_label(candidate: dict[str, Any]) -> str:
    order_id = str(candidate.get("order_id") or "")
    product = (candidate.get("product_summary") or "").strip()
    # Candidates for update_address are paid-only; label locked as 订单号 · 已支付 · 短名.
    if product:
        return f"{order_id} · 已支付 · {product}"
    return f"{order_id} · 已支付"


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
    options: list[dict[str, str]] = []
    for candidate in data.get("order_candidates") or []:
        if not isinstance(candidate, dict) or not candidate.get("order_id"):
            continue
        options.append(
            {
                "label": _update_address_order_option_label(candidate),
                "value": str(candidate["order_id"]),
            }
        )
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

    options: list[dict[str, str]] = []
    for candidate in data.get("order_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        order_id = candidate.get("order_id")
        if not order_id:
            continue
        detail = candidate.get("product_summary") or candidate.get("status_display") or ""
        label = f"{order_id} · {detail}" if detail else str(order_id)
        options.append({"label": label, "value": str(order_id)})
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
            if not name or name in _FORM_SCHEMA_SKIP_FIELDS or name == "order_id":
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
            if name in _FORM_SCHEMA_SKIP_FIELDS or name == "order_id":
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


def _normalize_submitted_values(value: Any) -> dict[str, Any] | None:
    """兼容 Chatwoot form 提交的 object 与 widget/agent-bot 的 [{name, value}] 数组。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, list):
        return None
    normalized: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("title")
        if name is not None and "value" in item:
            normalized[str(name)] = item["value"]
    return normalized or None


def approval_form_title_from_payload(data: dict[str, Any]) -> str:
    """Seat-facing Chatwoot form title: enough summary to decide (#64)."""
    summary = (data.get("summary") or "").strip() or "高风险操作待审批"
    approval_id = (data.get("approval_id") or "").strip()
    expires_at = (data.get("expires_at") or "").strip()
    parts = [f"高风险审批 · {summary}"]
    if approval_id:
        parts.append(f"单号 {approval_id}")
    if expires_at:
        parts.append(f"截止 {expires_at}")
    return "\n".join(parts)


def approval_form_items_from_payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build private Chatwoot ``form`` items for Seat Approver approve/reject."""
    approval_id = str(data.get("approval_id") or "")
    items: list[dict[str, Any]] = [
        {
            "name": _APPROVAL_FORM_ID_FIELD,
            "label": "审批单号",
            "type": "text",
            "default": approval_id,
        },
        dict(_APPROVAL_FORM_DECISION_ITEM),
        {
            "name": _APPROVAL_FORM_NOTE_FIELD,
            "label": "拒绝备注（可选）",
            "type": "text",
            "placeholder": "拒绝时填写原因",
        },
    ]
    return items


def parse_approval_form_submission(
    submitted: dict[str, Any] | list | None,
) -> dict[str, str] | None:
    """Parse seat approval form submit → ``{approval_id, action, note?}`` or None."""
    values = _normalize_submitted_values(submitted) if not isinstance(submitted, dict) else submitted
    if not values:
        return None
    approval_id = str(values.get(_APPROVAL_FORM_ID_FIELD) or "").strip()
    action = str(values.get(_APPROVAL_FORM_DECISION_FIELD) or "").strip()
    if not approval_id or action not in ("approve", "reject"):
        return None
    parsed: dict[str, str] = {"approval_id": approval_id, "action": action}
    note = values.get(_APPROVAL_FORM_NOTE_FIELD)
    if isinstance(note, str) and note.strip():
        parsed["note"] = note.strip()
    return parsed


async def send_approval_form(
    chatwoot: ChatwootClient,
    conversation_id: str,
    data: dict[str, Any],
) -> None:
    """Project ``approval_required`` to a private seat-facing Chatwoot form (#64).

    ``private=True`` keeps Approve/Reject off the customer Widget (ADR-0001).
    """
    title = approval_form_title_from_payload(data)
    items = approval_form_items_from_payload(data)
    await chatwoot.send_interactive_message(
        conversation_id,
        title,
        "form",
        {"items": items},
        private=True,
    )

def _contact_identifier(contact: dict[str, Any]) -> str | None:
    """Contact-first Identity：非空 contact.identifier（Widget setUser / HMAC 绑定后的登录 user_id）。

    Chatwoot 在 Website Channel 校验 Identity HMAC；AgentBot 侧只接受已落到 contact 的 identifier。
    v1 无匿名路径（CONTEXT.md Contact-first Binding）。
    """
    raw = contact.get("identifier")
    if not isinstance(raw, str):
        return None
    identifier = raw.strip()
    return identifier or None


async def handle_agent_bot_message(
    payload: dict[str, Any],
    chatwoot: ChatwootClient,
    graph=None,
    status=None,
    messages_store=None,
    mapping=None,
    profile_loader=None,
    orders_loader=None,
    on_interrupt=None,
    thread_lock_factory=None,
    chatwoot_lock_factory=None,
    callbacks_factory=None,
    on_handoff_created=None,
    on_approval_pending=None,
    handoff=None,
    context=None,
) -> dict[str, Any] | None:
    """处理 AgentBot incoming 消息，并回写 outgoing 消息。"""
    event = payload.get("event")
    if event not in ("message_created", "message_updated"):
        return None

    message_type = payload.get("message_type")
    is_form_update = (
        event == "message_updated"
        and payload.get("content_type") in ("form", "input_select")
    )
    if message_type not in ("incoming", 0) and not is_form_update:
        return None

    raw_submitted = (payload.get("content_attributes") or {}).get("submitted_values")
    submitted_values = _normalize_submitted_values(raw_submitted)
    if event == "message_updated" and submitted_values is None:
        return None

    # Approval Side-Channel is seat-only (#64 / ADR-0001): contact Widget form submit
    # of an approval card must never become a seat decision or Mutation Form Resume.
    if submitted_values is not None and parse_approval_form_submission(submitted_values) is not None:
        return None

    if submitted_values:
        # Form-Only Write / #48: never NL-rewrite address/order submits into Router turns.
        # Confirm/Cancel and field values stay structured for Form Resume or refuse path.
        content = ", ".join(f"{key}={value}" for key, value in submitted_values.items())
    else:
        content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    conversation = payload.get("conversation")
    if not isinstance(conversation, dict):
        return None
    try:
        cw_conv_id = int(conversation.get("id"))
    except (TypeError, ValueError):
        return None

    contact = payload.get("contact") or (conversation.get("meta") or {}).get("sender")
    if not isinstance(contact, dict):
        contact = {}
    contact_id = contact.get("id")

    if chatwoot_lock_factory is not None:
        conversation_lock = await chatwoot_lock_factory(f"chatwoot:{cw_conv_id}")
    else:
        conversation_lock = None

    locked_kwargs = dict(
        payload=payload,
        chatwoot=chatwoot,
        graph=graph,
        status=status,
        messages_store=messages_store,
        mapping=mapping,
        profile_loader=profile_loader,
        orders_loader=orders_loader,
        on_interrupt=on_interrupt,
        thread_lock_factory=thread_lock_factory,
        callbacks_factory=callbacks_factory,
        on_handoff_created=on_handoff_created,
        on_approval_pending=on_approval_pending,
        handoff=handoff,
        context=context,
        conversation=conversation,
        cw_conv_id=cw_conv_id,
        contact=contact,
        contact_id=contact_id,
        content=content.strip(),
        submitted_values=submitted_values,
    )
    if conversation_lock is not None:
        async with conversation_lock:
            return await _handle_locked_agent_bot_message(**locked_kwargs)
    return await _handle_locked_agent_bot_message(**locked_kwargs)


async def _handle_locked_agent_bot_message(
    *,
    payload: dict[str, Any],
    chatwoot: ChatwootClient,
    graph=None,
    status=None,
    messages_store=None,
    mapping=None,
    profile_loader=None,
    orders_loader=None,
    on_interrupt=None,
    thread_lock_factory=None,
    callbacks_factory=None,
    on_handoff_created=None,
    on_approval_pending=None,
    handoff=None,
    context=None,
    conversation: dict[str, Any],
    cw_conv_id: int,
    contact: dict[str, Any],
    contact_id: Any,
    content: str,
    submitted_values: dict[str, Any] | None,
) -> dict[str, Any] | None:
    user_id = _contact_identifier(contact)
    if user_id is None:
        # 无 validated Identity：拒绝建会话 / 跑图，不走匿名 chatwoot-* 回退
        return None

    rec = mapping.find(cw_conv_id) if mapping is not None else None
    if rec is not None:
        conversation_id = rec["conversation_id"]
        thread_id = status.get_thread_id(conversation_id) if status is not None else conversation_id
    else:
        if status is not None:
            session = status.create_session(user_id)
            conversation_id = session["conversation_id"]
            thread_id = session["thread_id"]
        else:
            conversation_id = str(uuid.uuid4())
            thread_id = conversation_id
        if mapping is not None:
            mapping.put(conversation_id, cw_conv_id, contact_id)

    if status is not None and status.get_mode(conversation_id) != "ai":
        # Human lane (handoff-spec §2): Message Store only + fixed tip; no Graph.
        if messages_store is not None:
            message_id = chatwoot_message_id(payload)
            if message_id and messages_store.get_by_message_id(message_id):
                return None
            messages_store.append(conversation_id, "user", content, message_id=message_id or None)
        tip = human_mode_tip_text()
        await chatwoot.send_bot_message(conversation_id, tip)
        return {
            "conversation_id": conversation_id,
            "chatwoot_conversation_id": cw_conv_id,
            "response": tip,
        }

    if callbacks_factory is not None:
        on_interrupt, on_handoff_requested, on_approval_pending = callbacks_factory(
            conversation_id, thread_id
        )
    else:
        on_handoff_requested = on_handoff_created

    if messages_store is not None:
        message_id = chatwoot_message_id(payload)
        if message_id and messages_store.get_by_message_id(message_id):
            return None
        messages_store.append(conversation_id, "user", content, message_id=message_id or None)
        history = messages_store.hydrate(conversation_id)
    else:
        history = [HumanMessage(content=content)]

    # Seat-gated entry detectors (handoff-spec §2): confirm/cancel only; no Graph.
    if handoff is not None and submitted_values is None:
        if handoff.get_awaiting(conversation_id) is not None and detect_handoff_cancel(content):
            handoff.cancel_handoff_request(conversation_id)
            tip = handoff_cancel_confirm_text()
            if messages_store is not None:
                messages_store.append(conversation_id, "assistant", tip)
            await chatwoot.send_bot_message(conversation_id, tip)
            return {
                "conversation_id": conversation_id,
                "chatwoot_conversation_id": cw_conv_id,
                "response": tip,
            }
        if detect_handoff_intent(content):
            ev = handoff.request_handoff(conversation_id, "用户显式请求转人工", content[:80])
            if on_handoff_requested is not None:
                await on_handoff_requested(ev)
            else:
                tip = handoff_request_confirm_text()
                if messages_store is not None:
                    messages_store.append(conversation_id, "assistant", tip)
                await chatwoot.send_bot_message(conversation_id, tip)
                try:
                    await chatwoot.prompt_handoff_request(
                        conversation_id,
                        handoff_request_private_note_text((ev.get("data") or {}).get("summary") or ""),
                    )
                except Exception:
                    pass
            tip = handoff_request_confirm_text()
            return {
                "conversation_id": conversation_id,
                "chatwoot_conversation_id": cw_conv_id,
                "response": tip,
            }

    mutation_form_data = None
    approval_form_data = None
    final_message_id = None
    # #73: capture update_address confirm fields for success-card projection.
    address_success: dict[str, str] | None = None
    address_write_ok = False
    aborted_for_human = False

    if graph is None:
        final_text = (
            f"[AgentBot] 收到表单：{content}"
            if submitted_values is not None
            else f"[AgentBot] Received: {content}"
        )
    else:
        config = {"configurable": {"thread_id": thread_id}}
        final_text = None
        resume_payload = None
        # #72 Implicit Cancel: pending Form Interrupt + non-form inbound →
        # silent cancel resume, then normal new-turn invoke (same thread_id / lock).
        implicit_cancel = False
        if getattr(graph, "checkpointer", None):
            state = await graph.aget_state(config)
            if state.interrupts:
                interrupt_value = state.interrupts[0].value
                approval_state = approval_dispatch(interrupt_value)
                if approval_state == "pending":
                    if on_approval_pending is not None:
                        await on_approval_pending(interrupt_value)
                    return None
                if approval_state == "expired":
                    resume_payload = {"result": "expired"}
                elif is_form_interrupt(interrupt_value):
                    submitted = submitted_values or {}
                    action = submitted.get("confirmation") or submitted.get("action")
                    # #71: update_address primary submit has no action select — business
                    # fields alone mean confirm. Ignore read-only / footnote echo fields.
                    _resume_skip = frozenset(
                        {
                            "action",
                            "confirmation",
                            "request_id",
                            "current_address",
                            "abandon_footnote",
                        }
                    )
                    write_keys = {
                        key
                        for key, value in submitted.items()
                        if key not in _resume_skip and value not in (None, "")
                    }
                    primary_submit_confirm = (
                        not action
                        and write_keys
                        and submitted_values is not None
                        and interrupt_value.get("tool") == "update_address"
                    )
                    if action in ("confirmed", "confirm") or primary_submit_confirm:
                        resume_payload = {"action": "confirm"}
                        for key, value in submitted.items():
                            if key in _resume_skip:
                                continue
                            resume_payload[key] = value
                        if interrupt_value.get("tool") == "update_address":
                            order_id = resume_payload.get("order_id")
                            address = resume_payload.get("address")
                            if order_id and address:
                                address_success = {
                                    "order_id": str(order_id),
                                    "address": str(address),
                                }
                    elif action in ("canceled", "cancel"):
                        resume_payload = {"action": "cancel"}
                    else:
                        # Non-form inbound while Form Interrupt pending (#72).
                        implicit_cancel = True
                else:
                    # #48: legacy confirmation_required interactive retired; no special resume.
                    resume_payload = resolve_resume_payload(interrupt_value, content)

        async def _consume_graph_events(*, messages, resume):
            nonlocal final_text, mutation_form_data, approval_form_data, final_message_id
            nonlocal address_write_ok
            async for event in event_stream(
                graph,
                config,
                messages,
                on_interrupt=on_interrupt,
                resume_payload=resume,
                extra_state={"user_id": user_id},
                context=context,
            ):
                if event.get("type") == "done":
                    final_text = event.get("data", {}).get("final")
                    final_message_id = event.get("data", {}).get("message_id")
                elif event.get("type") in ("form_required", "form_rejected"):
                    # form_rejected: same Form Interrupt session with errors
                    # (Closed MutationGraph); Adapter only projects Transport.
                    mutation_form_data = event.get("data") or {}
                elif event.get("type") == "tool_result":
                    data = event.get("data") or {}
                    if data.get("name") == "update_address" and data.get("ok"):
                        address_write_ok = True
                elif event.get("type") == "approval_required":
                    # Seat Approver side-channel card (#64); private form, not Widget.
                    approval_form_data = event.get("data") or {}
                elif event.get("type") == "handoff_requested" and on_handoff_requested is not None:
                    await on_handoff_requested(event)
                # #48: clarification_required / confirmation_required interactive retired.
                # Operation Clarify and HighRisk missing-fields stay outbound chat (done.final).

        async def run_graph():
            nonlocal final_text, mutation_form_data, approval_form_data, final_message_id
            nonlocal aborted_for_human
            if status is not None and status.get_mode(conversation_id) != "ai":
                # Takeover cancelled the form under this lock: do not Form Resume.
                aborted_for_human = True
                final_text = human_mode_tip_text()
                return
            if (
                isinstance(resume_payload, dict)
                and resume_payload.get("action") == "confirm"
                and getattr(graph, "checkpointer", None)
            ):
                fresh = await graph.aget_state(config)
                if not fresh.interrupts or not is_form_interrupt(fresh.interrupts[0].value):
                    return
            if implicit_cancel:
                # Phase 1: official cancel resume only — drain events; do not project
                # cancel tip or re-push the old form (silent Implicit Cancel).
                async for _event in event_stream(
                    graph,
                    config,
                    [],
                    on_interrupt=on_interrupt,
                    resume_payload={"action": "cancel"},
                    extra_state={"user_id": user_id},
                    context=context,
                ):
                    pass
                final_text = None
                mutation_form_data = None
                approval_form_data = None
                final_message_id = None
                # Phase 2: same user message as a normal new turn on the same thread.
                await _consume_graph_events(messages=history, resume=None)
            else:
                await _consume_graph_events(messages=history, resume=resume_payload)

        if thread_lock_factory is not None:
            async with await thread_lock_factory(thread_id):
                await run_graph()
        else:
            await run_graph()

    # #73: confirm write ok + no new form → cards (+ short bubble); suppress long done.
    if aborted_for_human and final_text is not None:
        await chatwoot.send_bot_message(conversation_id, final_text)
        if messages_store is not None:
            messages_store.append(
                conversation_id, "assistant", final_text, message_id=final_message_id
            )
    elif (
        address_success is not None
        and address_write_ok
        and mutation_form_data is None
        and (status is None or status.get_mode(conversation_id) == "ai")
    ):
        followup = await send_mutation_success_card(
            chatwoot,
            conversation_id,
            order_id=address_success["order_id"],
            address=address_success["address"],
        )
        final_text = followup
        if followup is not None and messages_store is not None:
            messages_store.append(
                conversation_id, "assistant", followup, message_id=final_message_id
            )
    elif final_text is not None and (status is None or status.get_mode(conversation_id) == "ai"):
        await chatwoot.send_bot_message(conversation_id, final_text)
        if messages_store is not None:
            messages_store.append(conversation_id, "assistant", final_text, message_id=final_message_id)

    if mutation_form_data is not None:
        await send_mutation_form(chatwoot, conversation_id, mutation_form_data)

    if approval_form_data is not None:
        await send_approval_form(chatwoot, conversation_id, approval_form_data)

    return {
        "conversation_id": conversation_id,
        "chatwoot_conversation_id": cw_conv_id,
        "response": final_text,
    }


def update_address_success_card_items(*, order_id: str, address: str) -> list[dict[str, Any]]:
    """Chatwoot `cards` items for update_address success (#73 / plan §2④).

    Platform has no status badge: put「已完成」on the first description line.
    `actions` is required by Chatwoot card validation (informational ack).
    """
    return [
        {
            "title": update_address_success_card_title_text(),
            "description": update_address_success_card_description(order_id, address),
            "actions": [
                {
                    "type": "postback",
                    "text": "知道了",
                    "payload": "update_address_success_ack",
                }
            ],
        }
    ]


async def send_mutation_success_card(
    chatwoot: ChatwootClient,
    conversation_id: str,
    *,
    order_id: str,
    address: str,
    followup: bool = True,
) -> str | None:
    """Project update_address confirm success to Chatwoot `cards` (+ optional short text).

    Prefer cards over long Graph `done.final`; return the follow-up bubble text
    (or `None`) so the Adapter can store/return it instead of the long done.
    """
    items = update_address_success_card_items(order_id=order_id, address=address)
    await chatwoot.send_interactive_message(
        conversation_id,
        update_address_success_card_title_text(),
        "cards",
        {"items": items},
    )
    if not followup:
        return None
    bubble = update_address_success_followup_text()
    await chatwoot.send_bot_message(conversation_id, bubble)
    return bubble


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


async def send_clarify_form(
    chatwoot: ChatwootClient,
    conversation_id: str,
    missing_fields: list[str],
    profile: dict | None = None,
    orders: list[dict] | None = None,
) -> None:
    """Legacy clarify-form builder（#48 AgentBot 出站已停用；保留供兼容/单测）。"""
    items = []

    if "order_id" in missing_fields:
        options = [
            {"label": f"{item['order_id']} · {item.get('status_display', '')}", "value": item["order_id"]}
            for item in (orders or [])
        ]
        items.append({"name": "order_id", "label": "选择订单", "type": "select", "options": options})

    if "address" in missing_fields:
        old_address = (profile or {}).get("address", "")
        items.append({
            "name": "address",
            "label": f"新收货地址（当前：{old_address}）",
            "type": "text",
            "placeholder": "请输入新地址",
        })

    if "phone" in missing_fields:
        old_phone = (profile or {}).get("phone", "")
        items.append({
            "name": "phone",
            "label": f"新手机号（当前：{old_phone}）",
            "type": "text",
            "placeholder": "请输入新手机号",
        })

    if "address" in missing_fields:
        title = "修改收货地址"
    elif "phone" in missing_fields:
        title = "修改联系电话"
    elif missing_fields:
        title = "请补充：" + "、".join(FIELD_LABELS.get(field, field) for field in missing_fields)
    else:
        title = "请补充信息"

    await chatwoot.send_interactive_message(conversation_id, title, "form", {"items": items})


async def send_confirmation(
    chatwoot: ChatwootClient,
    conversation_id: str,
    summary: str,
) -> None:
    """Legacy confirmation input_select（#48 AgentBot 出站已停用；保留供兼容/单测）。"""
    await chatwoot.send_interactive_message(
        conversation_id,
        summary,
        "input_select",
        {"items": [
            {"title": "确认执行", "value": "confirmed"},
            {"title": "取消操作", "value": "canceled"},
        ]},
    )
