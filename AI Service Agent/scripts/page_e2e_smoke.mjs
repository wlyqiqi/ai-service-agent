#!/usr/bin/env node
/**
 * Stage 3: 用户页面真机 E2E（Chrome headless + CDP，契约对齐后）
 *
 * 前置：
 *  - Runtime 运行中（ai-service-agent，0.0.0.0:8000，真实 ARK + Chatwoot；
 *    页面由 Runtime 同源托管，静态挂载于 "/"；file:// 会强制 mock，勿用）
 *  - Chrome 可执行（CHROME_PATH 可覆盖）
 *  - .env 含 CHATWOOT_*（坐席回复/ resolve 需直连 Chatwoot 与 Runtime API）
 *
 * 用法：
 *  node scripts/page_e2e_smoke.mjs
 *  （env: RUNTIME_URL / CDP_PORT / CHROME_PATH / E2E_TIMEOUT）
 *
 * 链路：登录(占位) -> AI 对话 -> 转人工 -> 人工车道消息 -> 坐席回复(webhook->页面轮询)
 *       -> resolve -> AI 恢复；全程捕获 console/异常/网络错误。
 */
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const RUNTIME = process.env.RUNTIME_URL || "http://127.0.0.1:8000";
// 随机调试端口：避免上一次残留 Chrome 仍占 9222 时，驱动连到旧实例（旧页面停在上一轮状态）。
const CDP_PORT = process.env.CDP_PORT
  ? Number(process.env.CDP_PORT)
  : 9222 + Math.floor(Math.random() * 800);
const CHROME =
  process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const TIMEOUT = Number(process.env.E2E_TIMEOUT || 120000);
const AGENT_REPLY = "好的，已为您登记，人工客服正在跟进。";

function loadEnv() {
  const p = join(process.cwd(), ".env");
  if (!existsSync(p)) return {};
  const out = {};
  for (const line of readFileSync(p, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (m) out[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
  return out;
}
const ENV = loadEnv();

const results = [];
const consoleErrors = [];
const sseChatBodies = [];
const sessionBodies = [];

/* ---------------- CDP 客户端（Node 内置 WebSocket） ---------------- */
class Cdp {
  constructor(url) {
    this.url = url;
    this.id = 0;
    this.pending = new Map();
    this.handlers = new Map();
    this.ready = this._connect();
  }
  async _connect() {
    this.ws = new WebSocket(this.url);
    await new Promise((res, rej) => {
      this.ws.onopen = res;
      this.ws.onerror = () => rej(new Error("ws connect failed"));
    });
    this.ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id) {
        const p = this.pending.get(msg.id);
        if (!p) return;
        this.pending.delete(msg.id);
        msg.error ? p.rej(new Error(msg.error.message)) : p.res(msg.result);
      } else if (msg.method) {
        const hs = this.handlers.get(msg.method);
        if (hs) for (const h of hs) h(msg.params);
      }
    };
  }
  async send(method, params = {}) {
    await this.ready;
    const id = ++this.id;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  on(method, h) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(h);
  }
  close() {
    try {
      this.ws && this.ws.close();
    } catch {}
  }
}

async function evaluate(cdp, expression) {
  const r = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (r.exceptionDetails) {
    throw new Error("page eval error: " + JSON.stringify(r.exceptionDetails));
  }
  return r.result.value;
}

async function waitFor(cdp, expression, label, timeout = TIMEOUT) {
  const deadline = Date.now() + timeout;
  let last = null;
  while (Date.now() < deadline) {
    try {
      last = await evaluate(cdp, expression);
      if (last) return last;
    } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`timeout waiting for ${label} (last=${JSON.stringify(last)})`);
}

async function sendMessage(cdp, text) {
  await waitFor(
    cdp,
    `!document.getElementById('msg-input').disabled`,
    "input enabled"
  );
  await evaluate(
    cdp,
    `document.getElementById('msg-input').value = ${JSON.stringify(text)}; true`
  );
  await evaluate(cdp, `document.getElementById('btn-send').click(); true`);
}

async function waitText(cdp, selector, needle, label, timeout = TIMEOUT) {
  return waitFor(
    cdp,
    `(() => { const t = Array.from(document.querySelectorAll(${JSON.stringify(
      selector
    )})).map(e => e.textContent).join(' | '); return t.includes(${JSON.stringify(
      needle
    )}) ? t : false; })()`,
    label,
    timeout
  );
}

/* ---------------- Chatwoot / Runtime 直连（坐席侧动作） ---------------- */
async function cwSendAgentReply(convPrefix) {
  if (!ENV.CHATWOOT_BASE_URL || !ENV.CHATWOOT_ACCOUNT_ID || !ENV.CHATWOOT_API_TOKEN) {
    throw new Error("CHATWOOT_* env missing");
  }
  const base = ENV.CHATWOOT_BASE_URL.replace(/\/$/, "");
  const headers = { "api_access_token": ENV.CHATWOOT_API_TOKEN };
  // 找与 Runtime 会话同名的 contact 所在 inbox 会话：用 contact search 太重，
  // 直接列 inbox 最近会话（取最新一个 contact 名匹配 user- 前缀且最近创建的）
  const ib = await (
    await fetch(`${base}/api/v1/accounts/${ENV.CHATWOOT_ACCOUNT_ID}/inboxes`, {
      headers,
    })
  ).json();
  const inboxId = ENV.CHATWOOT_INBOX_ID
    ? Number(ENV.CHATWOOT_INBOX_ID)
    : (ib.payload || []).find((i) => i.name && i.name.includes("澄元"))?.id;
  const list = await (
    await fetch(
      `${base}/api/v1/accounts/${ENV.CHATWOOT_ACCOUNT_ID}/conversations?inbox_id=${inboxId}&status=open&page=1`,
      { headers }
    )
  ).json();
  const nested = (list.data && list.data.payload) || list.payload || [];
  const convs = Array.isArray(nested) ? nested : [];
  // 精确匹配 contact 名 user-<session前8位>（本 E2E 刚建的会话；旧冒烟残留 open 会话不干扰）
  const target = convs.find((c) => {
    const n = (c.meta && c.meta.sender && c.meta.sender.name) || (c.sender && c.sender.name) || "";
    return n === `user-${convPrefix}` || n.startsWith(`user-${convPrefix}`);
  });
  if (!target) throw new Error(`no chatwoot conversation for user-${convPrefix}`);
  const cwId = target.id;
  const r = await fetch(
    `${base}/api/v1/accounts/${ENV.CHATWOOT_ACCOUNT_ID}/conversations/${cwId}/messages`,
    {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ content: AGENT_REPLY, message_type: "outgoing" }),
    }
  );
  return { ok: r.ok, status: r.status, cwId };
}

function sessionIdFromBodies() {
  for (const b of sessionBodies) {
    try {
      const d = JSON.parse(b);
      if (d && d.session_id) return d.session_id;
    } catch {}
  }
  return null;
}

async function fetchHandoffId(sessId) {
  const r = await fetch(
    `${RUNTIME}/handoffs?conversation_id=${encodeURIComponent(sessId)}`
  );
  if (r.status !== 200) return null;
  const d = await r.json();
  return d.handoff_id || null;
}

function handoffIdFromBodies() {
  for (const b of sseChatBodies) {
    for (const line of b.body.split(/\r?\n/)) {
      if (!line.startsWith("data: ")) continue;
      try {
        const ev = JSON.parse(line.slice(6));
        if (ev.type === "handoff_created" && ev.data && ev.data.handoff_id) {
          return ev.data.handoff_id;
        }
      } catch {}
    }
  }
  return null;
}

/* ---------------- main ---------------- */
async function main() {
  const profile = mkdtempSync(join(tmpdir(), "page-e2e-"));
  let chrome;
  let cdp;
  try {
    chrome = spawn(
      CHROME,
      [
        "--headless=new",
        `--remote-debugging-port=${CDP_PORT}`,
        `--user-data-dir=${profile}`,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--window-size=1280,900",
        "about:blank",
      ],
      { stdio: "ignore" }
    );

    // 等 CDP 就绪 + 取 page target
    let wsUrl = null;
    for (let i = 0; i < 60 && !wsUrl; i++) {
      try {
        const res = await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`);
        const list = await res.json();
        const page = list.find(
          (t) => t.type === "page" && !String(t.url).startsWith("devtools://")
        );
        if (page) wsUrl = page.webSocketDebuggerUrl;
      } catch {}
      if (!wsUrl) await new Promise((r) => setTimeout(r, 500));
    }
    if (!wsUrl) throw new Error("CDP page target unavailable");

    cdp = new Cdp(wsUrl);
    await cdp.ready;

    cdp.on("Runtime.consoleAPICalled", (p) => {
      if (p.type === "error")
        consoleErrors.push({
          kind: "console.error",
          text: (p.args || []).map((a) => a.value ?? a.description ?? "").join(" "),
        });
    });
    cdp.on("Log.entryAdded", (p) => {
      if (p.entry && p.entry.level === "error")
        consoleErrors.push({ kind: "log.error", text: p.entry.text });
    });
    cdp.on("Runtime.exceptionThrown", (p) =>
      consoleErrors.push({
        kind: "exception",
        text: (p.exceptionDetails && p.exceptionDetails.text) || "",
      })
    );
    cdp.on("Network.loadingFailed", (p) => {
      if (!p.canceled)
        consoleErrors.push({ kind: "network", text: p.errorText || "" });
    });
    cdp.on("Network.responseReceived", async (p) => {
      const u = p.response && p.response.url;
      if (u && u.endsWith("/chat")) {
        try {
          const b = await cdp.send("Network.getResponseBody", {
            requestId: p.requestId,
          });
          sseChatBodies.push({ url: u, body: b.body || "" });
        } catch {}
      } else if (u && u.endsWith("/sessions")) {
        try {
          const b = await cdp.send("Network.getResponseBody", {
            requestId: p.requestId,
          });
          sessionBodies.push(b.body || "");
        } catch {}
      }
    });

    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Network.enable");
    await cdp.send("Log.enable");

    await cdp.send("Page.navigate", { url: RUNTIME + "/" });
    await waitFor(
      cdp,
      `document.readyState === 'complete'`,
      "page readyState complete"
    );

    // 1. 登录视图
    await waitFor(
      cdp,
      `document.getElementById('view-login') && document.getElementById('view-login').classList.contains('active')`,
      "login view active"
    );
    results.push({ name: "1.页面加载(登录视图)", pass: true });

    // 2. 登录
    await evaluate(
      cdp,
      `document.getElementById('login-account').value='demo'; document.getElementById('login-password').value='demo'; true`
    );
    await evaluate(cdp, `document.getElementById('btn-login').click(); true`);
    await waitFor(
      cdp,
      `document.getElementById('view-chat') && document.getElementById('view-chat').classList.contains('active')`,
      "chat view active"
    );
    results.push({ name: "2.登录(占位)->会话", pass: true });
    const sessId = sessionIdFromBodies();
    if (!sessId) throw new Error("session_id not captured from /sessions");

    // 3. AI 对话
    await sendMessage(cdp, "你好");
    const aiText = await waitFor(
      cdp,
      `(() => { const t = Array.from(document.querySelectorAll('#msg-list .msg.assistant')).map(e => e.textContent).join(''); return t.length > 1 ? t : false; })()`,
      "AI 回复出现"
    ).catch(() => null);
    results.push({
      name: "3.AI 对话(真实模型)",
      pass: !!aiText && aiText.length > 1,
      preview: aiText ? aiText.slice(0, 80) : null,
    });

    // 4. 转人工
    await sendMessage(cdp, "转人工客服");
    await waitFor(
      cdp,
      `document.getElementById('status-tag').textContent.trim() === '人工接待中'`,
      "human mode tag"
    );
    const hid = await fetchHandoffId(sessId);
    results.push({
      name: "4.转人工(人工接待中)",
      pass: !!hid,
      handoff_id: hid,
    });

    // 5. 人工车道消息
    await sendMessage(cdp, "在吗");
    await waitText(cdp, "#msg-list .msg.user", "在吗", "人工消息出现");
    results.push({ name: "5.人工消息上屏", pass: true });

    // 6. 坐席回复（webhook -> Message Store -> 页面轮询）
    const cw = await cwSendAgentReply(sessId.slice(0, 8));
    const agentText = await waitText(
      cdp,
      "#msg-list .msg.assistant",
      "正在跟进",
      "坐席回复经轮询上屏"
    );
    results.push({
      name: "6.坐席回复(webhook->轮询)",
      pass: !!agentText,
      cw_status: cw.status,
      preview: agentText ? agentText.slice(-60) : null,
    });

    // 7. resolve -> AI 恢复
    if (hid) {
      const rr = await fetch(`${RUNTIME}/handoffs/${hid}/resolve`, {
        method: "POST",
      });
      await waitFor(
        cdp,
        `document.getElementById('status-tag').textContent.trim() === 'AI 在线'`,
        "AI 在线 tag",
        30000
      );
      results.push({
        name: "7.resolve->AI 恢复",
        pass: rr.status === 200,
        http: rr.status,
      });
    } else {
      results.push({ name: "7.resolve->AI 恢复", pass: false, reason: "无 handoff_id" });
    }

    // 汇总
    console.log("=".repeat(72));
    console.log("Stage 3 用户页面真机 E2E 报告（Chrome headless + CDP）");
    console.log("=".repeat(72));
    for (const r of results) {
      console.log(`\n### ${r.name}: ${r.pass ? "PASS" : "FAIL"}`);
      for (const [k, v] of Object.entries(r)) {
        if (k === "name" || k === "pass") continue;
        console.log(`  ${k}: ${JSON.stringify(v)}`);
      }
    }
    const severe = consoleErrors.filter(
      (e) => e.kind !== "network" || e.text !== "net::ERR_ABORTED"
    );
    console.log("\n### 页面错误（console/log/exception/network）:");
    if (severe.length) {
      severe.forEach((e) => console.log(`  - [${e.kind}] ${e.text}`));
    } else {
      console.log("  无");
    }
    const ok = results.every((r) => r.pass);
    console.log("\n" + "=".repeat(72));
    console.log(`结论: ${ok ? "全部通过" : "存在失败"}`);
    process.exitCode = ok ? 0 : 2;
  } finally {
    try {
      cdp && cdp.close();
    } catch {}
    try {
      if (chrome && chrome.pid) {
        if (process.platform === "win32") {
          const { spawnSync } = await import("node:child_process");
          spawnSync("taskkill", ["/pid", String(chrome.pid), "/T", "/F"], {
            stdio: "ignore",
          });
        } else {
          chrome.kill("SIGKILL");
        }
      }
    } catch {}
    try {
      rmSync(profile, { recursive: true, force: true });
    } catch {}
  }
}

main().catch((err) => {
  console.error("E2E ERROR:", err.message);
  process.exitCode = 1;
});
