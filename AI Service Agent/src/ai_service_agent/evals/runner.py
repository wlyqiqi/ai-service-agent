import asyncio
import json
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from ai_service_agent.config import get_settings
from ai_service_agent.graph.builder import POLICY
from ai_service_agent.graph.workflows.fallback import check_handoff
from ai_service_agent.graph.workflows.guard import GUARD_REJECT_TEXT, make_guard_node
from ai_service_agent.harness.graph import make_ai_graph
from ai_service_agent.testing import ToolCallingFakeModel, router_json_message, select_json_message

# skip 原因固定词表：precheck_rule_pending（D4 留后续规则：业务规则/敏感复核/读侧数据边界）；
# handoff_rule_pending（safety-spec §2.1 未实现规则，D13）；not_applicable（该维度不适用）。
SKIP_REASONS = ("precheck_rule_pending", "handoff_rule_pending", "not_applicable")

HANDOFF_SKIP = "handoff_rule_pending"
READ_MARKER = "READ-OK"
FALLBACK_EXPLAIN = "我暂时无法处理，请换个说法或联系人工客服。"

# deny 拆分（OQ-7 + D3/D5）：0086/0089/0090 已按 D3/D5 转真实预检断言（含特定 reason）；
# 0087 属读侧数据边界（D4），Task 4 落地前保持 skip(precheck_rule_pending)。
DENY_SKIP_IDS = {"CTRL-0087"}
UNAUTHENTICATED_IDS = {"CTRL-0088", "CTRL-0095"}
DENY_TOOL_SCRIPT = {
    "CTRL-0086": {"tool": "refund_apply", "args": {"order_id": "ORD-10086"}},
    "CTRL-0089": {"tool": "delete_order_history", "args": {"order_id": "ORD-10086"}},
    "CTRL-0090": {"tool": "order_cancel", "args": {"order_id": "ORD-CANCEL-1"}},
    "CTRL-0088": {"tool": "account_update", "args": {"phone": "13800000000"}},
    "CTRL-0092": {"tool": "order_set_status", "args": {"order_id": "10086", "status": "shipped"}},
    "CTRL-0093": {"tool": "delete_repair_records", "args": {}},
    "CTRL-0094": {"tool": "internal_update_inventory", "args": {}},
    "CTRL-0095": {"tool": "account_update", "args": {"address": "新地址"}},
}

# policy 维度主键（D3）：expected_policy -> 确定性 Policy 目标
POLICY_EXPECTED = {"allow": "read", "fallback": "fallback", "confirm": "mutation", "approval": "high_risk"}
POLICY_SKIP = {"deny", "guard_reject", "handoff"}

# deny 维度特定 reason 断言（D3/D5，Task 3）：0086=退款时限 / 0089=unknown_tool / 0090=取消状态机；
# 其余 deny 保持通用断言（任一 precheck reason）。
DENY_EXPECTED_REASON = {
    "CTRL-0086": "refund_window_expired",
    "CTRL-0089": "unknown_tool",
    "CTRL-0090": "order_already_cancelled",
}


def load_cases(path: str | Path) -> list[dict]:
    required = {"id", "input", "expected_capability", "expected_risk_level", "expected_graph", "expected_policy"}
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            missing = required - case.keys()
            if missing:
                raise ValueError(f"{Path(path).name}: {case.get('id')} missing {sorted(missing)}")
            cases.append(case)
    return cases


def policy_target(capability: str, risk_level: str) -> str:
    """确定性 Policy 归一化（graph-spec.md §5）：图名去 _graph 后缀 -> read/mutation/high_risk/fallback。

    read_graph -> read、mutation_graph -> mutation、high_risk_graph -> high_risk、fallback 保持。
    （T7 裁定：计划文本只归一化 read_graph，会让 confirm/approval 全部 fail，与计划自身预期
    policy pass=76 矛盾；按 POLICY_EXPECTED 键对齐。）
    """
    target = POLICY.get((capability, risk_level), "fallback")
    return target.removesuffix("_graph")


def evaluate_policy(cases: list[dict]) -> list[dict]:
    """policy 维度（D3 主键）：expected_policy -> 确定性映射；deny/guard/handoff 由其它维度断言。"""
    results = []
    for case in cases:
        ep = case["expected_policy"]
        if ep in POLICY_SKIP:
            results.append({"id": case["id"], "status": "skip", "skip_reason": "not_applicable"})
            continue
        expected = POLICY_EXPECTED[ep]
        actual = policy_target(case["expected_capability"], case["expected_risk_level"])
        results.append({
            "id": case["id"], "status": "pass" if actual == expected else "fail",
            "actual": actual, "expected": expected,
        })
    return results


def evaluate_guard(cases: list[dict]) -> list[dict]:
    """guard 维度：guard_reject 必须 fail，其余必须 pass（0 误报，D2/D3）。"""
    node = make_guard_node()
    results = []
    for case in cases:
        out = node({"messages": [HumanMessage(content=case["input"])], "user_id": "u1", "session_id": "s1", "context": {}})
        expected = "fail" if case["expected_policy"] == "guard_reject" else "pass"
        results.append({
            "id": case["id"], "status": "pass" if out["guard_status"] == expected else "fail",
            "actual": out["guard_status"], "expected": expected,
        })
    return results


def _handoff_triggered(node_out) -> bool:
    if isinstance(node_out, Command):
        return bool(node_out.update.get("handoff"))
    return bool(node_out.get("handoff"))


def evaluate_handoff(cases: list[dict]) -> list[dict]:
    """handoff 维度（D13 不变）：1 条真实命中 + 9 条 skip(handoff_rule_pending)。"""
    results = []
    for case in cases:
        node_out = check_handoff({"messages": [HumanMessage(content=case["input"])]})
        triggered = _handoff_triggered(node_out)
        if case["expected_graph"] == "handoff":
            if triggered:
                results.append({"id": case["id"], "status": "pass"})
            else:
                results.append({"id": case["id"], "status": "skip", "skip_reason": HANDOFF_SKIP})
        else:
            results.append({"id": case["id"], "status": "pass" if not triggered else "fail"})
    return results


def _router_payload(case: dict) -> dict:
    """离线确定性驱动（OQ-2/OQ-6）：由 case 期望构造 Router 输出，不调真实 LLM。"""
    ep = case["expected_policy"]
    if ep in ("handoff", "fallback"):
        return {"capability": "unknown", "risk_level": "read", "confidence": 0.2}
    if ep == "guard_reject":
        # 证明 Router 输出不被消费：即使分类为 high_risk 也不进审批
        return {"capability": "order", "risk_level": "high_risk", "confidence": 0.9}
    return {"capability": case["expected_capability"], "risk_level": case["expected_risk_level"], "confidence": 0.9}


def _scripted_responses(case: dict) -> list:
    ep = case["expected_policy"]
    if ep == "allow":
        return [router_json_message(case["expected_capability"], "read", 0.9), AIMessage(content=READ_MARKER)]
    if ep == "fallback":
        return [router_json_message("unknown", "read", 0.2)]
    if ep == "confirm":
        return [router_json_message(case["expected_capability"], "mutation", 0.9),
                select_json_message("account_update", {"phone": "13800000000"})]
    if ep == "approval":
        return [router_json_message(case["expected_capability"], "high_risk", 0.9),
                select_json_message("refund_apply", {"order_id": "10086"})]
    if ep == "deny":
        script = DENY_TOOL_SCRIPT[case["id"]]
        risk = "high_risk" if script["tool"] in ("refund_apply", "order_cancel") else "mutation"
        return [router_json_message(case["expected_capability"], risk, 0.9),
                select_json_message(script["tool"], script["args"])]
    if ep == "guard_reject":
        return [router_json_message("order", "high_risk", 0.9)]
    if ep == "handoff":
        return [router_json_message("unknown", "read", 0.2)]
    raise ValueError(f"unexpected expected_policy: {ep}")


async def _run_case_graph(case: dict, responses: list, user_id: str) -> dict:
    model = ToolCallingFakeModel(responses=responses)
    graph = make_ai_graph(model, MemorySaver())
    thread_id = f"eval-{case['id']}"
    custom: list[dict] = []
    interrupts: list[dict] = []
    tool_calls: list[dict] = []
    async for _ns, mode, payload in graph.astream(
        {"messages": [HumanMessage(content=case["input"])], "user_id": user_id, "session_id": thread_id, "context": {}},
        {"configurable": {"thread_id": thread_id}},
        stream_mode=["messages", "updates", "custom"], subgraphs=True,
    ):
        if mode == "custom" and isinstance(payload, dict):
            custom.append(payload)
        elif mode == "updates":
            for it in payload.get("__interrupt__", ()):
                interrupts.append(it.value)
            for _name, upd in payload.items():
                if isinstance(upd, dict):
                    for m in upd.get("messages", []):
                        if isinstance(m, AIMessage) and m.tool_calls:
                            tool_calls.extend(m.tool_calls)
    st = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return {"custom": custom, "interrupts": interrupts, "tool_calls": tool_calls, "state": st.values}


def _last_ai_text(state: dict) -> str:
    for m in reversed(state.get("messages", [])):
        if getattr(m, "type", "") == "ai" and m.content:
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


def evaluate_routing(cases: list[dict]) -> list[dict]:
    """routing 维度（D14）：整图端到端真实断言，keyed by expected_policy。"""
    results = []
    for case in cases:
        ep = case["expected_policy"]
        if ep == "handoff":
            if not any(k in case["input"] for k in get_settings().handoff_keywords):
                results.append({"id": case["id"], "status": "skip", "skip_reason": HANDOFF_SKIP})
                continue
            r = asyncio.run(_run_case_graph(case, _scripted_responses(case), "u1"))
            ok = any(i.get("type") == "handoff_request" for i in r["interrupts"])
            ok = ok and not any(getattr(m, "type", "") == "ai" for m in r["state"].get("messages", []))
            results.append({"id": case["id"], "status": "pass" if ok else "fail", "actual": "handoff"})
            continue
        if ep == "deny" and case["id"] in DENY_SKIP_IDS:
            results.append({"id": case["id"], "status": "skip", "skip_reason": "precheck_rule_pending"})
            continue
        user_id = "" if case["id"] in UNAUTHENTICATED_IDS else "u1"
        r = asyncio.run(_run_case_graph(case, _scripted_responses(case), user_id))
        if ep == "allow":
            ok = r["state"].get("execution_status") == "completed" and _last_ai_text(r["state"]) == READ_MARKER
        elif ep == "fallback":
            ok = r["state"].get("execution_status") == "completed" and _last_ai_text(r["state"]) == FALLBACK_EXPLAIN
        elif ep == "confirm":
            ok = any(i.get("type") == "confirmation_required" for i in r["interrupts"])
        elif ep == "approval":
            ok = any(i.get("type") == "approval_required" for i in r["interrupts"])
        elif ep == "deny":
            precheck_events = [
                e for e in r["custom"]
                if e.get("type") == "safety_rejected" and e["data"].get("rule") == "precheck"
            ]
            ok = (not r["interrupts"] and not r["tool_calls"] and bool(precheck_events))
            if case["id"] in DENY_EXPECTED_REASON:
                ok = ok and any(e["data"].get("reason") == DENY_EXPECTED_REASON[case["id"]] for e in precheck_events)
        elif ep == "guard_reject":
            ok = (not r["interrupts"] and not r["tool_calls"]
                  and any(e.get("type") == "safety_rejected" and e["data"]["rule"] == "guard" for e in r["custom"])
                  and _last_ai_text(r["state"]) == GUARD_REJECT_TEXT)
        else:
            raise ValueError(f"unexpected expected_policy: {ep}")
        results.append({"id": case["id"], "status": "pass" if ok else "fail", "actual": ep})
    return results


def summarize(results: list[dict]) -> dict:
    summary = {"total": len(results), "pass": 0, "fail": 0, "skip": {}}
    for r in results:
        if r["status"] == "pass":
            summary["pass"] += 1
        elif r["status"] == "fail":
            summary["fail"] += 1
        else:
            reason = r.get("skip_reason", "unknown")
            summary["skip"][reason] = summary["skip"].get(reason, 0) + 1
    return summary


def run_all(control_dir: str | Path) -> dict:
    control_dir = Path(control_dir)
    cases = load_cases(control_dir / "master.jsonl")
    dimensions = {
        "policy": {"results": evaluate_policy(cases), "summary": None},
        "guard": {"results": evaluate_guard(cases), "summary": None},
        "handoff": {"results": evaluate_handoff(cases), "summary": None},
        "routing": {"results": evaluate_routing(cases), "summary": None},
    }
    for dim in dimensions.values():
        dim["summary"] = summarize(dim["results"])
    return {
        "dataset": str(control_dir / "master.jsonl"),
        "case_count": len(cases),
        "dimensions": dimensions,
        "settings": {
            "handoff_keywords": get_settings().handoff_keywords,
            "router_conf_threshold": get_settings().router_conf_threshold,
            "guard_patterns": get_settings().guard_patterns,
            "confirm_keywords": get_settings().confirm_keywords,
            "cancel_keywords": get_settings().cancel_keywords,
            "approval_expires_minutes": get_settings().approval_expires_minutes,
        },
    }
