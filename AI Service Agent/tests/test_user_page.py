"""用户页面测试（web/index.html 单文件）。

结构测试：pytest 直接读 HTML。
逻辑测试：提取内嵌 <script>，用 Node vm 在无 DOM 沙箱中执行纯逻辑层，
断言 window.UserPage 暴露的函数行为。Node 缺失时跳过逻辑测试。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = PROJECT_ROOT / "web" / "index.html"


@pytest.fixture
def html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _extract_script(html: str) -> str:
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m, "web/index.html 必须包含一个内嵌 <script> 块"
    return m.group(1)


def _wrap_completion(code: str) -> str:
    """把表达式包装为 async IIFE，用 return 捕获最后一个顶层表达式的完成值。

    Node 的 eval 把代码解析为 Script，顶层 await 在 Script 中是非法的
    （即使在 async 函数内调用 eval 也一样），因此测试表达式里的顶层 await
    需要先包进 async 函数体。表达式约定：最后一个顶层表达式即结果。
    """
    depth = 0
    mode = 0  # 0=代码 1=单引号 2=双引号 3=模板串
    stmt_start = 0
    i, n = 0, len(code)
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
        if c in "'\"`":
            mode = {"'": 1, '"': 2, "`": 3}[c]
            i += 1
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "/":
            while i < n and code[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "*":
            i += 2
            while i + 1 < n and not (code[i] == "*" and code[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]":
            depth -= 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                stmt_start = i  # 顶层块语句（try/catch、function 声明等）到此结束
            continue
        if c == ";" and depth == 0:
            stmt_start = i + 1
        i += 1
    head = code[:stmt_start].rstrip()
    last = code[stmt_start:].strip()
    if last.endswith(";"):
        last = last[:-1].rstrip()
    return "(async()=>{" + head + " return (" + last + ");})()"


def _run_js(expr: str, tmp_path: Path):
    """在 Node vm 沙箱中执行 expr（作用域内 UserPage = window.UserPage），返回 JSON 解析结果。"""
    if shutil.which("node") is None:
        pytest.skip("node 不可用，跳过前端逻辑测试（需要 Node >= 18）")
    script = _extract_script(HTML_PATH.read_text(encoding="utf-8"))
    (tmp_path / "site.js").write_text(script, encoding="utf-8")
    (tmp_path / "expr.js").write_text(_wrap_completion(expr), encoding="utf-8")
    runner = tmp_path / "runner.js"
    runner.write_text(
        "const fs=require('fs');const vm=require('vm');\n"
        "const sandbox={window:{},console,setTimeout,clearTimeout,setInterval,clearInterval,\n"
        "ReadableStream,TextEncoder,TextDecoder};\n"
        "vm.runInNewContext(fs.readFileSync(__dirname+'/site.js','utf8'),sandbox);\n"
        "const {UserPage}=sandbox.window;\n"
        "const expr=fs.readFileSync(__dirname+'/expr.js','utf8');\n"
        "(async()=>{console.log(JSON.stringify(await eval(expr)));})()"
        ".catch(e=>{console.error(e);process.exit(1);});\n",
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(runner)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_single_file_no_external_network(html):
    assert "<script src=" not in html, "禁止外部 JS"
    assert '<link rel="stylesheet"' not in html, "禁止外部 CSS"
    assert "http://" not in html and "https://" not in html, "禁止外部网络资源"


def test_has_two_views_and_login_fields(html):
    for marker in (
        'id="view-login"', 'id="view-chat"',
        'id="login-account"', 'id="login-password"',
        'id="btn-login"', 'id="btn-logout"',
        'id="msg-input"', 'id="btn-send"',
        'id="msg-list"', 'id="status-tag"', 'id="chips"',
    ):
        assert marker in html, f"缺少 {marker}"


def test_resolve_view_requires_token(tmp_path):
    out = _run_js(
        "UserPage.resolveView('#/chat', false) + '|' + UserPage.resolveView('#/chat', true) + '|' "
        "+ UserPage.resolveView('#/login', true)",
        tmp_path,
    )
    assert out == "login|chat|login"


def test_initial_store_shape(tmp_path):
    out = _run_js("UserPage.createInitialStore()", tmp_path)
    assert out["view"] is None
    assert out["auth"] is None
    assert out["conversation"] == {"conversation_id": None, "mode": "ai"}
    assert out["messages"] == []
    assert out["conn"]["status"] == "idle"
    assert out["pendingAction"] is None


def test_apply_token_appends_streaming_text(tmp_path):
    out = _run_js(
        "UserPage.applyEvent(UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'token',data:{text:'您'}}), {type:'token',data:{text:'好'}})",
        tmp_path,
    )
    assert out["messages"][-1]["kind"] == "text"
    assert out["messages"][-1]["content"] == "您好"
    assert out["conn"]["status"] == "streaming"


def test_apply_done_ends_turn(tmp_path):
    out = _run_js(
        "UserPage.applyEvent(UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'token',data:{text:'x'}}), {type:'done',data:{final:true}})",
        tmp_path,
    )
    assert out["conn"]["status"] == "idle"


def test_apply_tool_call_then_result(tmp_path):
    out = _run_js(
        "UserPage.applyEvent(UserPage.applyEvent(UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'tool_call',data:{name:'order_lookup',args:{order_id:'A1001'}}}), "
        "{type:'tool_result',data:{name:'order_lookup',ok:true,summary:'已发货'}}), "
        "{type:'done',data:{}})",
        tmp_path,
    )
    tool = [m for m in out["messages"] if m["kind"] == "tool"][0]
    assert tool["content"]["name"] == "order_lookup"
    assert tool["meta"] == {"phase": "result", "ok": True, "summary": "已发货"}


def test_apply_clarify_and_safety(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'clarification_required',data:{missing:['order_id']}});"
        "var c=UserPage.applyEvent(b,{type:'safety_rejected',data:{rule:'refund_blacklist',reason:'超期'}});"
        "({kinds:c.messages.map(function(m){return m.kind;})})",
        tmp_path,
    )
    assert out["kinds"] == ["clarify", "notice"]


def test_apply_confirm_and_update(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'confirmation_required',data:{summary:'修改收货地址'}});"
        "var c=UserPage.applyEvent(b,{type:'confirmation_updated',data:{action:'confirmed'}});"
        "({pa:c.pendingAction,meta:c.messages[0].meta})",
        tmp_path,
    )
    assert out["pa"]["kind"] == "confirm" and out["pa"]["status"] == "confirmed"
    assert out["meta"]["status"] == "confirmed"


def test_apply_approval_and_update(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'approval_required',data:{approval_id:'ap-1',summary:'取消订单',expires_at:'2026-09-17T23:59:59Z'}});"
        "var c=UserPage.applyEvent(b,{type:'approval_updated',data:{approval_id:'ap-1',status:'approved'}});"
        "({pa:c.pendingAction,meta:c.messages[0].meta})",
        tmp_path,
    )
    assert out["pa"] == {
        "kind": "approval", "id": "ap-1", "status": "approved",
        "summary": "取消订单", "expires_at": "2026-09-17T23:59:59Z",
    }
    assert out["meta"]["status"] == "approved"


def test_apply_handoff_cycle(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'handoff_created',data:{reason:'情绪升级'}});"
        "var c=UserPage.applyEvent(b,{type:'handoff_resolved',data:{}});"
        "({mode:c.conversation.mode,status:c.conn.status,states:c.messages.map(function(m){return m.meta.state;})})",
        tmp_path,
    )
    assert out == {"mode": "ai", "status": "idle", "states": ["created", "resolved"]}


def test_apply_error_sets_lasterror(tmp_path):
    out = _run_js(
        "UserPage.applyEvent(UserPage.createInitialStore(),"
        "{type:'error',data:{code:'E_TIMEOUT',message:'超时'}})",
        tmp_path,
    )
    assert out["conn"]["lastError"] == {"code": "E_TIMEOUT", "message": "超时"}
    assert out["messages"][-1]["kind"] == "error"


def test_apply_event_immutable(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'handoff_created',data:{}});"
        "({before:a.conversation.mode,after:b.conversation.mode,msgs:b.messages.length})",
        tmp_path,
    )
    assert out == {"before": "ai", "after": "human", "msgs": 1}


def test_event_types_cover_spec(tmp_path):
    out = _run_js("UserPage.EVENT_TYPES", tmp_path)
    assert sorted(out) == sorted([
        "token", "tool_call", "tool_result", "clarification_required",
        "confirmation_required", "confirmation_updated", "approval_required",
        "approval_updated", "safety_rejected", "handoff_created",
        "handoff_resolved", "error", "done",
    ])


def test_build_view_header_status(tmp_path):
    out = _run_js("UserPage.buildView(UserPage.createInitialStore()).headerStatus", tmp_path)
    assert out == {"text": "AI 在线", "cls": "ai", "connText": "连接正常"}


def test_build_view_header_human_after_handoff(tmp_path):
    out = _run_js(
        "UserPage.buildView(UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'handoff_created',data:{}})).headerStatus",
        tmp_path,
    )
    assert out == {"text": "人工接待中", "cls": "human", "connText": "当前由人工客服接待"}


def test_build_view_input_disabled_on_streaming(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'token',data:{text:'x'}});"
        "var c=UserPage.applyEvent(b,{type:'done',data:{}});"
        "({streaming:UserPage.buildView(b).inputDisabled,idle:UserPage.buildView(c).inputDisabled})",
        tmp_path,
    )
    assert out == {"streaming": True, "idle": False}


def test_build_view_chips_and_cmds(tmp_path):
    out = _run_js(
        "UserPage.buildView(UserPage.createInitialStore()).chips.map(function(n){return n.attrs['data-cmd'];})",
        tmp_path,
    )
    assert out == ["重新开始", "查询我的订单", "修改收货地址", "我要申请售后", "转人工客服"]


def test_build_view_confirm_card_buttons(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'confirmation_required',data:{summary:'修改收货地址'}});"
        "var c=UserPage.buildView(b);"
        "({kind:c.messages[0].tag,actions:c.messages[0].children[2].children.map(function(x){return x.attrs['data-action'];})})",
        tmp_path,
    )
    assert out == {"kind": "div", "actions": ["confirm", "cancel"]}


def test_build_view_confirm_card_settled(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'confirmation_required',data:{summary:'x'}});"
        "var c=UserPage.applyEvent(b,{type:'confirmation_updated',data:{action:'confirmed'}});"
        "var v=UserPage.buildView(c);"
        "var hasButton=false;"
        "function scan(n){if(n&&n.tag==='button')hasButton=true;if(n&&n.children)(n.children||[]).forEach(scan);}"
        "scan(v.messages[0]);"
        "({hasButton:hasButton,last:v.messages[0].children[2].children[0]})",
        tmp_path,
    )
    assert out["hasButton"] is False
    assert out["last"] == "已确认"


def test_build_view_approval_no_buttons(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=UserPage.applyEvent(a,{type:'approval_required',data:{approval_id:'ap-1',summary:'取消订单',expires_at:'2026-09-17T23:59:59Z'}});"
        "var v=UserPage.buildView(b);"
        "({title:v.messages[0].children[0].children[0],status:v.messages[0].children[2].children[0]})",
        tmp_path,
    )
    assert out["title"] == "高风险操作 · 人工审批中"
    assert "审批中" in out["status"]
    assert "2026-09-17T23:59:59Z" in out["status"]


def test_build_view_full_event_stream(tmp_path):
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "var events=[{type:'token',data:{text:'您好'}},"
        "{type:'tool_call',data:{name:'order_lookup',args:{order_id:'A1'}}},"
        "{type:'tool_result',data:{name:'order_lookup',ok:true,summary:'已发货'}},"
        "{type:'clarification_required',data:{missing:['order_id']}},"
        "{type:'confirmation_required',data:{summary:'修改地址'}},"
        "{type:'safety_rejected',data:{rule:'r',reason:'超期'}},"
        "{type:'error',data:{code:'E_1',message:'超时'}},"
        "{type:'done',data:{}}];"
        "for(var i=0;i<events.length;i++){s=UserPage.applyEvent(s,events[i]);}"
        "var v=UserPage.buildView(s);"
        "({count:v.messages.length,classes:v.messages.map(function(m){return m.attrs['class'];})})",
        tmp_path,
    )
    assert out["count"] == 6
    assert out["classes"] == [
        "msg assistant", "card tool-card", "card", "card", "notice", "error-bar",
    ]


def test_mock_stream_covers_all_event_types(tmp_path):
    out = _run_js(
        "Array.from(new Set(UserPage.mockStream().map(function(e){return e.type;}))).sort()",
        tmp_path,
    )
    assert out == sorted([
        "token", "tool_call", "tool_result", "clarification_required",
        "confirmation_required", "confirmation_updated", "approval_required",
        "approval_updated", "safety_rejected", "handoff_created",
        "handoff_resolved", "error", "done",
    ])


def test_mock_api_login_returns_token(tmp_path):
    out = _run_js("(await UserPage.createMockApi().login('u1', 'p1')).token", tmp_path)
    assert out == "mock-token"


def test_api_login_posts_credentials(tmp_path):
    expr = (
        "var sent=null;"
        "var fake={ok:true,status:200,json:async function(){return {token:'t'};}};"
        "var api=UserPage.createApi({fetchImpl:async function(url,opts){sent={url:url,opts:opts};return fake;},baseUrl:'http://x'});"
        "var r=await api.login('a','b');"
        "({token:r.token,url:sent.url,body:sent.opts.body})"
    )
    out = _run_js(expr, tmp_path)
    assert out["token"] == "t"
    assert out["url"] == "http://x/auth/login"
    assert json.loads(out["body"]) == {"account": "a", "password": "b"}


def test_api_send_message_parses_sse(tmp_path):
    expr = (
        "var enc=new TextEncoder();"
        "var stream=new ReadableStream({start:function(c){"
        "c.enqueue(enc.encode('data: {\"type\":\"token\",\"data\":{\"text\":\"你\"}}\\n\\n'));"
        "c.enqueue(enc.encode('data: {\"type\":\"done\",\"data\":{}}\\n\\n'));"
        "c.close();}});"
        "var fake={ok:true,status:200,body:stream,json:async function(){return {};}};"
        "var api=UserPage.createApi({fetchImpl:async function(){return fake;},baseUrl:'http://x'});"
        "var got=[];"
        "await api.sendMessage('c1','hi',function(e){got.push(e.type);});"
        "got"
    )
    out = _run_js(expr, tmp_path)
    assert out == ["token", "done"]


def test_api_create_session_throws_on_401(tmp_path):
    expr = (
        "var api=UserPage.createApi({fetchImpl:async function(){"
        "return {ok:false,status:401,json:async function(){return {};}};},baseUrl:'http://x'});"
        "var err=null;try{await api.createSession();}catch(e){err=e.message;}"
        "err"
    )
    out = _run_js(expr, tmp_path)
    assert out == "SESSION_FAILED 401"


def test_next_poll_delay_exponential_cap(tmp_path):
    out = _run_js(
        "[0,1,2,3,4,5,20].map(function(a){return UserPage.nextPollDelay(a);})",
        tmp_path,
    )
    assert out == [1000, 2000, 4000, 8000, 15000, 15000, 15000]


def test_apply_poll_result_appends_seat_message(tmp_path):
    out = _run_js(
        "var a=UserPage.applyEvent(UserPage.createInitialStore(),{type:'handoff_created',data:{}});"
        "var b=UserPage.applyPollResult(a,{messages:[{id:'s1',role:'assistant',text:'人工客服：您好'}],mode:'human'});"
        "({mode:b.conversation.mode,status:b.conn.status,last:b.messages[b.messages.length-1]})",
        tmp_path,
    )
    assert out["mode"] == "human"
    assert out["status"] == "polling"
    assert out["last"]["role"] == "assistant"
    assert out["last"]["meta"]["seat"] is True


def test_apply_poll_result_resolves_handoff(tmp_path):
    out = _run_js(
        "var a=UserPage.applyEvent(UserPage.createInitialStore(),{type:'handoff_created',data:{}});"
        "var b=UserPage.applyPollResult(a,{messages:[],mode:'ai'});"
        "({mode:b.conversation.mode,status:b.conn.status,last:b.messages[b.messages.length-1]})",
        tmp_path,
    )
    assert out["mode"] == "ai"
    assert out["status"] == "idle"
    assert out["last"]["meta"]["state"] == "resolved"


def test_mark_reconnecting(tmp_path):
    out = _run_js(
        "var s=UserPage.markReconnecting(UserPage.createInitialStore(),'连接中断');"
        "({status:s.conn.status,err:s.conn.lastError})",
        tmp_path,
    )
    assert out["status"] == "reconnecting"
    assert out["err"]["code"] == "E_RECONNECT"


def test_can_retry_false_after_logout(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "var b=JSON.parse(JSON.stringify(a));b.auth={token:'t',user:{name:'u'}};"
        "({none:UserPage.canRetry(a),authed:UserPage.canRetry(b)})",
        tmp_path,
    )
    assert out == {"none": False, "authed": True}
