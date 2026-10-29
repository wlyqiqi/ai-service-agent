/* 坐席端审批卡交互 —— 在会话里完成批准/拒绝，不跳确认页。
 *
 * 为什么需要它：Chatwoot 坐席端渲染消息时会把 `content` 过 DOMPurify，
 * `<script>` / `on*` 一律剥掉，所以卡片里的按钮只能是「指向 Runtime 确认页的链接」。
 * 这个脚本挂在 Dashboard layout 上（不是消息内容），把链接接管成真按钮：
 *
 *   点批准 → fetch POST /approvals/{id}/decide → 就地换成结果态
 *   点拒绝 → 就地展开理由输入 → 提交 → 就地换成结果态
 *
 * 顺带藏掉「客户侧表单确认」那条消息：它是客户在 Widget 里填表的中间态
 * （含原始 args 与 request_id），对坐席没有价值，且与审批卡重复。
 *
 * 依赖：Runtime 侧 `/approvals/{id}/decide` 已开 CORS（APPROVAL_LINK_CORS_ORIGINS）。
 * 不改 Chatwoot 镜像，纯 DOM 增强；JS 失效时链接仍可点（降级到确认页）。
 */
(function () {
  "use strict";

  var CARD_SELECTOR = 'div[style*="width:424px"]';
  var STATE_KEY = "cy-approval-state";
  var ACCENT = "#C0562B";
  var ACCENT_STRONG = "#A2461F";
  var OK = "#2F7A57";
  var BAD = "#B23A2E";
  var INK = "#2A211B";
  var INK_3 = "#8A7C6D";
  var INK_4 = "#A9998A";
  var LINE = "#E6D8C6";
  var SURFACE_2 = "#FAF6F0";
  var FONT =
    '-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif';

  // --- 本地状态：决策结果要能扛住 Vue 重渲染与页面刷新 -----------------------

  function loadStates() {
    try {
      var raw = window.localStorage.getItem(STATE_KEY);
      var parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (err) {
      return {};
    }
  }

  var states = loadStates();

  function remember(id, state) {
    states[id] = state;
    try {
      window.localStorage.setItem(STATE_KEY, JSON.stringify(states));
    } catch (err) {
      /* 隐私模式下写不进去，忽略：本次会话内仍然生效 */
    }
  }

  // --- 工具 -----------------------------------------------------------------

  function parseAction(href) {
    try {
      var url = new URL(href, window.location.href);
      var match = url.pathname.match(/\/approvals\/([^/]+)\/decide/);
      if (!match) return null;
      return {
        origin: url.origin,
        approvalId: decodeURIComponent(match[1]),
        token: url.searchParams.get("t") || "",
      };
    } catch (err) {
      return null;
    }
  }

  function nowHm() {
    var d = new Date();
    return (
      String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0")
    );
  }

  function el(tag, style, text) {
    var node = document.createElement(tag);
    if (style) node.setAttribute("style", style);
    if (text != null) node.textContent = text;
    return node;
  }

  // --- 藏掉客户侧表单确认的镜像消息 -----------------------------------------
  //
  // 客户在 Widget 里填表时，Chatwoot 会把同一条 form 消息同步到坐席视图：
  // 「您即将进行「订单取消」操作，参数：{'order_id': ..., 'request_id': ...}，请确认是否继续。」
  // 审批卡已经把这笔单讲清楚了，这条只剩噪音（还带内部 request_id）。
  //
  // 消息行是 `div.message-bubble-container[data-message-id]`（新版组件没有 .chat-bubble）。

  var ECHO_MARKERS = ["您即将进行", "请确认是否继续"];

  function hideFormEcho() {
    var rows = document.querySelectorAll(".message-bubble-container");
    for (var i = 0; i < rows.length; i += 1) {
      var row = rows[i];
      if (row.getAttribute("data-cy-echo-hidden") === "1") continue;
      var text = row.textContent || "";
      var matched = true;
      for (var j = 0; j < ECHO_MARKERS.length; j += 1) {
        if (text.indexOf(ECHO_MARKERS[j]) === -1) {
          matched = false;
          break;
        }
      }
      if (!matched) continue;
      row.style.setProperty("display", "none", "important");
      row.setAttribute("data-cy-echo-hidden", "1");
    }
  }

  // --- 终态渲染 -------------------------------------------------------------

  function renderResult(card, state) {
    var stripe = card.firstElementChild;
    var body = card.lastElementChild;
    if (!stripe || !body) return;
    var approved = state.status === "approved";
    var rejected = state.status === "rejected";
    stripe.style.background = approved ? OK : rejected ? BAD : "#C9BEB2";

    var head = el(
      "div",
      "font-size:15px;font-weight:650;color:" + (approved ? "#235C41" : rejected ? "#8A2B21" : INK_3) + ";",
      approved ? "已批准" : rejected ? "已拒绝" : "已处理"
    );
    var meta = el(
      "div",
      "margin-top:4px;font-size:11.5px;color:" + INK_4 + ";font-variant-numeric:tabular-nums",
      state.at ? "今天 " + state.at : ""
    );
    var note = el(
      "div",
      "margin-top:8px;font-size:12.5px;line-height:1.6;color:" + INK_3,
      approved
        ? "已执行，客户会立即收到结果通知。"
        : rejected
          ? "订单保持不变，客户会收到拒绝理由。"
          : "这张审批已处理。"
    );

    body.innerHTML = "";
    body.appendChild(head);
    body.appendChild(meta);
    body.appendChild(note);
    card.setAttribute("data-cy-state", state.status);
  }

  // --- 拒绝理由：就地展开 ---------------------------------------------------

  function openRejectPanel(card, ctx, reset) {
    if (card.querySelector("[data-cy-note]")) return;
    var panel = el("div", "margin-top:10px");
    panel.setAttribute("data-cy-note", "1");

    var label = el(
      "div",
      "font-size:11.5px;font-weight:600;color:" + INK_3 + ";margin-bottom:6px",
      "拒绝理由（必填，客户可见）"
    );
    var input = el("textarea");
    input.setAttribute(
      "style",
      "width:100%;box-sizing:border-box;min-height:64px;padding:9px 11px;font:inherit;font-size:12.5px;" +
        "line-height:1.6;color:" + INK + ";background:#fff;border:1px solid #DDCDBA;border-radius:9px;resize:vertical"
    );
    input.setAttribute("maxlength", "200");
    input.setAttribute("placeholder", "例如：商品已发货，需走退货流程");

    var row = el("div", "display:flex;gap:8px;margin-top:8px;align-items:center");
    var confirm = el("button", buttonStyle(BAD, "#8A2B21"), "确认拒绝");
    var cancel = el("button", buttonStyle("#fff", INK_3, "#DDCDBA"), "取消");
    var hint = el("span", "font-size:11px;color:" + INK_4, "");

    confirm.addEventListener("click", function () {
      var reason = (input.value || "").trim();
      if (!reason) {
        hint.textContent = "请填写理由";
        input.focus();
        return;
      }
      submit(card, ctx, "reject", reason, [confirm, cancel]);
    });
    cancel.addEventListener("click", function () {
      panel.remove();
      reset();
    });

    row.appendChild(confirm);
    row.appendChild(cancel);
    row.appendChild(hint);
    panel.appendChild(label);
    panel.appendChild(input);
    panel.appendChild(row);

    var actions = card.querySelector("table:last-of-type");
    (actions ? actions.parentElement : card.lastElementChild).appendChild(panel);
    input.focus();
  }

  function buttonStyle(bg, color, border) {
    return (
      "height:34px;padding:0 14px;border-radius:8px;border:1px solid " +
      (border || bg) +
      ";background:" +
      bg +
      ";color:" +
      color +
      ";font:inherit;font-size:12.5px;font-weight:650;cursor:pointer;font-family:" +
      FONT
    );
  }

  // --- 提交 -----------------------------------------------------------------

  function submit(card, ctx, action, note, buttons) {
    (buttons || []).forEach(function (b) {
      b.disabled = true;
      b.setAttribute("aria-busy", "true");
    });

    var payload = { t: ctx.token, action: action };
    if (note) payload.note = note;

    window
      .fetch(ctx.origin + "/approvals/" + encodeURIComponent(ctx.approvalId) + "/decide", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
      })
      .then(function (res) {
        return res.json().then(function (data) {
          return { ok: res.ok, status: res.status, data: data || {} };
        });
      })
      .then(function (result) {
        if (result.ok) {
          var state = { status: result.data.status || (action === "approve" ? "approved" : "rejected"), at: nowHm() };
          remember(ctx.approvalId, state);
          renderResult(card, state);
          return;
        }
        if (result.status === 409) {
          // 已被别人处理：如实显示，不假装成功
          var decided = { status: "processed", at: nowHm() };
          remember(ctx.approvalId, decided);
          renderResult(card, decided);
          return;
        }
        showError(card, result.data.detail || "操作失败，请刷新后重试");
        (buttons || []).forEach(function (b) {
          b.disabled = false;
          b.removeAttribute("aria-busy");
        });
      })
      .catch(function () {
        showError(card, "网络异常，未能提交。可点「查看订单详情」旁的链接改用确认页。");
        (buttons || []).forEach(function (b) {
          b.disabled = false;
          b.removeAttribute("aria-busy");
        });
      });
  }

  function showError(card, message) {
    var old = card.querySelector("[data-cy-error]");
    if (old) old.remove();
    var box = el(
      "div",
      "margin-top:10px;padding:8px 10px;border-radius:8px;background:#FBEDEA;color:#8A2B21;font-size:12px;line-height:1.6",
      message
    );
    box.setAttribute("data-cy-error", "1");
    card.lastElementChild.appendChild(box);
  }

  // --- 绑定 -----------------------------------------------------------------

  function enhance(card) {
    var links = card.querySelectorAll("a[data-approval-action]");
    if (!links.length) return;

    var approveLink = card.querySelector('a[data-approval-action="approve"]');
    var rejectLink = card.querySelector('a[data-approval-action="reject"]');
    var ctx = parseAction((approveLink || rejectLink).getAttribute("href") || "");
    if (!ctx || !ctx.approvalId || !ctx.token) return;

    var known = states[ctx.approvalId];
    if (known) {
      renderResult(card, known);
      return;
    }
    if (card.getAttribute("data-cy-bound") === "1") return;
    card.setAttribute("data-cy-bound", "1");

    if (approveLink) {
      approveLink.setAttribute("title", "点击后立即执行，不可撤销");
      approveLink.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        submit(card, ctx, "approve", null, [approveLink, rejectLink]);
      });
    }
    if (rejectLink) {
      rejectLink.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        openRejectPanel(card, ctx, function () {
          /* 取消后按钮恢复可点（submit 未触发，无需复位） */
        });
      });
    }
  }

  // --- 扫描（含 Vue 重渲染后的重放） ---------------------------------------

  var scheduled = false;

  function scan() {
    scheduled = false;
    hideFormEcho();
    var cards = document.querySelectorAll(CARD_SELECTOR);
    for (var i = 0; i < cards.length; i += 1) enhance(cards[i]);
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    window.setTimeout(scan, 200);
  }

  function start() {
    scan();
    var observer = new MutationObserver(schedule);
    observer.observe(document.body, { childList: true, subtree: true });
    // 会话切换是 SPA 路由，不触发 childList；补一个低频兜底
    window.setInterval(scan, 3000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
