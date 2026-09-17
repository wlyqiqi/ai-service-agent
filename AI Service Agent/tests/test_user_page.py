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


def _run_js(expr: str, tmp_path: Path):
    """在 Node vm 沙箱中执行 expr（作用域内 UserPage = window.UserPage），返回 JSON 解析结果。"""
    if shutil.which("node") is None:
        pytest.skip("node 不可用，跳过前端逻辑测试（需要 Node >= 18）")
    script = _extract_script(HTML_PATH.read_text(encoding="utf-8"))
    (tmp_path / "site.js").write_text(script, encoding="utf-8")
    (tmp_path / "expr.js").write_text(expr, encoding="utf-8")
    runner = tmp_path / "runner.js"
    runner.write_text(
        "const fs=require('fs');const vm=require('vm');\n"
        "const sandbox={window:{},console};\n"
        "vm.runInNewContext(fs.readFileSync(__dirname+'/site.js','utf8'),sandbox);\n"
        "const {UserPage}=sandbox.window;\n"
        "const expr=fs.readFileSync(__dirname+'/expr.js','utf8');\n"
        "console.log(JSON.stringify(eval(expr)));\n",
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
