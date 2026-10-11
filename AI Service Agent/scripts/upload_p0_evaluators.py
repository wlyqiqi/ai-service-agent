"""Upload P0 code evaluators to LangSmith workspace and attach to functional dataset.

Official SDK path: client.evaluators.create/update (langsmith>=0.9.8).
Dataset attach via runs/rules REST when SDK has no first-class binder.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

import httpx
from langsmith import Client

from ai_service_agent.config import get_settings
from ai_service_agent.evals.p0_evaluators import (
    P0_EVALUATOR_SPECS,
    P0EvaluatorSpec,
    build_hosted_perform_eval_source,
    workspace_evaluator_name,
)

DEFAULT_DATASET_NAME = "ai-cs-functional-scenarios-v1"


def _api_base(client: Client) -> str:
    api_url = getattr(client, "api_url", None) or os.environ.get(
        "LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"
    )
    return str(api_url).rstrip("/")


async def _find_existing(client: Client, name: str) -> Any | None:
    page = await client.evaluators.list(name_contains=name, type="code", limit=50)
    evaluators = getattr(page, "evaluators", None) or []
    for ev in evaluators:
        if getattr(ev, "name", None) == name:
            return ev
    # list() may be async iterable in some versions
    if hasattr(page, "__aiter__"):
        async for ev in page:
            if getattr(ev, "name", None) == name:
                return ev
    return None


async def upsert_evaluator(client: Client, spec: P0EvaluatorSpec, *, dry_run: bool) -> dict:
    name = workspace_evaluator_name(spec)
    code = build_hosted_perform_eval_source(spec.key)
    existing = await _find_existing(client, name)
    payload = {
        "name": name,
        "type": "code",
        "code_evaluator": {"code": code, "language": "python"},
    }
    if dry_run:
        return {
            "action": "update" if existing else "create",
            "name": name,
            "key": spec.key,
            "zh_note": spec.zh_note,
            "existing_id": str(getattr(existing, "id", "") or "") or None,
            "dry_run": True,
        }
    if existing is not None:
        updated = await client.evaluators.update(
            str(existing.id),
            name=name,
            code_evaluator=payload["code_evaluator"],
        )
        ev = getattr(updated, "evaluator", updated)
        return {
            "action": "update",
            "name": name,
            "key": spec.key,
            "id": str(getattr(ev, "id", existing.id)),
            "feedback_keys": list(getattr(ev, "feedback_keys", None) or [spec.key]),
        }
    created = await client.evaluators.create(**payload)
    ev = getattr(created, "evaluator", created)
    return {
        "action": "create",
        "name": name,
        "key": spec.key,
        "id": str(ev.id),
        "feedback_keys": list(getattr(ev, "feedback_keys", None) or [spec.key]),
    }


def _list_run_rules(api_base: str, api_key: str, dataset_id: str) -> list[dict]:
    url = f"{api_base}/api/v1/runs/rules"
    with httpx.Client(timeout=60.0, trust_env=False) as http:
        resp = http.get(
            url,
            headers={"x-api-key": api_key},
            params={"dataset_id": dataset_id},
        )
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            # Some tenants use query without filter; fall back to empty and create.
            return []
        data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.get("rules") or data.get("items") or data.get("data") or [])
    return []


def attach_evaluator_to_dataset(
    *,
    api_base: str,
    api_key: str,
    evaluator_id: str,
    dataset_id: str,
    display_name: str,
    dry_run: bool,
) -> dict:
    rules = _list_run_rules(api_base, api_key, dataset_id)
    existing = None
    for rule in rules:
        if str(rule.get("evaluator_id") or "") == str(evaluator_id) and str(
            rule.get("dataset_id") or ""
        ) == str(dataset_id):
            existing = rule
            break
    body = {
        "display_name": display_name,
        "dataset_id": str(dataset_id),
        "evaluator_id": str(evaluator_id),
        "sampling_rate": 1.0,
        "is_enabled": True,
    }
    if dry_run:
        return {
            "action": "update_rule" if existing else "create_rule",
            "evaluator_id": evaluator_id,
            "dataset_id": dataset_id,
            "dry_run": True,
        }
    headers = {"x-api-key": api_key, "content-type": "application/json"}
    with httpx.Client(timeout=60.0, trust_env=False) as http:
        if existing and existing.get("id"):
            resp = http.patch(
                f"{api_base}/api/v1/runs/rules/{existing['id']}",
                headers=headers,
                json=body,
            )
            if resp.status_code >= 400:
                resp = http.put(
                    f"{api_base}/api/v1/runs/rules/{existing['id']}",
                    headers=headers,
                    json=body,
                )
            action = "update_rule"
        else:
            resp = http.post(
                f"{api_base}/api/v1/runs/rules",
                headers=headers,
                json=body,
            )
            action = "create_rule"
        if resp.status_code >= 400:
            return {
                "action": action,
                "ok": False,
                "status_code": resp.status_code,
                "error": resp.text[:500],
                "evaluator_id": evaluator_id,
                "dataset_id": dataset_id,
            }
        data = resp.json() if resp.content else {}
    return {
        "action": action,
        "ok": True,
        "rule_id": str((data or {}).get("id") or (existing or {}).get("id") or ""),
        "evaluator_id": evaluator_id,
        "dataset_id": dataset_id,
    }


async def async_main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.langsmith_api_key:
        raise RuntimeError("LANGSMITH_API_KEY required")
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    if settings.langsmith_project:
        os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)

    client = Client()
    api_base = _api_base(client)
    api_key = settings.langsmith_api_key

    dataset_id = None
    if not args.skip_attach:
        ds = client.read_dataset(dataset_name=args.dataset_name)
        dataset_id = str(ds.id)

    rows: list[dict] = []
    for spec in P0_EVALUATOR_SPECS:
        row = await upsert_evaluator(client, spec, dry_run=args.dry_run)
        if not args.skip_attach and dataset_id and row.get("id"):
            attach = attach_evaluator_to_dataset(
                api_base=api_base,
                api_key=api_key,
                evaluator_id=str(row["id"]),
                dataset_id=dataset_id,
                display_name=f"{workspace_evaluator_name(spec)} @ {args.dataset_name}",
                dry_run=args.dry_run,
            )
            row["attach"] = attach
        elif not args.skip_attach and args.dry_run and dataset_id:
            row["attach"] = {
                "action": "create_or_update_rule",
                "dataset_id": dataset_id,
                "dry_run": True,
            }
        rows.append(row)

    summary = {
        "dataset_name": args.dataset_name,
        "dataset_id": dataset_id,
        "skip_attach": args.skip_attach,
        "dry_run": args.dry_run,
        "evaluators": rows,
        "note": (
            "CI functional_langsmith_eval uses local P0_EVALUATORS (path A). "
            "If experiment feedback keys appear doubled after attach, re-run with --skip-attach "
            "and keep workspace evaluators for UI reuse only."
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    failed_attach = [
        r for r in rows
        if isinstance(r.get("attach"), dict) and r["attach"].get("ok") is False
    ]
    return 2 if failed_attach else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Upsert P0 Code Evaluators to LangSmith workspace and optionally attach "
            "them to the functional dataset. Use --skip-attach if bound + SDK "
            "evaluators double-count the same feedback keys."
        )
    )
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-attach",
        action="store_true",
        help="Only create/update workspace evaluators; do not bind to dataset",
    )
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
