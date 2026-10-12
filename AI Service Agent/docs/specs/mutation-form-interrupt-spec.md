# Mutation Form Interrupt Spec

> Status: ready-for-agent  
> Glossary: `CONTEXT.md`  
> Seams: (1) Conversation Runtime `/chat` + resume E2E — primary; (2) MutationGraph unit with Fake model + `Command(resume=...)` — optional

## Problem Statement

Customers who want medium-risk order changes (address, phone, note, invoice, service ticket) today bounce through multi-turn chat clarification and a separate confirmation interrupt. That feels like filing paperwork in conversation: fields get lost across turns, routing can drift on the follow-up message, and even low-stakes edits share a heavy confirmation ritual. Users need a clear form to fill once, one confirm click to submit, and honest errors on the same form—without the system writing from free text while a form is open.

## Solution

Keep intent clarification (“change address or invoice?”) **outside** MutationGraph. Once the tool is known, MutationGraph opens a **Mutation Form** via LangGraph official **Form Interrupt** (`interrupt` + `response_schema`). The client renders fields and an always-present Order Picker. Clicking confirm sends structured values through the harness as **Form Resume** (`Command(resume=...)`) on the same thread—never as a new Router turn. Validation/precheck failures re-issue interrupt on the same form session (`form_rejected` projection). Cancel is an explicit form control that resumes with cancel and does not write. Free text while interrupted gets a friendly refusal and does not mutate data.

## User Stories

1. As a customer, I want to say I need to change something on my order in chat, so that the assistant can ask what kind of change I mean before showing a form.
2. As a customer, I want the assistant to ask whether I mean address, invoice, or another mutation when my intent is vague, so that I am not forced to pick from a form menu of operation types.
3. As a customer, I want a form to appear only after the change type is clear, so that I only fill fields relevant to that operation.
4. As a customer, I want every mutation form to include an order dropdown—even if I have only one eligible order—so that I always explicitly choose which order is affected.
5. As a customer, I want the form to prefill anything already known from chat, so that I do not retype information I already gave.
6. As a customer, I want a single Confirm button on the form, so that submitting the form is the only confirmation step.
7. As a customer, I want Confirm to apply my change (subject to business rules), so that I do not face a second “are you sure?” chat interrupt.
8. As a customer, I want an explicit Cancel on the form, so that I can abandon the draft without writing and without relying on typing “算了”.
9. As a customer, I want field or business errors shown on the same open form, so that I can fix and resubmit without starting over.
10. As a customer, I want a clear message if I type in chat while the form is waiting, so that I know free text will not apply the change.
11. As a customer, I want the system to refuse to write mutation data from plain chat while a form interrupt is pending, so that accidental text cannot bypass the form.
12. As a customer, I want successful completion explained in chat after submit, so that I know what changed.
13. As a customer, I want failed business rules (e.g. address not editable) explained on the form, so that I know why Confirm did not write.
14. As a support operator looking at traces, I want form wait/resume to appear as LangGraph interrupts, so that Studio and LangSmith match runtime behavior.
15. As a product owner, I want refund/cancel to stay on HighRisk approval flows, so that Mutation Form is not used for high-risk money moves.
16. As a frontend client, I want `form_required` / `form_rejected` events derived from interrupt payloads and response schema, so that I can render without a second pending-state store.
17. As a frontend client, I want Cancel and Confirm to map to Form Resume payloads, so that I do not invent a parallel session API for forms.
18. As a runtime maintainer, I want pending form interrupts to resume through the same resume path as other interrupts, so that FastAPI does not special-case “skip Router” for form JSON chat messages.
19. As a runtime maintainer, I want MutationGraph to remain closed (no internal jump to Read/HighRisk/Fallback), so that each new user intent re-enters via Router after the form flow ends.
20. As a runtime maintainer, I want tool execution through ToolNode with a single controlled tool call, so that the write path is not a ReAct loop.
21. As a customer with multiple editable orders, I want the Order Picker to list eligible candidates only, so that I cannot pick an order that precheck would reject for the wrong reason when avoidable.
22. As a customer who cancels the form, I want the conversation to return to normal AI chat, so that I can ask something else without a stuck interrupt.
23. As a customer who completes a mutation, I want the next message to go through normal routing again, so that a new intent is not trapped in the old form.
24. As a developer, I want graph-spec and event-spec updated (no separate ADR required for this feature), so that protocol and graph contract stay the source of truth.
25. As a QA engineer, I want one primary E2E seam over chat+resume, so that we validate behavior users actually hit.

## Implementation Decisions

### Architecture

- Prefer LangGraph official HITL: Form Interrupt = `interrupt(value=..., response_schema=...)`; Form Resume = `Command(resume=...)` with checkpointer and `thread_id`.
- Do not maintain a parallel “Form Pending” session flag as source of truth; checkpoint interrupts are authoritative. SSE `form_required` / `form_rejected` are projections only.
- Operation Clarify (vague “modify order”) stays in the entry/intent layer before MutationGraph. MutationGraph assumes tool is already known (Hybrid Tool Resolution: high confidence map to tool; low confidence chat clarify first).
- Closed MutationGraph: no internal handoff to Read/HighRisk/Fallback for business routing. Handoff remains the existing unified handoff interrupt if ever needed.
- Mutation tools only (address/phone/note/invoice/service ticket). HighRisk cancel/refund unchanged.
- Confirm = clicking Confirm emits Form Resume with field values; no second confirmation interrupt and no separate confirmed flag gate beyond “resume happened with submit payload”.
- Cancel = explicit form control → resume cancel sentinel → finalize without write.
- Validation failure or precheck failure → re-interrupt with errors on the same draft (form_rejected projection); do not open a brand-new unrelated form session.
- While a form interrupt is pending, plain-text chat must not write; friendly guidance to use the form. Entry uses generic “pending interrupt → resume or refuse” behavior, not a Mutation-only skip-Router hack for chat JSON.
- Transport (FastAPI/SSE/Chatwoot) adapts interrupt/resume; business gates stay inside the graph.
- Execute writes via ToolNode (or equivalent official tool runner) with a single controlled tool call constructed from resumed args + system `request_id`.
- Node shape (logical): ensure/collect form (interrupt loop with validate+precheck inside or immediately after resume) → execute → tools → single finalize. Remove chat clarify and confirmation_pause from Mutation main path.
- interrupt-before code must be idempotent because nodes re-run from the start on resume.
- Update graph-spec and event-spec; deprecate Mutation main-path use of `clarification_required` / `confirmation_required` for this flow; add `form_required` / `form_rejected` (and cancel completion via existing done/error as appropriate). No ADR file required (project choice).
- Order Picker always shown; candidates carried in interrupt value; never auto-select sole order.
- LangGraph version must support `response_schema` on interrupt (≥ 1.2.12 per upstream docs); verify against installed langgraph before implementation.

### Modules / interfaces (conceptual)

- MutationGraph workflow rebuild around Form Interrupt / Form Resume / ToolNode.
- Conversation Runtime entry: if thread has pending interrupt, handle resume or safe refusal; do not re-Router into a competing path.
- Event protocol: form_required / form_rejected shapes for clients.
- Select/intent layer: only ensure tool identity before entering Mutation; stop owning multi-turn field clarification for mutation main path.
- Frontend / Chatwoot form rendering: drive from interrupt value + response_schema; Confirm/Cancel → resume API already used for approvals/confirmations where possible.
- Order repository remains the fact source for candidates and precheck; no new dual write model.

### Runtime decoupling constraints (must hold)

- Single source of truth for waiting-on-form: graph interrupt.
- ResumeHandler remains generic over interrupt types.
- Form field schema derived from tool/graph, not duplicated as a second registry in the server.
- Subgraph does not absorb Operation Clarify.

## Testing Decisions

- Good tests assert external behavior: events seen by the client, whether a write occurred, execution_status, and that Router did not re-classify a Form Resume. Do not assert internal private helper names or exact node count.
- **Primary seam:** Conversation Runtime chat + resume E2E (same class of tests as existing mutation confirm / approval resume flows): form interrupt emitted → Confirm resume writes once → precheck failure re-interrupts with errors → Cancel does not write → plain text during interrupt does not write.
- **Optional seam:** MutationGraph unit tests with Fake model and direct `Command(resume=...)` for state transitions without HTTP.
- Prior art: existing mutation graph tests, approval resume paths in server/streaming tests, interrupt three-state coverage patterns, real_smoke mutation lanes (update after protocol change).
- Prefer offline Fake model + stub tools; live SiliconFlow smoke optional after protocol lands.
- Regression: HighRisk approval path and Read path must not regress; Mutation must not still require chat clarification for happy-path address update when tool is known.

## Out of Scope

- Redesigning HighRisk approval (cancel/refund) onto Mutation Form.
- Replacing Operation Clarify with a form-based tool picker.
- Text-only clients that can complete writes without forms (Form-Only Write).
- Auto-selecting the only eligible order.
- PR #8 pending-clarification-context chat draft merge as the main mutation path (superseded for mutation main path by Form Interrupt; may remain irrelevant or follow-up cleanup).
- Chatwoot-specific widget chrome beyond consuming the same form events/resume contract.
- Changing ReadGraph / Fallback architecture.
- Writing an ADR (explicitly declined).

## Further Notes

- Glossary terms live in `CONTEXT.md` (Mutation Form, Form Interrupt, Form Resume, Order Picker, Operation Clarify, Form-Only Write, Closed MutationGraph, ToolNode).
- Official LangGraph guidance favors interrupt + Command(resume) + optional response_schema for typed forms; this spec adopts that over “normal-turn form pending + chat JSON skip Router”.
- Installed project langgraph pin should be checked for `response_schema` support; if below 1.2.12, bump is part of enabling work.
- Seams confirmed with stakeholder: primary `/chat`+resume E2E; optional graph unit resume tests.
