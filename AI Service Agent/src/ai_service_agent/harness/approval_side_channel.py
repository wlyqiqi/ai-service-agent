"""Approval Side-Channel（ADR-0002）：坐席侧投影与入站解析。

坐席侧产物住在客户侧适配器（``agent_bot``）里会让 ``server`` 反向依赖它，
本模块先把**投影**与**解析**归位；决策 / 工牌鉴权 / resume 编排仍在
``server.create_app`` 闭包内，由后续 PR 迁入。

分层：本模块只做「``approval_required`` 载荷 → Chatwoot 私有表单」与
「坐席提交 → ``{approval_id, action, note?}``」，不含任何业务决策
（批准后执行什么由 ``HighRiskGraph`` 决定，``approval-spec §3.1``）。
"""

from typing import Any

from ai_service_agent.harness.chatwoot import ChatwootClient
from ai_service_agent.harness.form_gate import normalize_submitted_values

# Seat Approver Approval Side-Channel form fields (ADR-0002 / #64).
# Customer Widget must never treat these as Mutation Form Resume.
_APPROVAL_FORM_ID_FIELD = "approval_id"
_APPROVAL_FORM_DECISION_FIELD = "decision"
_APPROVAL_FORM_NOTE_FIELD = "note"


def _approval_form_decision_item() -> dict[str, Any]:
    """Fresh item per call: a shallow ``dict(...)`` copy would share ``options``."""
    return {
        "name": _APPROVAL_FORM_DECISION_FIELD,
        "label": "审批决定",
        "type": "select",
        "options": [
            {"label": "批准", "value": "approve"},
            {"label": "拒绝", "value": "reject"},
        ],
    }


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
        _approval_form_decision_item(),
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
    values = normalize_submitted_values(submitted) if not isinstance(submitted, dict) else submitted
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
