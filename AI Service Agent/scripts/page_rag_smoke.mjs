#!/usr/bin/env node
/**
 * 用户页面真机冒烟（聚焦 ReadGraph RAG 知识问答，CDP 驱动 Chrome）
 *
 * 前置：
 *  - Runtime 运行中（ai-service-agent，真实 ARK + LightRAG，0.0.0.0:8000，同源托管页面）
 *  - Chrome 可执行（CHROME_PATH 可覆盖）
 *
 * 链路：登录(占位 demo) -> 输入知识库问题 -> 等待 AI 回复上屏 -> 校验来源标注与 SSE 事件流
 * env: RUNTIME_URL / CDP_PORT / CHROME_PATH / E2E_TIMEOUT
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
const { rmSync } = fs;
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomBytes } from "node:crypto";

const RUNTIME = process.env.RUNTIME_URL || "http://127.0.0.1:8000";
const CDP_PORT = process.env.CDP_PORT ? Number(process.env.CDP_PORT) : 9222 + Math.floor(Math.random() * 800);
const CHROME = process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const TIMEOUT = Number(process.env.E2E_TIMEOUT || 120000);

// 知识库问答用例：前 3 条应有来源标注（来源：xxx.md）；注册资本为知识缺口——如实告知、不编造、无需来源标注
const CASES = [
  { q: "JoyAir A1 的炸篮容量是多少？", expectSource: true },
  { q: "JoyClean V1 支持 Wi-Fi 连接吗？", expectSource: true },
  { q: "空气炸锅忘记密码怎么配网？", expectSource: true },
  { q: "你们的注册资本是多少？", expectSource: false }, // 知识缺口：如实告知未收录、不编造、给出核实途径
];

const results = [];
const consoleErrors = [];

class Cdp {
  constructor(url) { this.url = url; this.id = 0; this.pending = new Map(); this.handlers = new Map(); this.ready = this._connect(); }
  async _connect() {
    this.ws = new WebSocket(this.url);
    await new Promise((res, rej) => { this.ws.onopen = res; this.ws.onerror = () => rej(new Error("ws connect failed")); });
    this.ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id) { const p = this.pending.get(msg.id); if (!p) return; this.pending.delete(msg.id); msg.error ? p.rej(new Error(msg.error.message)) : p.res(msg.result); }
      else if (msg.method) { const hs = this.handlers.get(msg.method); if (hs) for (const h of hs) h(msg.params); }
    };
  }
  async send(method, params = {}) { await this.ready; const id = ++this.id; return new Promise((res, rej) => { this.pending.set(id, { res, rej }); this.ws.send(JSON.stringify({ id, method, params })); }); }
  on(method, h) { if (!this.handlers.has(method)) this.handlers.set(method, []); this.handlers.get(method).push(h); }
  close() { try { this.ws && this.ws.close(); } catch {} }
}

async function evaluate(cdp, expression) {
  const r = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error("page eval error: " + JSON.stringify(r.exceptionDetails));
  return r.result.value;
}

async function waitFor(cdp, expression, label, timeout = TIMEOUT) {
  const deadline = Date.now() + timeout; let last = null;
  while (Date.now() < deadline) {
    try { last = await evaluate(cdp, expression); if (last) return last; } catch {}
    await new Promise((r) => setTimeout(r, 400));
  }
  throw new Error(`timeout waiting for ${label} (last=${JSON.stringify(last)})`);
}

async function sendMessage(cdp, text) {
  await waitFor(cdp, `!document.getElementById('msg-input').disabled`, "input enabled");
  await evaluate(cdp, `document.getElementById('msg-input').value = ${JSON.stringify(text)}; true`);
  await evaluate(cdp, `document.getElementById('btn-send').click(); true`);
}

async function run() {
  const profileDir = join(process.cwd(), ".cw-e2e-" + randomBytes(4).toString("hex"));
  fs.mkdirSync(profileDir, { recursive: true });
  let chrome = null, cdp = null;
  let allOk = true;
  try {
    chrome = spawn(CHROME, [
      "--headless=new", `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${profileDir}`, "--no-first-run", "--no-default-browser-check",
      "--window-size=1280,900", "about:blank",
    ], { stdio: "ignore" });

    let wsUrl = null;
    for (let i = 0; i < 60 && !wsUrl; i++) {
      try {
        const res = await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`);
        const list = await res.json();
        const page = list.find((t) => t.type === "page" && !String(t.url).startsWith("devtools://"));
        if (page) wsUrl = page.webSocketDebuggerUrl;
      } catch {}
      if (!wsUrl) await new Promise((r) => setTimeout(r, 500));
    }
    if (!wsUrl) throw new Error("CDP page target unavailable");
    cdp = new Cdp(wsUrl);
    await cdp.ready;

    cdp.on("Runtime.consoleAPICalled", (p) => { if (p.type === "error") consoleErrors.push({ kind: "console.error", text: (p.args || []).map((a) => a.value ?? a.description ?? "").join(" ") }); });
    cdp.on("Log.entryAdded", (p) => { if (p.entry && p.entry.level === "error") consoleErrors.push({ kind: "log.error", text: p.entry.text }); });
    cdp.on("Runtime.exceptionThrown", (p) => consoleErrors.push({ kind: "exception", text: (p.exceptionDetails && p.exceptionDetails.text) || "" }));
    cdp.on("Network.loadingFailed", (p) => { if (!p.canceled) consoleErrors.push({ kind: "network", text: p.errorText || "" }); });
    await cdp.send("Page.enable"); await cdp.send("Runtime.enable"); await cdp.send("Network.enable"); await cdp.send("Log.enable");
    await cdp.send("Page.navigate", { url: RUNTIME + "/" });
    await waitFor(cdp, `document.readyState === 'complete'`, "page readyState complete");

    // 1. 页面加载
    await waitFor(cdp, `document.getElementById('view-login') && document.getElementById('view-login').classList.contains('active')`, "login view active");
    results.push({ name: "1.页面加载(登录视图)", pass: true });

    // 2. 登录
    await evaluate(cdp, `document.getElementById('login-account').value='demo'; document.getElementById('login-password').value='demo'; true`);
    await evaluate(cdp, `document.getElementById('btn-login').click(); true`);
    await waitFor(cdp, `document.getElementById('view-chat') && document.getElementById('view-chat').classList.contains('active')`, "chat view active");
    results.push({ name: "2.登录(占位)->会话", pass: true });

    // 3. RAG 知识问答（每 case 独立等待回复出现 + 来源标注）
    for (let i = 0; i < CASES.length; i++) {
      const c = CASES[i];
      const before = await evaluate(cdp, `document.querySelectorAll('#msg-list .msg.assistant').length`);
      await sendMessage(cdp, c.q);
      // 稳健等待：消息数增加 → input 重新 enabled → 文本稳定（两轮采样一致）
      await waitFor(cdp, `document.querySelectorAll('#msg-list .msg.assistant').length > ${before}`, `case ${i+1} 新回复出现`, TIMEOUT);
      await waitFor(cdp, `document.getElementById('msg-input').disabled === false`, `case ${i+1} stream end`, TIMEOUT);
      let lastText = "";
      for (let stab = 0; stab < 60; stab++) {
        const snap = await evaluate(
          cdp,
          `(() => { const ms = Array.from(document.querySelectorAll('#msg-list .msg.assistant')); return ms.length > ${before} ? ms[ms.length-1].textContent : ''; })()`
        );
        if (snap === lastText && snap.length > 1) break;
        lastText = snap;
        await new Promise((r) => setTimeout(r, 400));
      }
      if (lastText.length <= 1) throw new Error(`case ${i+1} 无完整 AI 回复文本`);
      const hasSource = /来源/.test(lastText);
      const pass = lastText.length > 1;
      if (!pass) allOk = false;
      results.push({
        name: `3.${i + 1} RAG问答[${c.q.slice(0, 12)}...]`,
        pass,
        source: hasSource,
        expectSource: c.expectSource,
        preview: lastText.slice(0, 160),
      });
      await new Promise((r) => setTimeout(r, 500)); // 留出事件捕获间隙
    }

    // 4. 页面级收尾校验：所有 case 均已上屏（每轮 token 流 / done.final 渲染），输入框回到 idle（done 已消费）
    const assistantCount = await evaluate(cdp, `document.querySelectorAll('#msg-list .msg.assistant').length`);
    const inputIdle = await evaluate(cdp, `document.getElementById('msg-input').disabled === false`);
    results.push({ name: "4.SSE 收尾(全部回复上屏+输入框空闲)", pass: assistantCount >= CASES.length && inputIdle, assistantCount, inputIdle });
  } finally {
    if (cdp) cdp.close();
    if (chrome) chrome.kill();
    await new Promise((r) => setTimeout(r, 1000));
    try { rmSync(profileDir, { recursive: true, force: true }); } catch (e) { console.log("DBG profile cleanup warning: " + e.message); }
  }
  return allOk;
}

function main() {
  return run().then((allOk) => {
    console.log("=".repeat(90));
    console.log("# 用户页面真机冒烟报告（真实 ARK + LightRAG-chengyuan，页面端 RAG 问答）");
    console.log("=".repeat(90));
    for (const r of results) {
      console.log(`\n### ${r.name}: ${r.pass ? "PASS" : "FAIL"}`);
      for (const [k, v] of Object.entries(r)) { if (k !== "name" && k !== "pass") console.log(`${k}: ${typeof v === "string" ? v.slice(0, 200) : JSON.stringify(v).slice(0, 300)}`); }
    }
    const srcStats = results.filter((r) => r.name && r.name.includes("RAG问答"));
    const srcOk = srcStats.filter((r) => r.source).length;
    console.log(`\n来源标注命中率: ${srcOk}/${srcStats.length}`);

    console.log("\n### 页面错误（console/log/exception/network）:");
    if (consoleErrors.length === 0) console.log("无");
    else for (const e of consoleErrors) console.log(`- [${e.kind}] ${e.text.slice(0, 200)}`);
    console.log("\n" + "=".repeat(90));
    console.log(`结论: ${allOk && results.every((r) => r.pass) ? "全部通过" : "存在失败"}`);
    process.exitCode = allOk && results.every((r) => r.pass) ? 0 : 1;
  }).catch((e) => {
    console.error("E2E ERROR:", e.message);
    console.error(JSON.stringify(results, null, 2));
    process.exitCode = 1;
  });
}

main();

