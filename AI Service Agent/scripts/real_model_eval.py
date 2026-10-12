"""Opt-in real-model evaluation with the existing FastAPI + LangGraph runtime.

Requires ARK_MODEL / ARK_BASE_URL / ARK_API_KEY in .env. FAQ cases also require
LightRAG at localhost:9621. This script is intentionally not part of pytest.
"""

import argparse
import asyncio
import importlib.util
import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ai_service_agent.config import get_settings, make_model
from ai_service_agent.evals.route_intent import build_messages
from langchain_core.messages import AIMessage, HumanMessage
from ai_service_agent.graph.route_policy import resolve_route_policy
from ai_service_agent.graph.router import make_deepseek_router, make_jev_router
from ai_service_agent.harness.server import create_app


BASE = "http://127.0.0.1:8765"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "tests" / "evals" / "real_model" / "master.jsonl"
_DB_PATH = ""
_SNAPSHOT_PATH = ""


class NullChatwoot:
    """Isolated eval stand-in for the optional Chatwoot transport."""

    async def ensure_conversation(self, *args, **kwargs):
        return {}

    async def send_bot_message(self, *args, **kwargs):
        return {}

    async def set_conversation_status(self, *args, **kwargs):
        return {}

    async def list_messages(self, *args, **kwargs):
        return []

    async def send_as_contact(self, *args, **kwargs):
        return {}


def load_seed_script():
    path = ROOT / "scripts" / "seed_mock_db.py"
    spec = importlib.util.spec_from_file_location("real_model_eval_seed", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load seed script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = json.loads(line)
            if any(row["id"] == case["id"] for row in cases):
                raise ValueError(f"duplicate case id: {case['id']}")
            cases.append(case)
    if not 30 <= len(cases) <= 40:
        raise ValueError(f"real-model dataset must contain 30-40 cases, got {len(cases)}")
    return cases


def parse_sse(text: str) -> list[dict]:
    events = []
    payload: list[str] = []
    for line in text.splitlines():
        if line == "":
            if payload:
                events.append(json.loads("\n".join(payload)))
                payload = []
        elif line.startswith("data: "):
            payload.append(line.removeprefix("data: "))
    if payload:
        events.append(json.loads("\n".join(payload)))
    return events


def required_events(events: list[dict]) -> list[str]:
    watched = {
        "handoff_created", "clarification_required", "confirmation_required",
        "form_required", "form_rejected",
        "approval_required", "tool_call", "tool_result", "done", "error",
    }
    return [event.get("type", "") for event in events if event.get("type", "") in watched]


def tool_names(events: list[dict]) -> set[str]:
    names = set()
    for event in events:
        if event.get("type") in {"tool_call", "tool_result"}:
            name = event.get("data", {}).get("name")
            if name:
                names.add(name)
    return names


def normalize_router_messages(messages: list[dict]) -> list[AIMessage | HumanMessage]:
    normalized = []
    for message in messages:
        role = message.get("role", message.get("type", "human"))
        message_type = AIMessage if str(role).lower() in {"ai", "assistant"} else HumanMessage
        normalized.append(message_type(content=message.get("content", "")))
    return normalized


def make_eval_router():
    jev_router = make_jev_router()
    deepseek_router = make_deepseek_router(make_model())

    async def route(state: dict, config=None) -> dict:
        output = await jev_router(state, config)
        if output.get("route_control") == "deepseek_fallback":
            return await deepseek_router(state, config)
        return output

    return route


def conversation_thread_id(conversation_id: str) -> str:
    conn = sqlite3.connect(_DB_PATH)
    try:
        row = conn.execute(
            "SELECT thread_id FROM conversation_status WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else ""


def create_eval_snapshot(db_path: str, snapshot_path: str) -> None:
    source = sqlite3.connect(db_path)
    try:
        destination = sqlite3.connect(snapshot_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def reset_eval_database(snapshot_path: str, db_path: str) -> None:
    source = sqlite3.connect(snapshot_path)
    try:
        destination = sqlite3.connect(db_path)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
    finally:
        source.close()


def graph_state(db_path: str, thread_id: str) -> dict:
    from ai_service_agent.harness.graph import make_ai_graph
    from ai_service_agent.testing import make_fake_model

    async def read():
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = make_ai_graph(make_fake_model(), saver)
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            state_tools = []
            for message in state.values.get("messages", []):
                if getattr(message, "type", "") == "ai":
                    state_tools.extend(
                        tool_call.get("name")
                        for tool_call in getattr(message, "tool_calls", None) or []
                        if tool_call.get("name")
                    )
            return {
                "route_intent": state.values.get("route_intent"),
                "target_graph": state.values.get("target_graph"),
                "execution_status": state.values.get("execution_status"),
                "interrupts": [getattr(item, "value", None) for item in state.interrupts],
                "tools": state_tools,
            }

    return asyncio.run(read())


def new_session(client: httpx.Client, user_id: str) -> str:
    response = client.post(f"{BASE}/sessions", json={"user_id": user_id})
    response.raise_for_status()
    return response.json()["session_id"]


def chat(client: httpx.Client, session_id: str, message: str) -> tuple[int, list[dict]]:
    with client.stream("POST", f"{BASE}/chat", json={"session_id": session_id, "message": message}) as response:
        text = "".join(response.iter_text())
    return response.status_code, parse_sse(text)


def evaluate_router(case: dict, router) -> dict:
    started = time.perf_counter()
    output = router({
        "messages": normalize_router_messages(build_messages({
            "current_user_message": case["input"],
            "recent_context": case.get("recent_context", []),
        })),
        "user_id": case["user_id"],
        "session_id": case["id"],
        "context": {},
    })
    if asyncio.iscoroutine(output):
        output = asyncio.run(output)
    latency_ms = (time.perf_counter() - started) * 1000
    actual_intent = output.get("route_intent")
    actual_target = resolve_route_policy(actual_intent).target_graph
    problems = []
    if actual_intent != case["expected_route_intent"]:
        problems.append(f"route_intent expected={case['expected_route_intent']} actual={actual_intent}")
    if actual_target != case["expected_target_graph"]:
        problems.append(f"target_graph expected={case['expected_target_graph']} actual={actual_target}")
    return {
        "id": case["id"],
        "category": case["category"],
        "status": "fail" if problems else "pass",
        "problems": problems,
        "actual": {
            "route_intent": actual_intent,
            "target_graph": actual_target,
            "confidence": output.get("confidence"),
            "routing_backend": output.get("routing_backend"),
            "routing_fallback": output.get("routing_fallback"),
            "latency_ms": round(latency_ms, 1),
        },
    }


def evaluate_graph(client: httpx.Client, db_path: str, case: dict) -> dict:
    session_id = new_session(client, case["user_id"])
    started = time.perf_counter()
    status_code, events = chat(client, session_id, case["input"])
    timeline = [required_events(events)]

    pending_form = any(
        event.get("type") in {"form_required", "form_rejected", "confirmation_required"}
        for event in events
    )
    if case.get("confirm") and pending_form:
        # Form Resume via /actions/confirm (Form-Only Write); not chat「确认」.
        payload: dict = {"conversation_id": session_id, "action": "confirm"}
        if case.get("confirm_order_id") is not None:
            payload["order_id"] = case["confirm_order_id"]
        if case.get("confirm_fields"):
            payload["fields"] = case["confirm_fields"]
        response = client.post(f"{BASE}/actions/confirm", json=payload)
        if response.status_code != 200:
            status_code = response.status_code
            confirmation_events = [
                {"type": "error", "data": {"code": "confirm_http", "message": response.text[:200]}}
            ]
        else:
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            confirmation_events = list(body.get("events") or [])
            if not any(e.get("type") == "done" for e in confirmation_events) and body.get("final") is not None:
                confirmation_events.append({"type": "done", "data": {"final": body.get("final")}})
        events.extend(confirmation_events)
        timeline.append(required_events(confirmation_events))

    approval_id = next(
        (event.get("data", {}).get("approval_id") for event in events if event.get("type") == "approval_required"),
        None,
    )
    approval = None
    if approval_id and case.get("approve"):
        response = client.post(f"{BASE}/approvals/{approval_id}/approve")
        approval = {"http_status": response.status_code, "body": response.json()}
        if response.status_code != 200:
            status_code = response.status_code

    latency_ms = (time.perf_counter() - started) * 1000
    state = graph_state(db_path, conversation_thread_id(session_id))
    actual_tools = tool_names(events) | set(state.get("tools") or [])
    expected_tools = set(case.get("expected_tools", []))
    actual_events = required_events(events)

    problems = []
    if status_code != 200:
        problems.append(f"http status={status_code}")
    if "error" in actual_events:
        problems.append("error event emitted")
    if "done" not in actual_events:
        problems.append("done event missing")
    for event_type in case.get("expected_events", []):
        if event_type not in actual_events:
            problems.append(f"event missing: {event_type}")
    for event_type in case.get("forbidden_events", []):
        if event_type in actual_events:
            problems.append(f"forbidden event emitted: {event_type}")
    if actual_tools != expected_tools:
        problems.append(f"tools expected={sorted(expected_tools)} actual={sorted(actual_tools)}")
    if case["expected_route_intent"] and state["route_intent"] != case["expected_route_intent"]:
        problems.append(f"route_intent expected={case['expected_route_intent']} actual={state['route_intent']}")
    if case["expected_target_graph"] and state["target_graph"] != case["expected_target_graph"]:
        problems.append(f"target_graph expected={case['expected_target_graph']} actual={state['target_graph']}")

    return {
        "id": case["id"],
        "category": case["category"],
        "status": "fail" if problems else "pass",
        "problems": problems,
        "session_id": session_id,
        "thread_id": conversation_thread_id(session_id),
        "actual": {
            "route_intent": state["route_intent"],
            "target_graph": state["target_graph"],
            "execution_status": state["execution_status"],
            "interrupts": state["interrupts"],
            "tools": sorted(actual_tools),
            "events": actual_events,
            "timeline": timeline,
            "approval": approval,
            "latency_ms": round(latency_ms, 1),
        },
    }


def evaluate_handoff(client: httpx.Client, case: dict) -> dict:
    session_id = new_session(client, case["user_id"])
    started = time.perf_counter()
    status_code, events = chat(client, session_id, case["input"])
    latency_ms = (time.perf_counter() - started) * 1000
    actual_events = required_events(events)
    problems = []
    if status_code != 200:
        problems.append(f"http status={status_code}")
    if "error" in actual_events:
        problems.append("error event emitted")
    for event_type in case.get("expected_events", []):
        if event_type not in actual_events:
            problems.append(f"event missing: {event_type}")
    return {
        "id": case["id"],
        "category": case["category"],
        "status": "fail" if problems else "pass",
        "problems": problems,
        "session_id": session_id,
        "actual": {"events": actual_events, "latency_ms": round(latency_ms, 1)},
    }


def start_server(app, host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    return server


def main() -> int:
    global _DB_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--categories", nargs="*")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.model_api_key or not settings.model_endpoint:
        raise RuntimeError("real-model eval requires ARK_API_KEY and ARK_BASE_URL in .env")

    cases = load_cases(args.dataset)
    if args.categories:
        cases = [case for case in cases if case["category"] in set(args.categories)]
    if args.limit:
        cases = cases[: args.limit]

    results = []
    router = make_eval_router()
    with tempfile.TemporaryDirectory(prefix="real-model-eval-", ignore_cleanup_errors=True) as temp_dir:
        db_path = str(Path(temp_dir) / "eval.db")
        load_seed_script().build_database(db_path)
        _DB_PATH = db_path
        settings.mock_ecommerce_db_path = db_path
        app = create_app(
            db_path,
            model=make_model(),
            simulate_approval=False,
            auth_enabled=False,
            chatwoot=NullChatwoot(),
            profile_db_path=db_path,
        )
        server = start_server(app, "127.0.0.1", 8765)
        try:
            snapshot_path = str(Path(temp_dir) / "eval-clean.db")
            create_eval_snapshot(db_path, snapshot_path)
            global _SNAPSHOT_PATH
            _SNAPSHOT_PATH = snapshot_path
            with httpx.Client(timeout=180) as client:
                for case in cases:
                    print(f"RUN {case['id']} [{case['execution_mode']}/{case['category']}]")
                    if case["execution_mode"] == "router":
                        result = evaluate_router(case, router)
                    elif case["execution_mode"] == "handoff":
                        reset_eval_database(snapshot_path, db_path)
                        result = evaluate_handoff(client, case)
                    else:
                        reset_eval_database(snapshot_path, db_path)
                        result = evaluate_graph(client, db_path, case)
                    print(f"    {result['status'].upper()} {'; '.join(result['problems'])}")
                    results.append(result)
                    if args.fail_fast and result["status"] == "fail":
                        break
        finally:
            server.should_exit = True

    latencies = [result["actual"].get("latency_ms", 0) for result in results]
    router_results = [result for result in results if result["id"].startswith("REAL-ROUTE-")]
    report = {
        "summary": {
            "cases": len(results),
            "passed": sum(result["status"] == "pass" for result in results),
            "failed": sum(result["status"] == "fail" for result in results),
            "router_cases": len(router_results),
            "router_passed": sum(result["status"] == "pass" for result in router_results),
            "latency_ms": {
                "avg": round(sum(latencies) / len(latencies), 1) if latencies else None,
                "p50": sorted(latencies)[len(latencies) // 2] if latencies else None,
                "max": max(latencies) if latencies else None,
            },
        },
        "results": results,
    }
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1 if any(result["status"] == "fail" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
