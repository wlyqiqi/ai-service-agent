"""Chatwoot AgentBot webhook handler（Transport/harness 层）。"""

import logging
import uuid
from typing import Any

from langchain_core.messages import HumanMessage

from ai_service_agent.graph.runtime_context import GraphContext
from ai_service_agent.harness.chatwoot import ChatwootClient, chatwoot_message_id
from ai_service_agent.graph.workflows.copy import (
    FIELD_LABELS,
    build_mutation_failure_card,
    handoff_cancel_confirm_text,
    handoff_request_private_note_text,
)
from ai_service_agent.harness.approval_side_channel import (
    parse_approval_form_submission,
    send_approval_form,
)
from ai_service_agent.harness.form_events import is_form_interrupt
from ai_service_agent.harness.form_gate import (
    FormDecisionKind,
    FormInboundKind,
    decide_form_inbound,
    normalize_submitted_values,
)
from ai_service_agent.harness.complaint_offer import (
    clear_complaint_offer_flags,
    complaint_handoff_accept,
    read_complaint_offer,
)
from ai_service_agent.harness.handoff import handoff_request_tip
from ai_service_agent.harness.handoff_entry import (
    EntryDecision,
    decide_entry,
    entry_handoff_reason,
    is_human_lane,
)
from ai_service_agent.harness.mutation_form_projection import (
    form_rejection_reason,
    order_picker_options,
    send_mutation_form,
)
from ai_service_agent.harness.mutation_projection import (
    WriteFailureCapture,
    WriteSuccessCapture,
    send_mutation_failure_card,
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


# Mutation Form Chatwoot projection (items / title / address fields / send)
# lives in harness/mutation_form_projection.py. AgentBot keeps Transport
# adaptation and inbound only (decide_form_inbound / form_gate / graph resume).
#
# Seat Approver Approval Side-Channel projection/parsing lives in
# harness/approval_side_channel.py (ADR-0002); the AgentBot only keeps the
# "customer Widget submit must never become a seat decision" guard.


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
    thread_config = {"configurable": {"thread_id": thread_id}}
    offer_pending, offer_summary = False, ""
    if (
        handoff is not None
        and submitted_values is None
        and graph is not None
        and not is_human_lane(mode)
    ):
        offer_pending, offer_summary = await read_complaint_offer(graph, thread_config)
    if handoff is not None and submitted_values is None:
        decision = decide_entry(
            mode=mode,
            awaiting=handoff.get_awaiting(conversation_id) is not None,
            text=content,
            complaint_offer_pending=offer_pending,
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
            reason = entry_handoff_reason(
                text=content, complaint_offer_pending=offer_pending
            )
            ev = handoff.request_handoff(
                conversation_id, reason, (offer_summary or content)[:80]
            )
            if offer_pending and graph is not None:
                await clear_complaint_offer_flags(graph, thread_config)
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
    write_failure = WriteFailureCapture()
    form_rejected = False
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
                elif is_form_interrupt(interrupt_value):
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
            nonlocal form_rejected
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
                write_failure.observe(event)
                if event.get("type") == "done":
                    final_text = event.get("data", {}).get("final")
                    final_message_id = event.get("data", {}).get("message_id")
                elif event.get("type") in ("form_required", "form_rejected"):
                    # form_rejected: same Form Interrupt session with errors
                    # (Closed MutationGraph); Adapter only projects Transport.
                    mutation_form_data = event.get("data") or {}
                    form_rejected = event.get("type") == "form_rejected"
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
            nonlocal aborted_for_human, form_rejected
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

        if handoff is not None and getattr(graph, "checkpointer", None):
            accepted = complaint_handoff_accept(
                (await graph.aget_state(thread_config)).values
            )
            if accepted is not None:
                ev = handoff.request_handoff(
                    conversation_id, "complaint", accepted or content[:80]
                )
                await clear_complaint_offer_flags(graph, thread_config)
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
                                handoff_request_private_note_text(
                                    (ev.get("data") or {}).get("summary") or ""
                                ),
                            )
                        except Exception:
                            pass
                return {
                    "conversation_id": conversation_id,
                    "chatwoot_conversation_id": cw_conv_id,
                    "response": tip,
                }

    if aborted_for_human:
        await _clear_agent_typing(chatwoot, conversation_id)
        return {
            "conversation_id": conversation_id,
            "chatwoot_conversation_id": cw_conv_id,
            "response": None,
        }

    # 卡片优先于长 done.final：成功 → 成功卡 + 短 followup；
    # form_rejected → 失败卡 + 重推 form；写库失败 → 失败卡 + 短引导。
    success_spec = None
    failure_spec = None
    failure_card = None
    if mutation_form_data is None and approval_form_data is None:
        if write_failure.tool is not None:
            failure_card = write_failure.card()
            if failure_card is not None:
                failure_spec = {"followup": write_failure.followup()}
        else:
            success_spec = write_success.spec()
    elif form_rejected and mutation_form_data is not None:
        failure_card = build_mutation_failure_card(
            str(mutation_form_data.get("tool") or ""),
            form_rejection_reason(mutation_form_data),
            kind="form_rejected",
            facts=mutation_form_data.get("prefills"),
        )
        if failure_card is not None:
            # 失败卡后直接重推 form，不再加气泡。
            failure_spec = {"followup": None}
    delivery = decide_delivery(
        mode=status.get_mode(conversation_id) if status is not None else "ai",
        origin=TurnOrigin.AI_TURN,
        success_spec=success_spec,
        failure_spec=failure_spec,
        final=final_text,
    )
    if delivery.card and delivery.outbound:
        if delivery.card_kind == "failure":
            await send_mutation_failure_card(chatwoot, conversation_id, failure_card)
            final_text = delivery.persist
            if final_text is not None:
                # 失败卡后可跟一句短引导（tool_failed）；form_rejected 没有。
                await chatwoot.send_bot_message(conversation_id, final_text)
        else:
            final_text = await send_mutation_success_card(
                chatwoot,
                conversation_id,
                success_spec,
            )
        if final_text is not None and messages_store is not None:
            messages_store.append(
                conversation_id, "assistant", final_text, message_id=final_message_id
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
        options = order_picker_options(orders)
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
