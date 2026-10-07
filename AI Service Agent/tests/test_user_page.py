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


def test_mount_node_tolerates_childless_elements(html):
    assert "var children = node.children || [];" in html
    assert "children.forEach(function (child)" in html


def test_has_two_views_and_login_fields(html):
    for marker in (
        'id="view-login"', 'id="view-chat"',
        'id="login-account"', 'id="login-password"',
        'id="btn-login"', 'id="btn-logout"',
        'id="msg-input"', 'id="btn-send"',
        'id="msg-list"', 'id="status-tag"', 'id="chips"',
    ):
        assert marker in html, f"缺少 {marker}"
    assert '<label for="login-account">手机号</label>' in html
    assert '演示账号：客户手机号，默认密码 123456' in html


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
    assert out["conn"]["status"] == "idle"
    assert out["profile"] is None
    assert out["pendingAction"] is None


def test_initial_store_includes_order_options_state(tmp_path):
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "({orders:s.orders,status:s.ordersStatus})",
        tmp_path,
    )
    assert out["orders"] == []
    assert out["status"] == "idle"


def test_mask_phone_hides_middle_digits(tmp_path):
    out = _run_js("UserPage.maskPhone('+8492722552')", tmp_path)
    assert out == "+84****2552"


def test_resolve_form_type_maps_p0_missing_fields(tmp_path):
    out = _run_js(
        "({phone:UserPage.resolveFormType(['phone']),"
        "address:UserPage.resolveFormType(['address']),"
        "address_with_order:UserPage.resolveFormType(['order_id','address']),"
        "unknown:UserPage.resolveFormType(['order_id'])})",
        tmp_path,
    )
    assert out == {
        "phone": "update_phone",
        "address": "update_address",
        "address_with_order": "update_address",
        "unknown": None,
    }


def test_build_phone_form_content(tmp_path):
    out = _run_js(
        "UserPage.buildFormContent(['phone'], "
        "{user_id:'u0001',phone:'+8492722552',address:'No. 1, Chengyuan VN'})",
        tmp_path,
    )
    assert out["form_type"] == "update_phone"
    assert out["title"] == "修改联系电话"
    assert out["old_info"] == {"当前手机号": "+84****2552"}
    assert out["fields"] == [
        {"name": "phone", "label": "新手机号", "type": "tel", "required": True}
    ]
    assert out["submit_label"] == "确认修改"


def test_build_address_form_content_requires_order_id_when_missing(tmp_path):
    out = _run_js(
        "UserPage.buildFormContent(['order_id','address'], "
        "{user_id:'u0001',phone:'+8492722552',address:'No. 1, Chengyuan VN'})",
        tmp_path,
    )
    assert out["form_type"] == "update_address"
    assert out["old_info"] == {"当前收货地址": "No. 1, Chengyuan VN"}
    assert [field["name"] for field in out["fields"]] == ["order_id", "address"]


def test_build_address_form_content_omits_order_id_when_not_missing(tmp_path):
    out = _run_js(
        "UserPage.buildFormContent(['address'], "
        "{user_id:'u0001',phone:'+8492722552',address:'No. 1, Chengyuan VN'})",
        tmp_path,
    )
    assert [field["name"] for field in out["fields"]] == ["address"]


def test_build_form_content_falls_back_to_none(tmp_path):
    out = _run_js("UserPage.buildFormContent(['order_id'], null)", tmp_path)
    assert out is None


def test_apply_clarification_creates_phone_form(tmp_path):
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "s.profile={user_id:'u0001',phone:'+8492722552',address:'No. 1, Chengyuan VN'};"
        "s=UserPage.applyEvent(s,{type:'clarification_required',data:{missing:['phone']}});"
        "s.messages[0]",
        tmp_path,
    )
    assert out["kind"] == "form"
    assert out["content"]["form_type"] == "update_phone"
    assert out["meta"] == {"status": "pending"}


def test_apply_clarification_keeps_text_card_for_unknown_fields(tmp_path):
    out = _run_js(
        "UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'clarification_required',data:{missing:['order_id']}})",
        tmp_path,
    )
    assert out["messages"][0]["kind"] == "clarify"


def test_validate_phone_form(tmp_path):
    out = _run_js(
        "var form={form_type:'update_phone',fields:[{name:'phone',label:'新手机号',required:true}]};"
        "({empty:UserPage.validateFormValues(form,{phone:''},null),"
        "invalid:UserPage.validateFormValues(form,{phone:'123'},null),"
        "same:UserPage.validateFormValues(form,{phone:'+8492722552'},"
        "{phone:'+8492722552'}),"
        "ok:UserPage.validateFormValues(form,{phone:'+8413800138000'},"
        "{phone:'+8492722552'})})",
        tmp_path,
    )
    assert out["empty"] == "请填写新手机号"
    assert out["invalid"] == "请输入有效的新手机号"
    assert out["same"] == "新手机号不能与当前手机号相同"
    assert out["ok"] == ""


def test_validate_address_form(tmp_path):
    out = _run_js(
        "var form={form_type:'update_address',fields:["
        "{name:'order_id',label:'订单号',required:true},"
        "{name:'address',label:'新收货地址',required:true}]};"
        "({missing_order:UserPage.validateFormValues(form,{order_id:'',address:'New address'},null),"
        "same:UserPage.validateFormValues(form,{order_id:'ORD-1',address:'No. 1, Chengyuan VN'},"
        "{address:'No. 1, Chengyuan VN'}),"
        "ok:UserPage.validateFormValues(form,{order_id:'ORD-1',address:'New address'},"
        "{address:'No. 1, Chengyuan VN'})})",
        tmp_path,
    )
    assert out["missing_order"] == "请填写订单号"
    assert out["same"] == "新收货地址不能与当前收货地址相同"
    assert out["ok"] == ""


def test_format_form_submissions(tmp_path):
    out = _run_js(
        "({phone:UserPage.formatFormSubmission('update_phone',{phone:'+8413800138000'}),"
        "address:UserPage.formatFormSubmission('update_address',"
        "{order_id:'ORD-1',address:'New address'})})",
        tmp_path,
    )
    assert out["phone"] == "[表单提交] 修改联系电话\n新手机号：+8413800138000"
    assert out["address"] == "[表单提交] 修改收货地址\n订单号：ORD-1\n新地址：New address"


def test_build_view_renders_form_card_without_second_entry(tmp_path):
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "s.profile={user_id:'u0001',phone:'+8492722552',address:'No. 1, Chengyuan VN'};"
        "s=UserPage.applyEvent(s,{type:'clarification_required',data:{missing:['phone']}});"
        "var v=UserPage.buildView(s);"
        "({class:v.messages[0].attrs.class,text:JSON.stringify(v.messages[0])})",
        tmp_path,
    )
    assert out["class"] == "card form-card"
    assert "修改联系电话" in out["text"]
    assert "+84****2552" in out["text"]
    assert "新手机号" in out["text"]
    assert "确认修改" in out["text"]


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


def test_apply_done_final_appends_text_when_no_token(tmp_path):
    """done.final 在无 token 流（KB 缺口整段回复）时补齐 assistant text（event-spec done.final 契约）。"""
    out = _run_js(
        "UserPage.applyEvent(UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'tool_call',data:{name:'kb_search',args:{text:'注册资本'}}}), "
        "{type:'done',data:{final:'我暂时无法处理，请换个说法或联系人工客服。'}})",
        tmp_path,
    )
    last = out["messages"][-1]
    assert last["kind"] == "text"
    assert last["role"] == "assistant"
    assert "联系人工客服" in last["content"]
    assert out["conn"]["status"] == "idle"


def test_apply_done_final_skips_text_for_pending_form(tmp_path):
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "s.profile={user_id:'u0001',phone:'+8492722552',address:'No. 1, Chengyuan VN'};"
        "s=UserPage.applyEvent(s,{type:'clarification_required',data:{missing:['phone']}});"
        "s=UserPage.applyEvent(s,{type:'done',data:{final:'请提供手机号'}});"
        "({kinds:s.messages.map(function(m){return m.kind;}),status:s.messages[0].meta.status})",
        tmp_path,
    )
    assert out["kinds"] == ["form"]
    assert out["status"] == "pending"


def test_apply_done_final_idempotent_with_token(tmp_path):
    """已有 token 流时，即使 tool 事件插在中间，done.final 也不重复追加。"""
    out = _run_js(
        "var s=UserPage.createInitialStore();"
        "s=UserPage.applyEvent(s,{type:'token',data:{text:'您好'}});"
        "s=UserPage.applyEvent(s,{type:'tool_call',data:{name:'kb_search',args:{}}});"
        "s=UserPage.applyEvent(s,{type:'tool_result',data:{name:'kb_search',ok:true,summary:'ok'}});"
        "s=UserPage.applyEvent(s,{type:'done',data:{final:'您好'}})",
        tmp_path,
    )
    texts = [m for m in out["messages"] if m["kind"] == "text" and m["role"] == "assistant"]
    assert len(texts) == 1
    assert texts[0]["content"] == "您好"


def test_apply_tool_events_are_not_user_visible(tmp_path):
    """工具执行是内部过程事件，用户页面不展示工具调用信息。"""
    out = _run_js(
        "(function(){var s=UserPage.applyEvent(UserPage.applyEvent(UserPage.createInitialStore(), "
        "{type:'tool_call',data:{name:'order_lookup',args:{order_id:'A1001'}}}), "
        "{type:'tool_result',data:{name:'order_lookup',ok:true,summary:'已发货'}});"
        "return s.messages;})()",
        tmp_path,
    )
    assert out == []


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
    assert out == [
        "重新开始", "查询我的订单", "我想咨询食谱", "如何更新固件",
        "如何配对 Chengyuan Home App", "我要申请售后", "转人工客服",
    ]


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
    assert out["count"] == 5
    assert out["classes"] == [
        "msg assistant", "card", "card", "notice", "error-bar",
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
    assert json.loads(out["body"]) == {"phone": "a", "password": "b"}


def test_api_login_attaches_token_to_next_request(tmp_path):
    expr = (
        "var calls=[];"
        "var api=UserPage.createApi({"
        "fetchImpl:async function(url,opts){calls.push({url:url,opts:opts});"
        "if(url.endsWith('/auth/login'))return {ok:true,status:200,json:async function(){return {token:'t'};}};"
        "return {ok:true,status:200,json:async function(){return {};}};"
        "},baseUrl:'http://x'});"
        "var auth=await api.login('a','b');"
        "await api.createSession({id:'alice',name:'Alice'});"
        "({token:auth.token,nextUrl:calls[1].url,authorization:calls[1].opts.headers['Authorization']})"
    )
    out = _run_js(expr, tmp_path)
    assert out == {
        "token": "t",
        "nextUrl": "http://x/sessions",
        "authorization": "Bearer t",
    }


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


def test_reset_conversation_clears_session_state(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "a.auth={token:'t',user:{name:'u'}};"
        "a=UserPage.applyEvent(a,{type:'token',data:{text:'x'}});"
        "a.pendingAction={kind:'confirm',status:'pending',summary:'x'};"
        "a.conversation={conversation_id:'c1',mode:'human'};"
        "a.conn={status:'reconnecting',lastError:{code:'E'}};"
        "var b=UserPage.resetConversation(a);"
        "({auth:b.auth,conversation:b.conversation,messages:b.messages,conn:b.conn,pendingAction:b.pendingAction})",
        tmp_path,
    )
    assert out["auth"] == {"token": "t", "user": {"name": "u"}}
    assert out["conversation"] == {"conversation_id": None, "mode": "ai"}
    assert out["messages"] == []
    assert out["conn"] == {"status": "idle", "lastError": None}
    assert out["pendingAction"] is None


def test_bootstrap_session_creates_or_reuses_conversation(tmp_path):
    out = _run_js(
        "var calls=0;"
        "var api={createSession:async function(){calls++;return {conversation_id:'c9',mode:'ai'};}};"
        "var r1=await UserPage.bootstrapSession(UserPage.createInitialStore(),api,null);"
        "var r2=await UserPage.bootstrapSession(UserPage.createInitialStore(),api,'c-old');"
        "({calls:calls,c1:r1.store.conversation.conversation_id,created1:r1.created,"
        "c2:r2.store.conversation.conversation_id,created2:r2.created})",
        tmp_path,
    )
    assert out == {
        "calls": 1, "c1": "c9", "created1": True,
        "c2": "c-old", "created2": False,
    }


def test_api_401_error_normalized(tmp_path):
    out = _run_js(
        "var api=UserPage.createApi({fetchImpl:async function(){"
        "return {ok:false,status:401,json:async function(){return {};}};},baseUrl:'http://x'});"
        "var err=null;"
        "try{await api.getMessages('c1');}catch(e){err={message:e.message,status:e.status,code:e.code};}"
        "err",
        tmp_path,
    )
    assert out == {"message": "MESSAGES_FAILED 401", "status": 401, "code": "AUTH_EXPIRED"}


def test_mark_auth_expired_clears_session(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "a.auth={token:'t',user:{name:'u'}};"
        "a.conversation={conversation_id:'c1',mode:'human'};"
        "a.messages=[{id:'m1',role:'user',kind:'text',content:'hi',meta:{}}];"
        "var b=UserPage.markAuthExpired(a);"
        "({expired:b.auth.expired,token:b.auth.token,conversation:b.conversation,"
        "messages:b.messages,retry:UserPage.canRetry(b)})",
        tmp_path,
    )
    assert out["expired"] is True
    assert out["token"] is None
    assert out["conversation"] == {"conversation_id": None, "mode": "ai"}
    assert out["messages"] == []
    assert out["retry"] is False


def test_can_retry_false_after_expired(tmp_path):
    out = _run_js(
        "var a=UserPage.createInitialStore();"
        "a.auth={token:'t',user:{name:'u'}};"
        "({ok:UserPage.canRetry(a),after:UserPage.canRetry(UserPage.markAuthExpired(a))})",
        tmp_path,
    )
    assert out == {"ok": True, "after": False}


def test_apply_poll_result_dedupes_by_id(tmp_path):
    out = _run_js(
        "var a=UserPage.applyEvent(UserPage.createInitialStore(),{type:'handoff_created',data:{}});"
        "var b=UserPage.applyPollResult(a,{messages:[{id:'s1',role:'assistant',text:'A'}],mode:'human'});"
        "var c=UserPage.applyPollResult(b,{messages:[{id:'s1',role:'assistant',text:'A'}],mode:'human'});"
        "({count:c.messages.filter(function(m){return m.id==='s1';}).length,total:c.messages.length})",
        tmp_path,
    )
    assert out == {"count": 1, "total": 2}


def test_merge_history_dedupes_and_appends_legacy(tmp_path):
    out = _run_js(
        "var r=UserPage.mergeHistory("
        "[{id:'m1',role:'user',kind:'text',content:'hi',meta:{}}],"
        "[{id:'m1',role:'assistant',text:'dup'},{role:'assistant',text:'legacy'}]);"
        "({length:r.length,ids:r.map(function(m){return m.id;}),contents:r.map(function(m){return m.content;})})",
        tmp_path,
    )
    assert out["length"] == 2
    assert out["ids"][0] == "m1"
    assert out["ids"][1].startswith("m-")
    assert out["contents"] == ["hi", "legacy"]


def test_merge_history_hides_ui_generated_form_submissions(tmp_path):
    out = _run_js(
        "var r=UserPage.mergeHistory([], ["
        "{id:'m1',role:'user',text:'[表单提交] 修改联系电话\\n新手机号：+8413800138000'},"
        "{id:'m2',role:'user',text:'[表单提交] 修改收货地址\\n订单号：ORD-1\\n新地址：New address'},"
        "{id:'m3',role:'user',text:'我要修改电话号码'}"
        "]);"
        "({contents:r.map(function(m){return m.content;})})",
        tmp_path,
    )
    assert out["contents"] == ["我要修改电话号码"]


def test_build_view_address_form_buttons(tmp_path):
    out = _run_js(
        "var a=UserPage.applyEvent(UserPage.createInitialStore(),"
        "{type:'clarification_required',data:{missing:['order_id','address']}});"
        "var v=UserPage.buildView(a);"
        "var fields=[];"
        "function scan(n){if(n&&n.tag==='button')fields.push(n.attrs);"
        "if(n&&n.tag==='input')fields.push(n.attrs);"
        "if(n&&n.children)(n.children||[]).forEach(scan);}"
        "scan(v.messages[0]);"
        "({class:v.messages[0].attrs.class,fields:fields,text:JSON.stringify(v.messages[0])})",
        tmp_path,
    )
    assert out["class"] == "card form-card"
    assert out["text"].count("data-form-field") == 2
    assert "订单号" in out["text"]
    assert "新收货地址" in out["text"]
    assert "确认修改" in out["text"]
    assert any(field.get("data-action") == "form-submit" for field in out["fields"])


def test_api_clarify_placeholder_both_layers(tmp_path):
    out = _run_js(
        "var sent=null;"
        "var api=UserPage.createApi({fetchImpl:async function(url,opts){sent={url:url,body:opts.body};"
        "return {ok:true,status:200,json:async function(){return {};}};},baseUrl:'http://x'});"
        "await api.clarify('c1',['order_id']);"
        "var m=await UserPage.createMockApi().clarify('c1',['order_id']);"
        "({url:sent.url,body:sent.body,mockOk:m.ok,"
        "real:typeof api.clarify==='function',mockFn:typeof UserPage.createMockApi().clarify==='function'})",
        tmp_path,
    )
    assert out["url"] == "http://x/actions/clarify"
    assert json.loads(out["body"]) == {"conversation_id": "c1", "fields": ["order_id"]}
    assert out["mockOk"] is True
    assert out["real"] is True
    assert out["mockFn"] is True


def test_api_get_orders_requests_editable_orders(tmp_path):
    out = _run_js(
        "var sent=null;"
        "var api=UserPage.createApi({fetchImpl:async function(url,opts){sent={url:url,opts:opts};"
        "return {ok:true,status:200,json:async function(){return {orders:[],count:0};}};},"
        "baseUrl:'http://x'});"
        "var result=await api.getOrders();"
        "({url:sent.url,result:result})",
        tmp_path,
    )
    assert out["url"] == "http://x/me/orders?address_editable=true"
    assert out["result"] == {"orders": [], "count": 0}


def test_mock_api_get_orders_returns_options(tmp_path):
    out = _run_js(
        "var api=await UserPage.createMockApi();"
        "var result=await api.getOrders();"
        "({count:result.count,first:result.orders[0]})",
        tmp_path,
    )
    assert out["count"] == 1
    assert out["first"]["order_id"] == "ORD-PAID-1"
    assert out["first"]["status_display"] == "已付款"


def test_mock_get_messages_empty_before_handoff(tmp_path):
    """P1: mock getMessages 在未发生转人工前必须为空 + mode=ai（登录/刷新不得预置坐席回复）。"""
    out = _run_js(
        "var api=UserPage.createMockApi({delayMs:0});"
        "var before=await api.getMessages('c1');"
        "({count:before.messages.length,mode:before.mode})",
        tmp_path,
    )
    assert out == {"count": 0, "mode": "ai"}


def test_mock_get_messages_seat_after_handoff_then_resolved(tmp_path):
    """P1: 流内发出 handoff_created 后，第一次轮询返回坐席回复+human，第二次返回空+ai（人工已解决）。"""
    out = _run_js(
        "var api=UserPage.createMockApi({delayMs:0});"
        "await api.sendMessage('c1','你好',function(){});"
        "var first=await api.getMessages('c1');"
        "var second=await api.getMessages('c1');"
        "({firstCount:first.messages.length,firstText:first.messages.length?first.messages[0].text:'',"
        "firstMode:first.mode,secondCount:second.messages.length,secondMode:second.mode})",
        tmp_path,
    )
    assert out["firstCount"] == 1
    assert "人工客服" in out["firstText"]
    assert out["firstMode"] == "human"
    assert out["secondCount"] == 0
    assert out["secondMode"] == "ai"


def test_mock_send_message_events_spaced_by_delay(tmp_path):
    """P2: mock sendMessage 逐条推送事件并带延迟，streaming 状态在浏览器中可观察。"""
    out = _run_js(
        "var api=UserPage.createMockApi({delayMs:40});"
        "var times=[];var t0=Date.now();"
        "await api.sendMessage('c1','x',function(){times.push(Date.now()-t0);});"
        "({n:times.length,gap:times[1]-times[0]})",
        tmp_path,
    )
    assert out["n"] == 12  # mockStream 13 种事件去掉 handoff_resolved
    assert out["gap"] >= 25

def test_api_create_session_sends_user_id(tmp_path):
    out = _run_js(
        "var sent=null;"
        "var fake={ok:true,status:200,json:async function(){return {session_id:'s1',mode:'ai'};}};"
        "var api=UserPage.createApi({fetchImpl:async function(url,opts){sent={url:url,body:opts.body};return fake;},baseUrl:'http://x'});"
        "var r=await api.createSession({id:'alice',name:'Alice'});"
        "({sid:r.session_id,url:sent.url,body:sent.body})",
        tmp_path,
    )
    assert out["sid"] == "s1"
    assert out["url"] == "http://x/sessions"
    assert json.loads(out["body"]) == {"user_id": "alice"}


def test_api_send_message_uses_session_id_message(tmp_path):
    out = _run_js(
        "var sent=null;"
        "var stream=new ReadableStream({start:function(c){c.close();}});"
        "var fake={ok:true,status:200,body:stream,json:async function(){return {};}};"
        "var api=UserPage.createApi({fetchImpl:async function(url,opts){sent={url:url,body:opts.body};return fake;},baseUrl:'http://x'});"
        "await api.sendMessage('c1','你好',function(){});"
        "({url:sent.url,body:sent.body})",
        tmp_path,
    )
    assert out["url"] == "http://x/chat"
    assert json.loads(out["body"]) == {"session_id": "c1", "message": "你好"}


def test_api_get_messages_normalizes_items_and_mode(tmp_path):
    out = _run_js(
        "var fake={ok:true,status:200,json:async function(){return {items:[{id:1,role:'user',content:'在吗'}],mode:'human'};}};"
        "var api=UserPage.createApi({fetchImpl:async function(url){return fake;},baseUrl:'http://x'});"
        "var d=await api.getMessages('c1');"
        "({n:d.messages.length,mode:d.mode,content:d.messages[0].content})",
        tmp_path,
    )
    assert out == {"n": 1, "mode": "human", "content": "在吗"}
