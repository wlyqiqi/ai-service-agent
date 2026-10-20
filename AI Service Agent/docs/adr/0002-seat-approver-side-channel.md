# Seat Approver owns HighRisk approval (side-channel)

HighRisk approve/reject is performed by a Chatwoot **Seat Approver** via an Approval Side-Channel (seat form/card → Runtime), not by the customer Requester and not by flipping `conversation_mode` to `human`. Customer JWT may read own approval status (`GET /approvals`, `GET /approvals/{id}`) but never approve/reject. Human approve/reject is fail-closed when customer auth is off; only `simulate_approval` may auto-approve.

`decided_by` for seat decisions is stored as `seat:{Chatwoot sender.id}` (reserved words unchanged: `system`, `simulate`). Grill Q12 initially framed `seat:chatwoot-agent-{sender.id}`; we **intentionally simplified** so the `seat:` prefix alone is the source namespace—do **not** embed a redundant `chatwoot-agent-` segment. v1 Approval SoD is **channel-level** only (customer identity can never approve); there is no seat↔customer account link table yet.

**Seat HTTP badge (to-spec C):** Human approve/reject over HTTP is allowed only with an **internal service credential plus a seat actor id** (not a customer JWT, not a forgeable seat-id header alone). Tests and a future Chatwoot adapter share this entry; Chatwoot form UX remains a later ticket.

This **supersedes** `docs/decisions/2026-09-14-handoff-decisions.md` §2 and the matching MVP “pending 审批归宿 / 坐席不代为审批” lines in `handoff-spec.md` §1.3 and §5.1. Pending approvals remain actionable on the side-channel even while `conversation_mode=human`. On approve/reject, Runtime **immediately** resumes the graph and writes Message Store; **outbound** Chatwoot/Widget sends apply the same mode gate as other AI sends (`handoff-spec` §1.4)—only when `conversation_mode==ai` at send time. The resolve-time check that a conversation has pending approvals (§1.3 first bullet) is **not** superseded—only the destiny / “坐席不代为审批” policy is.

## Considered Options

- Customer JWT as approver (current code) — rejected: self-approval, no real actor in `decided_by`
- Approver role on demo UserStore / allowlist — rejected for v1 product shape; seat is Chatwoot
- Approval implies handoff (`conversation_mode=human`) — rejected: conflates session takeover with HighRisk gate
- Person-level SoD via seat↔customer binding — deferred; see residual risk
- `decided_by=seat:chatwoot-agent-{id}` — rejected as redundant; `seat:` prefix is sufficient source identity

## Consequences

- **Residual risk (accepted for v1):** a human who is both a Chatwoot agent and a customer cannot be blocked from approving “their own” Requester orders at the person level; audit relies on `decided_by=seat:{id}` plus Requester id for later binding.
- **Migration:** existing rows with `decided_by="approver"` (or other non-`seat:` values) are pre-namespace legacy; readers must not treat them as Seat Approver ids. New writes use `seat:` only (plus reserved words).
- **Spec sync:** `handoff-spec.md` §1.3 / §5.1 归宿段与 `handoff-decisions` §2 已加 supersede 注。仍待正文落地：`approval-spec.md` (authz + `decided_by` + side-channel transport)。交叉引用已 scrub：`architecture.md` §11、`graph-overview.md`、`interrupt-resume-sequence.md`、`customer_service_final_architecture.svg`、`state-machines.html`、`archify-lifecycle-mode.*`；历史留痕仅加指针：`handoff-understanding-check.md`、`2026-09-18-handoff-intent-gate.md` D6。Edit pattern for historical prose: `Superseded by ADR-0002` + keep text; do not delete the pending-approval **check** bullet in §1.3.

## Implementation checklist (harness)

Treat as one authz slice plus resume seams—not only `_ensure_approval_owner`:

1. **鉴权反转 + fail-closed + GET 归属过滤** — customer JWT never approve/reject; when customer auth is off, human approve/reject → 403 (only `simulate_approval`); `GET /approvals` and `GET /approvals/{id}` require auth when enabled and filter to the caller’s own Requester rows (no unauthenticated full-store dump).
2. **`decided_by=seat:{sender.id}`** — stop hardcoding `"approver"`; reserved `system` / `simulate` unchanged.
3. **Approval resume 出站 mode 门控** — before Chatwoot/Widget send after `_approval_resume`, re-read `conversation_mode==ai` (`handoff-spec` §1.4).
4. **Approval resume `on_interrupt` / callbacks** — `_approval_resume` must not bare-`ainvoke` without the handoff/approval interrupt callback path; a follow-on `approval_required` / `handoff_request` must not be silent.

### 落地位置（2026-10-20 收敛后）

侧信道执行链已归位到 `src/ai_service_agent/harness/approval_side_channel.py`（`ApprovalSideChannel`）；`server.create_app` 不再持有审批决策闭包。

| # | 落点 | 说明 |
|---|---|---|
| 1 | `ApprovalSideChannel.authenticate` + `server._seat_actor` / `_seat_http` | 工牌 fail-closed（无 secret / 口令错 / 缺 actor → 403）；客户 GET 归属过滤仍留 Transport（客户身份鉴权，非侧信道） |
| 2 | `SeatActor.decided_by` | 唯一实现 `seat:{actor_id}`，不再散落 f-string |
| 3 | `harness/outbound_gate.decide_delivery` + `ApprovalSideChannel._deliver` | 单点门控；`TurnOrigin.SEAT_DECISION` = 落库不受 mode 影响、仅出站受门控 |
| 4 | `ApprovalSideChannel._run`（`_interrupt_callbacks` 注入） | follow-on `approval_required` 投影坐席表单、`handoff_request` 走注入回调，均不静默 |

`ApprovalService` 保持浅存储（`approval-spec §3.1`）：`make_interrupt_callback` 已迁出为 `make_approval_interrupt_callback`。

