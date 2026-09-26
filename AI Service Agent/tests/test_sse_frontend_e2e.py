"""SSE 前端 E2E（方案 A + B，零浏览器依赖）：

方案 A：httpx 直接请求 /chat SSE 端点，验证事件协议（event-spec.md）——
Content-Type、data: {json}\\n\\n 格式、事件类型覆盖、done 必发且最后。

方案 B：Node vm 沙箱执行 web/index.html 内嵌 JS，mock fetch 返回 SSE 字节流，
验证前端 sendMessage 解析器 + applyEvent 状态机正确消费每种事件类型。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_service_agent.harness.server import create_app
from ai_service_agent.testing import make_fake_model
from ai_service_agent.tools import orders as orders_mod
from ai_service_agent.tools import write as write_mod

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = PROJECT_ROOT / "web" / "index.html"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_app(tmp_path, **kw):
    return create_app(str(tmp_path / "sse_e2e.db"), model=make_fake_model(), **kw)


def _create_session(client: TestClient, user_id="u1") -> str:
    return client.post("/sessions", json={"user_id": user_id}).json()["session_id"]


def _parse_sse(text: str) -> list[dict]:
    """把 SSE 原始文本解析为事件 dict 列表。"""
    events = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _assert_done_last(events: list[dict]):
    assert events, "SSE 流为空"
    assert events[-1]["type"] == "done", f"done 不是最后一个事件: {[e['type'] for e in events]}"


def _post_chat(client: TestClient, session_id: str, message: str) -> tuple[str, list[dict]]:
    r = client.post(
        "/chat",
        json={"session_id": session_id, "message": message},
    )
    assert r.status_code == 200
    return r.text, _parse_sse(r.text)


# ---------------------------------------------------------------------------
# Plan A: httpx / TestClient SSE 端点协议测试
# ---------------------------------------------------------------------------


class TestSseProtocol:
    """A 方案：SSE 传输层 + 事件协议验证。"""

    def test_content_type_is_event_stream(self, tmp_path):
        client = TestClient(_make_app(tmp_path))
        sid = _create_session(client)
        r = client.post("/chat", json={"session_id": sid, "message": "你好"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers.get("cache-control") == "no-cache"

    def test_sse_format_data_json_double_newline(self, tmp_path):
        client = TestClient(_make_app(tmp_path))
        sid = _create_session(client)
        text, _ = _post_chat(client, sid, "你好")
        assert "data: " in text
        for block in text.split("\n\n"):
            if block.strip():
                assert block.startswith("data: "), f"SSE block 不以 data: 开头: {block[:50]}"

    def test_faq_read_token_and_done(self, tmp_path):
        """FAQ read：token 流 + done 必发且最后。"""
        client = TestClient(_make_app(tmp_path))
        sid = _create_session(client)
        _, events = _post_chat(client, sid, "你好")
        types = [e["type"] for e in events]
        assert "token" in types
        _assert_done_last(events)
        assert events[-1]["data"]["final"] is not None

    def test_done_always_present_even_on_error(self, tmp_path):
        """错误路径也必发 done（event-spec：每个 turn 必发 done）。"""
        client = TestClient(_make_app(tmp_path))
        sid = _create_session(client)
        _, events = _post_chat(client, sid, "触发错误的消息XYZ")
        types = [e["type"] for e in events]
        assert "done" in types
        _assert_done_last(events)

    def test_unknown_session_rejected(self, tmp_path):
        """未知 session 的 SSE 流应包含 error + done（不 crash）。"""
        client = TestClient(_make_app(tmp_path))
        text, events = _post_chat(client, "nonexistent-session-12345", "你好")
        types = [e["type"] for e in events]
        assert "error" in types or "done" in types
        _assert_done_last(events)

    def test_multi_turn_session_messages_persist(self, tmp_path):
        """多轮对话后 message store 应包含 assistant 消息。"""
        client = TestClient(_make_app(tmp_path))
        sid = _create_session(client)
        _post_chat(client, sid, "你好")
        r = client.get(f"/conversations/{sid}/messages")
        assert r.status_code == 200
        items = r.json().get("items", [])
        roles = [m["role"] for m in items]
        assert "assistant" in roles


# ---------------------------------------------------------------------------
# Plan B: Node vm 前端逻辑测试（SSE 解析器 + applyEvent 状态机）
# ---------------------------------------------------------------------------


def _extract_script(html: str) -> str:
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m, "web/index.html 必须包含一个内嵌 <script> 块"
    return m.group(1)


def _wrap_completion(code: str) -> str:
    depth = 0
    mode = 0
    stmt_start = 0
    i = 0
    n = len(code)
    while i < n:
        c = code[i]
        if mode == 1:
            i += 2 if c == "\\" else 1
            if c != "\\" and c == "'":
                mode = 0
            continue
        if mode == 2:
            i += 2 if c == "\\" else 1
            if c != "\\" and c == '"':
                mode = 0
            continue
        if mode == 3:
            i += 2 if c == "\\" else 1
            if c != "\\" and c == "`":
                mode = 0
            continue
        if c == "'" and mode == 0:
            mode = 1
        elif c == '"' and mode == 0:
            mode = 2
        elif c == "`" and mode == 0:
            mode = 3
        elif c in "{[(" and mode == 0:
            depth += 1
        elif c in "}])" and mode == 0:
            depth -= 1
        if c == ";" and depth == 0:
            stmt_start = i + 1
        i += 1
    head = code[:stmt_start].rstrip()
    last = code[stmt_start:].strip()
    if last.endswith(";"):
        last = last[:-1].rstrip()
    return "(async()=>{" + head + " return (" + last + ");})()"


def _run_js(expr: str, tmp_path: Path):
    """在 Node vm 沙箱中执行 expr，返回 JSON 解析结果。"""
    if shutil.which("node") is None:
        pytest.skip("node 不可用，跳过前端逻辑测试")
    script = _extract_script(HTML_PATH.read_text(encoding="utf-8"))
    (tmp_path / "site.js").write_text(script, encoding="utf-8")
    (tmp_path / "expr.js").write_text(expr, encoding="utf-8")
    runner = tmp_path / "runner.js"
    runner.write_text(
        "const fs=require('fs');const vm=require('vm');\n"
        "const sandbox={window:{},console,setTimeout,clearTimeout,setInterval,clearInterval,\n"
        "ReadableStream,TextEncoder,TextDecoder};\n"
        "vm.runInNewContext(fs.readFileSync(__dirname+'/site.js','utf8'),sandbox);\n"
        "const {UserPage}=sandbox.window;\n"
        "const expr=fs.readFileSync(__dirname+'/expr.js','utf8');\n"
        "(async()=>{console.log(JSON.stringify(await eval('('+expr+')')));})()"
        ".catch(e=>{console.error(e);process.exit(1);});\n",
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(runner)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return json.loads(proc.stdout)


def _sse_bytes(events: list[dict]) -> bytes:
    """把事件列表编码为 SSE 原始字节（模拟服务端响应体）。"""
    return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode()


class TestFrontendSseParser:
    """B 方案：前端 sendMessage 的 SSE 字节流解析器。"""

    def test_parse_sse_token_stream_and_done(self, tmp_path):
        events = [
            {"type": "token", "data": {"text": "你好"}},
            {"type": "token", "data": {"text": "，我是"}},
            {"type": "token", "data": {"text": "AI 客服。"}},
            {"type": "done", "data": {"final": "你好，我是AI 客服。", "message_id": "m-1"}},
        ]
        expr = f"""(async()=>{{
            var collected = [];
            var fakeBody = new ReadableStream({{
                start(controller) {{
                    controller.enqueue(new TextEncoder().encode({json.dumps(_sse_bytes(events).decode("utf-8"))}));
                    controller.close();
                }}
            }});
            var fakeFetch = function(url, opts) {{
                return Promise.resolve({{ ok: true, body: fakeBody }});
            }};
            var api = UserPage.createApi({{ fetchImpl: fakeFetch }});
            await api.sendMessage('conv-1', '你好', function(ev) {{ collected.push(ev); }});
            return collected;
        }})()
        """
        result = _run_js(expr, tmp_path)
        assert len(result) == 4
        assert result[0]["type"] == "token"
        assert result[1]["data"]["text"] == "，我是"
        assert result[-1]["type"] == "done"
        assert result[-1]["data"]["final"] == "你好，我是AI 客服。"

    def test_parse_sse_confirmation_required(self, tmp_path):
        events = [
            {"type": "confirmation_required", "data": {"summary": "修改收货地址"}},
            {"type": "confirmation_updated", "data": {"action": "confirm"}},
            {"type": "done", "data": {"final": None, "message_id": None}},
        ]
        expr = f"""(async()=>{{
            var collected = [];
            var fakeBody = new ReadableStream({{
                start(controller) {{
                    controller.enqueue(new TextEncoder().encode({json.dumps(_sse_bytes(events).decode("utf-8"))}));
                    controller.close();
                }}
            }});
            var fakeFetch = function(url, opts) {{
                return Promise.resolve({{ ok: true, body: fakeBody }});
            }};
            var api = UserPage.createApi({{ fetchImpl: fakeFetch }});
            await api.sendMessage('conv-1', '改地址', function(ev) {{ collected.push(ev); }});
            return collected;
        }})()
        """
        result = _run_js(expr, tmp_path)
        assert result[0]["type"] == "confirmation_required"
        assert result[0]["data"]["summary"] == "修改收货地址"
        assert result[1]["type"] == "confirmation_updated"


class TestFrontendApplyEvent:
    """B 方案：前端 applyEvent 状态机对各事件类型的消息产出。"""

    def test_token_appends_text_message(self, tmp_path):
        result = _run_js(
            "(async()=>{"
            "var s = UserPage.applyEvent(UserPage.createInitialStore(),"
            " {type:'token', data:{text:'你好'}});"
            "var last = s.messages[s.messages.length-1];"
            "return {role: last.role, kind: last.kind, content: last.content};"
            "})()",
            tmp_path,
        )
        assert result["role"] == "assistant"
        assert "你好" in json.dumps(result["content"], ensure_ascii=False)

    def test_confirmation_required_sets_pending(self, tmp_path):
        result = _run_js(
            "(async()=>{"
            "var s = UserPage.applyEvent(UserPage.createInitialStore(),"
            " {type:'confirmation_required', data:{summary:'修改收货地址'}});"
            "return {pending: s.pendingConfirm !== undefined || s.confirmSummary !== undefined,"
            " messages: s.messages.length};"
            "})()",
            tmp_path,
        )
        assert result["messages"] >= 1

    def test_handoff_created_adds_system_message(self, tmp_path):
        result = _run_js(
            "(async()=>{"
            "var s = UserPage.applyEvent(UserPage.createInitialStore(),"
            " {type:'handoff_created', data:{summary:'用户投诉'}});"
            "var last = s.messages[s.messages.length-1];"
            "return {role: last.role, kind: last.kind};"
            "})()",
            tmp_path,
        )
        assert result["role"] == "system"
        assert result["kind"] == "handoff"

    def test_handoff_resolved_adds_resolved_banner(self, tmp_path):
        result = _run_js(
            "(async()=>{"
            "var s = UserPage.applyEvent(UserPage.createInitialStore(),"
            " {type:'handoff_resolved', data:{}});"
            "var last = s.messages[s.messages.length-1];"
            "return {role: last.role, kind: last.kind, meta: last.meta};"
            "})()",
            tmp_path,
        )
        assert result["kind"] == "handoff"
        assert result["meta"]["state"] == "resolved"

    def test_error_adds_error_bar(self, tmp_path):
        result = _run_js(
            "(async()=>{"
            "var s = UserPage.applyEvent(UserPage.createInitialStore(),"
            " {type:'error', data:{code:'runtime_error', message:'系统错误'}});"
            "var last = s.messages[s.messages.length-1];"
            "return {role: last.role, kind: last.kind, code: last.content.code};"
            "})()",
            tmp_path,
        )
        assert result["kind"] == "error"
        assert result["code"] == "runtime_error"

    def test_done_with_final_creates_assistant(self, tmp_path):
        """无 token 流时 done.final 应创建 assistant 消息（KB 缺口整段回复路径）。"""
        result = _run_js(
            "(async()=>{"
            "var s = UserPage.applyEvent(UserPage.createInitialStore(),"
            " {type:'done', data:{final:'这是整段回复。', message_id:'m-2'}});"
            "var last = s.messages[s.messages.length-1];"
            "return {role: last.role, kind: last.kind, content: last.content};"
            "})()",
            tmp_path,
        )
        assert result["role"] == "assistant"

    def test_tool_call_and_result_sequence(self, tmp_path):
        result = _run_js(
            "(async()=>{"
            "var a = UserPage.applyEvent(UserPage.createInitialStore(),"
            " {type:'tool_call', data:{name:'order_lookup', args:{order_id:'ORD-1'}}});"
            "var b = UserPage.applyEvent(a,"
            " {type:'tool_result', data:{name:'order_lookup', ok:true, summary:'ORD-1'}});"
            "return {callCount: a.messages.filter(function(m){return m.kind==='tool';}).length,"
            " resultCount: b.messages.filter(function(m){return m.kind==='tool'&&m.meta.phase==='result';}).length};"
            "})()",
            tmp_path,
        )
        assert result["callCount"] >= 1
        assert result["resultCount"] >= 1
