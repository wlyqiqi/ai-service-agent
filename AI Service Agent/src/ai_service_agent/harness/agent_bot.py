"""Chatwoot AgentBot webhook handler（Transport/harness 层）。"""

import logging
import uuid
from typing import Any

from langchain_core.messages import HumanMessage

from ai_service_agent.graph.runtime_context import GraphContext
from ai_service_agent.graph.workflows.form_resume import FORM_RESUME_SKIP_FIELDS
from ai_service_agent.harness.chatwoot import ChatwootClient, chatwoot_message_id
from ai_service_agent.graph.workflows.copy import (
    FIELD_LABELS,
    handoff_cancel_confirm_text,
    handoff_request_private_note_text,
    update_address_current_address_display,
    update_address_empty_state_text,
    update_address_form_footnote_text,
    update_address_form_intro_text,
    update_address_form_subtitle_text,
    update_address_form_title_text,
    update_address_new_address_placeholder,
)
from ai_service_agent.harness.approval_side_channel import (
    parse_approval_form_submission,
    send_approval_form,
)
from ai_service_agent.harness.form_events import pending_form_value
from ai_service_agent.harness.form_gate import (
    FormDecisionKind,
    FormInboundKind,
    decide_form_inbound,
    normalize_submitted_values,
)
from ai_service_agent.harness.handoff import handoff_request_tip
from ai_service_agent.harness.handoff_entry import EntryDecision, decide_entry, is_human_lane
from ai_service_agent.harness.mutation_projection import (
    WriteSuccessCapture,
    send_mutation_success_card,
)
from ai_service_agent.harness.outbound_gate import TurnOrigin, decide_delivery
from ai_service_agent.harness.resume import approval_dispatch, resolve_resume_payload
from ai_service_agent.harness.streaming import event_stream

logger = logging.getLogger("ai_service_agent")


async def _clear_agent_typing(chatwoot, conversation_id: str) -> None:
    """Drop the Agent Bot typing bubble. Seat typing stays on Chatwoot's own event."""
    clear = getattr(chatwoot, "clear_typing", None)
    if clear is None:
        return
    try:
        await clear(conversation_id)
    except Exception:
        logger.exception("chatwoot clear typing failed: conv=%s", conversation_id)


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

# Seat Approver Approval Side-Channel projection/parsing lives in
# harness/approval_side_channel.py (ADR-0002); the AgentBot only keeps the
# "customer Widget submit must never become a seat decision" guard.


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


def _order_picker_options(candidates: Any) -> list[dict[str, str]]:
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
    options = _order_picker_options(data.get("order_candidates"))
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

    options = _order_picker_options(data.get("order_candidates"))
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
    submitted_values = normalize_submitted_values(raw_submitted)
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

    mode = status.get_mode(conversation_id) if status is not None else "ai"
    if is_human_lane(mode):
        # Human lane: Message Store only. No bot bubble, no Graph.
        if messages_store is not None:
            message_id = chatwoot_message_id(payload)
            if message_id and messages_store.get_by_message_id(message_id):
                return None
            messages_store.append(conversation_id, "user", content, message_id=message_id or None)
        await _clear_agent_typing(chatwoot, conversation_id)
        return {
            "conversation_id": conversation_id,
            "chatwoot_conversation_id": cw_conv_id,
            "response": None,
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

    # Seat-gated entry (handoff-spec §2): 与 /chat 共用 harness/handoff_entry.py 的
    # 同一策略；表单提交不是文本入站，不参与入口判定（form_gate 的职责）。
    if handoff is not None and submitted_values is None:
        decision = decide_entry(
            mode=mode,
            awaiting=handoff.get_awaiting(conversation_id) is not None,
            text=content,
        )
        if decision is EntryDecision.CANCEL:
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
        if decision is EntryDecision.REQUEST:
            ev = handoff.request_handoff(conversation_id, "用户显式请求转人工", content[:80])
            # created 是唯一判据（handoff-spec §2）：挂起期间重复申请只发短 tip。
            created = bool(ev.get("created", True))
            tip = handoff_request_tip(created=created)
            if on_handoff_requested is not None:
                await on_handoff_requested(ev)
            else:
                if messages_store is not None:
                    messages_store.append(conversation_id, "assistant", tip)
                await chatwoot.send_bot_message(conversation_id, tip)
                if created:
                    try:
                        await chatwoot.prompt_handoff_request(
                            conversation_id,
                            handoff_request_private_note_text((ev.get("data") or {}).get("summary") or ""),
                        )
                    except Exception:
                        pass
            return {
                "conversation_id": conversation_id,
                "chatwoot_conversation_id": cw_conv_id,
                "response": tip,
            }

    mutation_form_data = None
    approval_form_data = None
    final_message_id = None
    write_success = WriteSuccessCapture()
    aborted_for_human = False

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
                elif pending_form_value(state.interrupts) is not None:
                    # Single decision point (harness/form_gate.py). Executing the
                    # decision — resume + two-phase Implicit Cancel — stays here.
                    decision = decide_form_inbound(
                        interrupt_value,
                        kind=(
                            FormInboundKind.FORM_SUBMIT
                            if submitted_values is not None
                            else FormInboundKind.TEXT
                        ),
                        submitted_values=submitted_values,
                    )
                    if decision.kind is FormDecisionKind.IMPLICIT_CANCEL:
                        # #72: pending form + non-form inbound -> silent cancel
                        # resume, then the same message as a new turn.
                        implicit_cancel = True
                    else:
                        resume_payload = decision.resume_payload
                else:
                    # #48: legacy confirmation_required interactive retired; no special resume.
                    resume_payload = resolve_resume_payload(interrupt_value, content)

        async def _consume_graph_events(*, messages, resume):
            nonlocal final_text, mutation_form_data, approval_form_data, final_message_id
            async for event in event_stream(
                graph,
                config,
                messages,
                on_interrupt=on_interrupt,
                resume_payload=resume,
                extra_state={"user_id": user_id},
                context=context,
            ):
                write_success.observe(event)
                if event.get("type") == "done":
                    final_text = event.get("data", {}).get("final")
                    final_message_id = event.get("data", {}).get("message_id")
                elif event.get("type") in ("form_required", "form_rejected"):
                    # form_rejected: same Form Interrupt session with errors
                    # (Closed MutationGraph); Adapter only projects Transport.
                    mutation_form_data = event.get("data") or {}
                elif event.get("type") == "approval_required":
                    # Seat Approver side-channel card (#64); private form, not Widget.
                    approval_form_data = event.get("data") or {}
                elif event.get("type") == "handoff_requested" and on_handoff_requested is not None:
                    await on_handoff_requested(event)
                # #48: clarification_required / confirmation_required interactive retired.
                # Operation Clarify and zero-candidate HighRisk missing-fields stay chat (done.final).
                # HighRisk with candidates opens the same Form Interrupt projection as Mutation.

        async def run_graph():
            nonlocal final_text, mutation_form_data, approval_form_data, final_message_id
            nonlocal aborted_for_human
            if status is not None and status.get_mode(conversation_id) != "ai":
                # Takeover cancelled the form under this lock: do not Form Resume
                # and do not send a bot bubble.
                aborted_for_human = True
                final_text = None
                return
            if (
                isinstance(resume_payload, dict)
                and resume_payload.get("action") == "confirm"
                and getattr(graph, "checkpointer", None)
            ):
                fresh = await graph.aget_state(config)
                if pending_form_value(fresh.interrupts) is None:
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

    if aborted_for_human:
        await _clear_agent_typing(chatwoot, conversation_id)
        return {
            "conversation_id": conversation_id,
            "chatwoot_conversation_id": cw_conv_id,
            "response": None,
        }

    # Confirm write ok + no new form/approval card → shared success cards (+ short bubble).
    # Failures stay on the plain done bubble (no failure cards).
    success_spec = None
    if mutation_form_data is None and approval_form_data is None:
        success_spec = write_success.spec()
    delivery = decide_delivery(
        mode=status.get_mode(conversation_id) if status is not None else "ai",
        origin=TurnOrigin.AI_TURN,
        success_spec=success_spec,
        final=final_text,
    )
    if delivery.card and delivery.outbound:
        followup = await send_mutation_success_card(
            chatwoot,
            conversation_id,
            success_spec,
        )
        final_text = followup
        if followup is not None and messages_store is not None:
            messages_store.append(
                conversation_id, "assistant", followup, message_id=final_message_id
            )
    elif delivery.persist is not None and delivery.outbound:
        await chatwoot.send_bot_message(conversation_id, delivery.persist)
        if messages_store is not None:
            messages_store.append(
                conversation_id, "assistant", delivery.persist, message_id=final_message_id
            )

    if mutation_form_data is not None:
        await send_mutation_form(chatwoot, conversation_id, mutation_form_data)

    if approval_form_data is not None:
        # 投影入口单点：生产接线走注入回调（→ ApprovalSideChannel.project），
        # 未注入时退回模块内的薄包装（同一实现，见 approval_side_channel）。
        if on_approval_pending is not None:
            await on_approval_pending(approval_form_data)
        else:
            await send_approval_form(chatwoot, conversation_id, approval_form_data)

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
        options = _order_picker_options(orders)
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
