# Product

## Audience

- Primary user: household customers of a Qingdao-origin global smart home brand who need product setup, order, service, and human escalation support.
- Key moment: arriving customers should immediately understand the brand is global, intelligent, and trustworthy before contacting AI support.

## Capabilities

- Login with phone number and password against the existing `/auth/login` API.
- Create an authenticated service session through the existing `/sessions` API.
- Launch the existing Chatwoot/WootChat widget as the AI-service surface; it remains an immutable black box.
- Preserve auth/session behavior, fallback mock behavior, and redirect semantics.

## Constraints

- Do not modify the Chatwoot/WootChat widget, APIs, message handling, or interaction logic.
- Do not replace or reimplement its internal behavior.
- Do not change backend, agent, LangGraph, router, runtime, or business logic.
- Keep the user-facing surface premium, spatial, global, and cinematic while avoiding generic AI-SaaS styling.

## Platform

- Web, delivered as the existing static frontend entrypoint.
