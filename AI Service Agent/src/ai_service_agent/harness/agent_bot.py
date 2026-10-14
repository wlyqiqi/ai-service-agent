"""Chatwoot AgentBot webhook handler（Transport/harness 层）。"""

import uuid
from typing import Any

from langchain_core.messages import HumanMessage

from ai_service_agent.graph.runtime_context import GraphContext
from ai_service_agent.harness.chatwoot import ChatwootClient, chatwoot_message_id
from ai_service_agent.graph.workflows.copy import (
    FIELD_LABELS,
    form_plain_text_refuse_text,
    human_mode_tip_text,
)
from ai_service_agent.harness.form_events import is_form_interrupt, project_form_interrupt
from ai_service_agent.harness.resume import approval_dispatch, resolve_resume_payload
from ai_service_agent.harness.streaming import event_stream

# Form Resume schema keys that are not Mutation Form input widgets.
# ``action`` is appended explicitly as Confirm/Cancel control (not from schema loop).
_FORM_SCHEMA_SKIP_FIELDS = frozenset({"action", "request_id"})

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


def mutation_form_title_from_form_data(data: dict[str, Any]) -> str:
    """Chatwoot form ``content``: summary plus form-level / leftover error copy."""
    title = data.get("summary") or "请填写表单"
    errors = _form_errors_map(data)
    if not errors:
        return title
    # Field errors are also painted onto matching items; ``_form`` and any
    # unmatched keys still need a visible place in the message copy.
    item_names = {"order_id", "action"}
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


def mutation_form_items_from_form_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build Chatwoot ``form`` items from a ``form_required`` / ``form_rejected`` payload.

    Source of truth matches the former SSE Mutation Form: Order Picker from
    ``order_candidates``, remaining fields from ``fields`` or ``response_schema``,
    prefills applied as item ``default`` values. Always ends with Confirm/Cancel
    ``action`` select so Form Cancel resume is possible from Widget.
    ``form_rejected`` ``errors`` are projected into item label/placeholder and
    title copy within what Chatwoot native forms allow.
    """
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


def bind_graph_context_user_id(context, user_id: str):
    """Put the Contact identifier (session user_id) on GraphContext.

    ``extra_state["user_id"]`` only fills graph state. Tools and nodes that read
    runtime context keep seeing ``""`` unless this channel is set too.
    Non-GraphContext objects are forwarded unchanged (test doubles).
    """
    identity = (user_id or "").strip()
    if not identity:
        return context
    if isinstance(context, GraphContext):
        if context.user_id == identity:
            return context
        return context.model_copy(update={"user_id": identity})
    if context is None:
        return GraphContext(user_id=identity)
    return context


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

    submitted_values = _normalize_submitted_values(
        (payload.get("content_attributes") or {}).get("submitted_values")
    )
    if event == "message_updated" and submitted_values is None:
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
        on_interrupt, on_handoff_created, on_approval_pending = callbacks_factory(conversation_id, thread_id)

    if messages_store is not None:
        message_id = chatwoot_message_id(payload)
        if message_id and messages_store.get_by_message_id(message_id):
            return None
        messages_store.append(conversation_id, "user", content, message_id=message_id or None)
        history = messages_store.hydrate(conversation_id)
    else:
        history = [HumanMessage(content=content)]

    mutation_form_data = None
    final_message_id = None

    if graph is None:
        final_text = (
            f"[AgentBot] 收到表单：{content}"
            if submitted_values is not None
            else f"[AgentBot] Received: {content}"
        )
    else:
        # Contact identifier is the session user_id (create_session(user_id) above).
        context = bind_graph_context_user_id(context, user_id)
        config = {"configurable": {"thread_id": thread_id}}
        final_text = None
        resume_payload = None
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
                    if action in ("confirmed", "confirm"):
                        resume_payload = {"action": "confirm"}
                        for key, value in submitted.items():
                            if key in ("action", "confirmation", "request_id"):
                                continue
                            resume_payload[key] = value
                    elif action in ("canceled", "cancel"):
                        resume_payload = {"action": "cancel"}
                    else:
                        # 纯文本 / 并发写意图不是 Form Resume：单挂 tip + 重推当前表单，不跑图。
                        final_text = form_plain_text_refuse_text()
                        projected = project_form_interrupt(state.interrupts[0])
                        if projected:
                            mutation_form_data = projected.get("data") or {}
                else:
                    # #48: legacy confirmation_required interactive retired; no special resume.
                    resume_payload = resolve_resume_payload(interrupt_value, content)

        if final_text is None:
            async def run_graph():
                nonlocal final_text, mutation_form_data
                async for event in event_stream(
                    graph,
                    config,
                    history,
                    on_interrupt=on_interrupt,
                    resume_payload=resume_payload,
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
                    elif event.get("type") == "handoff_created" and on_handoff_created is not None:
                        await on_handoff_created(event)
                    # #48: clarification_required / confirmation_required interactive retired.
                    # Operation Clarify and HighRisk missing-fields stay outbound chat (done.final).

            if thread_lock_factory is not None:
                async with await thread_lock_factory(thread_id):
                    await run_graph()
            else:
                await run_graph()

    if final_text is not None and (status is None or status.get_mode(conversation_id) == "ai"):
        await chatwoot.send_bot_message(conversation_id, final_text)
        if messages_store is not None:
            messages_store.append(conversation_id, "assistant", final_text, message_id=final_message_id)

    if mutation_form_data is not None:
        await send_mutation_form(chatwoot, conversation_id, mutation_form_data)

    return {
        "conversation_id": conversation_id,
        "chatwoot_conversation_id": cw_conv_id,
        "response": final_text,
    }


async def send_mutation_form(
    chatwoot: ChatwootClient,
    conversation_id: str,
    form_data: dict[str, Any],
) -> None:
    """Project Form Interrupt ``form_required`` / ``form_rejected`` to Chatwoot ``form``."""
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
