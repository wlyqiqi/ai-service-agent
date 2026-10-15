# Mutation Form Interrupt Spec

> Status: ready-for-agent  
> Glossary: `CONTEXT.md`  
> Seams: (1) Conversation Runtime `/chat` + resume E2E — protocol; (2) AgentBot Adapter — primary for `update_address` four-state + Implicit Cancel UX; (3) MutationGraph unit with Fake model + `Command(resume=...)` — optional

## Problem Statement

Customers who want medium-risk order changes (address, phone, note, invoice, service ticket) today bounce through multi-turn chat clarification and a separate confirmation interrupt. That feels like filing paperwork in conversation: fields get lost across turns, routing can drift on the follow-up message, and even low-stakes edits share a heavy confirmation ritual. Users need a clear form to fill once, one confirm click to submit, and honest errors on the same form—without the system writing from free text while a form is open.

## Solution

Keep intent clarification (“change address or invoice?”) **outside** MutationGraph. Once the tool is known, MutationGraph opens a **Mutation Form** via LangGraph official **Form Interrupt** (`interrupt` + `response_schema`) when there are eligible candidates. The client/Adapter renders fields and an Order Picker where the op requires one. Clicking confirm sends structured values through the harness as **Form Resume** (`Command(resume=...)`) on the same thread—never as a new Router turn. Validation/precheck failures re-issue interrupt on the same form session (`form_rejected` projection). Explicit Cancel is a form control that resumes with cancel and does not write. While a Form Interrupt is pending, plain text must not write (**Form-Only Write**); non-form inbound is handled by **Implicit Cancel** (Transport cancel-resume, then a normal new turn)—not tip+re-push of the old form.

For **`update_address`**, product UX is locked to four outbound states (AgentBot / Chatwoot projection; working plan `docs/plans/2026-10-15-mutation-form-ux-implicit-cancel.md`):

1. **Open form** — eligible paid-and-unshipped orders: intro text + form (current address read-only, order select with no preselect, new address, single submit = confirm, abandon footnote); no action confirm/cancel select.
2. **Empty state** — no eligible orders: plain-text explanation only; **no** empty dropdown form / no meaningless Form Interrupt.
3. **`form_rejected`** — same form session re-push with errors and draft kept; must **not** trigger Implicit Cancel.
4. **Success card** — after confirm write once: prefer Adapter `cards` (completed status, success title, order id, full new address, “账户收货地址已更新”) plus optional short bubble; Graph still ends with `done` (see `event-spec.md` §2.2).

## User Stories

1. As a customer, I want to say I need to change something on my order in chat, so that the assistant can ask what kind of change I mean before showing a form.
2. As a customer, I want the assistant to ask whether I mean address, invoice, or another mutation when my intent is vague, so that I am not forced to pick from a form menu of operation types.
3. As a customer, I want a form to appear only after the change type is clear, so that I only fill fields relevant to that operation.
4. As a customer, I want every mutation form that needs an order to include an order dropdown—even if I have only one eligible order—so that I always explicitly choose which order applies (no auto-preselect).
5. As a customer, I want the form to prefill anything already known from chat, so that I do not retype information I already gave.
6. As a customer, I want a single primary submit on the form (confirm), so that submitting the form is the only confirmation step.
7. As a customer, I want Confirm to apply my change (subject to business rules), so that I do not face a second “are you sure?” chat interrupt.
8. As a customer, I want an explicit Cancel on the form when the channel provides one, so that I can abandon the draft without writing and without relying on typing “算了”.
9. As a customer, I want field or business errors shown on the same open form (`form_rejected`), so that I can fix and resubmit without starting over and without Implicit Cancel clearing my session.
10. As a customer with a pending form who chats about something else, I want Implicit Cancel (silent abandon, no old-form re-push) so that my new message is handled as a normal turn.
11. As a customer, I want the system to refuse to write mutation data from plain chat while a form interrupt is pending, so that accidental text cannot bypass the form (Form-Only Write).
12. As a customer who successfully submits `update_address`, I want a success card stating the account shipping address was updated (plus order id and full new address), so that I am not misled into thinking only one order row was patched.
13. As a customer, I want failed business rules (e.g. address not editable) explained on the form, so that I know why Confirm did not write.
14. As a support operator looking at traces, I want form wait/resume to appear as LangGraph interrupts, so that Studio and LangSmith match runtime behavior.
15. As a product owner, I want refund/cancel to stay on HighRisk approval flows, so that Mutation Form is not used for high-risk money moves.
16. As a frontend client, I want `form_required` / `form_rejected` events derived from interrupt payloads and response schema, so that I can render without a second pending-state store.
17. As a frontend client / Adapter, I want Cancel and Confirm to map to Form Resume payloads, so that I do not invent a parallel session API for forms.
18. As a runtime maintainer, I want pending form interrupts to resume through the same resume path as other interrupts, so that FastAPI does not special-case “skip Router” for form JSON chat messages.
19. As a runtime maintainer, I want MutationGraph to remain closed (no internal jump to Read/HighRisk/Fallback), so that each new user intent re-enters via Router after the form flow ends.
20. As a runtime maintainer, I want tool execution through ToolNode with a single controlled tool call, so that the write path is not a ReAct loop.
21. As a customer with multiple editable orders, I want the Order Picker to list eligible candidates only, so that I cannot pick an order that precheck would reject for the wrong reason when avoidable.
22. As a customer who cancels the form (explicit or Implicit Cancel), I want the conversation to return to normal AI chat, so that I can ask something else without a stuck interrupt.
23. As a customer who completes a mutation, I want the next message to go through normal routing again, so that a new intent is not trapped in the old form.
24. As a developer, I want event-spec (and glossary) updated for Implicit Cancel / success cards, and graph-spec kept consistent when node copy drifts (no separate ADR required for this feature), so that protocol and graph contract stay the source of truth.
25. As a QA engineer, I want one primary E2E seam over chat+resume for protocol, and AgentBot as the primary seam for `update_address` four-state / Implicit Cancel UX, so that we validate behavior users actually hit.
26. As a customer with no eligible `update_address` orders, I want a plain-text empty state and no empty form, so that I know to look up orders or pay first.
27. As a customer changing address, I want the open form to show my current account shipping address read-only and a footnote that submit updates the account address and chatting about something else abandons the form, so that side effects and Implicit Cancel are clear.
28. As a runtime maintainer, I want Implicit Cancel implemented as official `Command(resume={"action":"cancel"})` then a normal new-turn invoke on the same `thread_id`, so that we follow framework HITL rules without a second pending-form store.

## Implementation Decisions

### Architecture

- Prefer LangGraph official HITL: Form Interrupt = `interrupt(value=..., response_schema=...)`; Form Resume = `Command(resume=...)` with checkpointer and `thread_id`.
- Do not maintain a parallel “Form Pending” session flag as source of truth; checkpoint interrupts are authoritative. SSE `form_required` / `form_rejected` are projections only.
- Operation Clarify (vague “modify order”) stays in the entry/intent layer before MutationGraph. MutationGraph assumes tool is already known (Hybrid Tool Resolution: high confidence map to tool; low confidence chat clarify first).
- Closed MutationGraph: no internal handoff to Read/HighRisk/Fallback for business routing. Handoff remains the existing unified handoff interrupt if ever needed.
- Mutation tools only (address/phone/note/invoice/service ticket). HighRisk cancel/refund unchanged.
- Confirm = clicking Confirm / primary submit emits Form Resume with field values; no second confirmation interrupt and no separate confirmed flag gate beyond “resume happened with submit payload”.
- Cancel = explicit form control → resume cancel sentinel → finalize without write.
- **Implicit Cancel** (glossary: `CONTEXT.md`): while a Form Interrupt is pending, inbound that is **not** a valid Mutation Form submit → Transport/Adapter first `Command(resume={"action":"cancel"})` on the same `thread_id` (no write), then a normal new-turn invoke with that user message; silent (no cancel tip); do not re-push the old form. Do **not** pass `Command(update=...)` as graph input to bypass the interrupt.
- Validation failure or precheck failure → re-interrupt with errors on the same draft (`form_rejected` projection); do not open a brand-new unrelated form session; **must not** trigger Implicit Cancel.
- While a form interrupt is pending, plain-text chat must not write (**Form-Only Write**). Prefer Implicit Cancel + re-route over tip+re-push as the main pending-text path. Entry still uses official resume then plain new-turn input—not a Mutation-only skip-Router hack for chat JSON.
- Transport (FastAPI/SSE/Chatwoot AgentBot) adapts interrupt/resume and may orchestrate Implicit Cancel; business gates stay inside the graph. Adapter owns Chatwoot `form` / `cards` / text projection.
- Execute writes via ToolNode (or equivalent official tool runner) with a single controlled tool call constructed from resumed args + system `request_id`.
- Node shape (logical): ensure/collect form (interrupt loop with validate+precheck inside or immediately after resume) → execute → tools → single finalize. Remove chat clarify and confirmation_pause from Mutation main path.
- interrupt-before code must be idempotent because nodes re-run from the start on resume.
- Keep graph-spec / event-spec aligned with Form Interrupt protocol; deprecate Mutation main-path use of `clarification_required` / `confirmation_required` for this flow; `form_required` / `form_rejected` are the projections (cancel completion via existing done/error as appropriate). Success may project Adapter cards while Graph still emits `done`. No ADR file required (project choice). `update_address` four-state + Implicit Cancel glossary/`event-spec` patches land with #70; graph-spec node copy may follow in implementation tickets if still stale.
- Order Picker shown when the op has order candidates; candidates carried in interrupt value; never auto-select sole order. Empty eligible candidates for `update_address` → empty-state text, **no** empty form.
- `update_address` open-form interrupt value must carry `current_address` (account profile; empty → display「暂无」) for read-only projection; resume write uses the new-address field only. Domain still updates account `customers.address` (not per-order address storage).
- LangGraph version must support `response_schema` on interrupt (≥ 1.2.12 per upstream docs); verify against installed langgraph before implementation.

### Modules / interfaces (conceptual)

- MutationGraph workflow rebuild around Form Interrupt / Form Resume / ToolNode.
- Conversation Runtime / AgentBot entry: if thread has pending Form Interrupt, handle Form Resume (confirm/cancel) or Implicit Cancel (cancel resume then new turn); do not tip+re-push as the main pending-text path; do not re-Router a Form Resume into a competing path.
- Event protocol: form_required / form_rejected shapes for clients; Implicit Cancel then new-turn events per `event-spec.md` §2.2.
- Select/intent layer: only ensure tool identity before entering Mutation; stop owning multi-turn field clarification for mutation main path.
- Frontend / Chatwoot form rendering: drive from interrupt value + response_schema; Confirm/Cancel → resume API already used for approvals/confirmations where possible; Adapter owns four-state projection for `update_address`.
- Order repository remains the fact source for candidates and precheck; no new dual write model.

### Runtime decoupling constraints (must hold)

- Single source of truth for waiting-on-form: graph interrupt.
- ResumeHandler remains generic over interrupt types.
- Form field schema derived from tool/graph, not duplicated as a second registry in the server.
- Subgraph does not absorb Operation Clarify.

## Testing Decisions

- Good tests assert external behavior: events / Chatwoot outbound seen by the client, whether a write occurred, execution_status, and that Router did not re-classify a Form Resume. Do not assert internal private helper names or exact node count.
- **Protocol seam:** Conversation Runtime chat + resume E2E (same class of tests as existing mutation confirm / approval resume flows): form interrupt emitted → Confirm resume writes once → precheck failure re-interrupts with errors → Cancel does not write → plain text during interrupt does not write (and, when Implicit Cancel is wired on `/chat`, cancel resume then new turn rather than tip+re-push).
- **`update_address` UX seam (sole primary for four-state + Implicit Cancel):** AgentBot Chatwoot Adapter — assert outbound message types/payloads (`text` / `form` items / `cards`) and invoke/resume call order for Implicit Cancel. Required scenarios: open-form layout contract; empty candidates → text only; success → cards with account-updated copy; `form_rejected` re-push with draft (no Implicit Cancel); pending non-form message → cancel resume then new turn, silent, no old form; confirm still writes once. Prior art: `tests/test_agent_bot.py`.
- **Optional seam:** MutationGraph unit tests with Fake model and direct `Command(resume=...)` for state transitions without HTTP.
- Prior art: existing mutation graph tests, approval resume paths in server/streaming tests, interrupt three-state coverage patterns, real_smoke mutation lanes / functional scenarios / real_model eval (T8: expect `form_required` / Form Resume, not Mutation `confirmation_required` or chat missing-field clarify).
- Prefer offline Fake model + stub tools / AgentBot fakes; live SiliconFlow or Widget smoke optional after protocol lands.
- Regression: HighRisk approval path and Read path must not regress; Mutation must not still require chat clarification for happy-path address update when tool is known.

## Out of Scope

- Redesigning HighRisk approval (cancel/refund) onto Mutation Form.
- Replacing Operation Clarify with a form-based tool picker.
- Text-only clients that can complete writes without forms (Form-Only Write).
- Auto-selecting the only eligible order.
- Per-order independent address storage (account `customers.address` remains the write target; copy disambiguates).
- Structured province/city/district widgets; Chatwoot native dual Cancel button inside `form`.
- Showing non-editable (shipped/delivered) orders in the `update_address` picker.
- Making `/chat` HTTP a second primary test seam for the `update_address` four-state UX delivery (AgentBot is sole primary UX seam; `/chat` may share Implicit Cancel later).
- Changing LangGraph runtime protocols (interrupt / resume / ToolNode).
- PR #8 pending-clarification-context chat draft merge as the main mutation path (superseded for mutation main path by Form Interrupt; may remain irrelevant or follow-up cleanup).
- Changing ReadGraph / Fallback architecture.
- Writing an ADR (explicitly declined).

## Further Notes

- Glossary terms live in `CONTEXT.md` (Mutation Form, Form Interrupt, Form Resume, Order Picker, Operation Clarify, Form-Only Write, Form Cancel, Implicit Cancel, Closed MutationGraph, ToolNode, AgentBot Adapter).
- Official LangGraph guidance favors interrupt + Command(resume) + optional response_schema for typed forms; this spec adopts that over “normal-turn form pending + chat JSON skip Router”. Implicit Cancel is still official resume-then-new-turn, not a bypass.
- Installed project langgraph pin should be checked for `response_schema` support; if below 1.2.12, bump is part of enabling work.
- Seams: protocol `/chat`+resume E2E; `update_address` UX = AgentBot; optional graph unit resume tests. Locked open/empty/success/footnote copy for that feature is in parent [#69](https://github.com/wlyqiqi/ai-service-agent/issues/69) and working plan `docs/plans/2026-10-15-mutation-form-ux-implicit-cancel.md` (plan is working notes; glossary + this spec + `event-spec.md` remain authoritative for agents).
