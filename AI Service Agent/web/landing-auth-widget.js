(function () {
  "use strict";

  const AUTH_KEY = "user_page_auth";
  const CONV_KEY = "user_page_conversation_id";

  function readJSON(raw) {
    try {
      return raw ? JSON.parse(raw) : null;
    } catch (error) {
      return null;
    }
  }

  function loadAuth() {
    try {
      return readJSON(window.localStorage.getItem(AUTH_KEY));
    } catch (error) {
      return null;
    }
  }

  function saveAuth(auth) {
    window.localStorage.setItem(AUTH_KEY, JSON.stringify(auth));
  }

  function clearAuth() {
    window.localStorage.removeItem(AUTH_KEY);
  }

  function readConversationId() {
    try {
      return window.sessionStorage.getItem(CONV_KEY) || null;
    } catch (error) {
      return null;
    }
  }

  function saveConversationId(id) {
    try {
      if (id) window.sessionStorage.setItem(CONV_KEY, id);
      else window.sessionStorage.removeItem(CONV_KEY);
    } catch (error) {
      /* storage may be unavailable in private mode */
    }
  }

  function isNetworkFailure(error) {
    return !!error && (
      error.name === "TypeError" ||
      /failed to fetch|networkerror|load failed/i.test(error.message || "")
    );
  }

  function displayName(auth) {
    return auth && auth.user && auth.user.name ? auth.user.name : "客户";
  }

  function createApi(fetchImpl, baseUrl) {
    let token = null;

    function headers() {
      const result = { "Content-Type": "application/json" };
      if (token) result.Authorization = `Bearer ${token}`;
      return result;
    }

    function apiError(label, response) {
      const error = new Error(`${label} ${response.status}`);
      error.status = response.status;
      error.code = response.status === 401 ? "AUTH_EXPIRED" : label;
      return error;
    }

    return {
      async login(account, password) {
        const response = await fetchImpl(`${baseUrl}/auth/login`, {
          method: "POST",
          headers: headers(),
          body: JSON.stringify({ phone: account, password })
        });
        if (!response.ok) throw apiError("LOGIN_FAILED", response);
        const auth = await response.json();
        token = auth && auth.token ? auth.token : null;
        return auth;
      },
      async createSession(user) {
        const uid = user && (user.id || user.name) ? (user.id || user.name) : "anonymous";
        const response = await fetchImpl(`${baseUrl}/sessions`, {
          method: "POST",
          headers: headers(),
          body: JSON.stringify({ user_id: uid })
        });
        if (!response.ok) throw apiError("SESSION_FAILED", response);
        return response.json();
      },
      setToken(value) {
        token = value || null;
      }
    };
  }

  function createMockApi() {
    return {
      async login(account) {
        return {
          token: "mock-token",
          user: { id: account || "demo-user", name: "客户" }
        };
      },
      async createSession() {
        return {
          conversation_id: `mock-conversation-${Date.now().toString(36)}`,
          mode: "ai"
        };
      },
      setToken() {}
    };
  }

  function getConfig() {
    let mock = false;
    try {
      mock = new URLSearchParams(window.location.search).get("mock") === "true" ||
        window.localStorage.getItem("mock") === "true";
      if (window.location.protocol === "file:") mock = true;
    } catch (error) {
      /* storage may be unavailable */
    }
    return { mock, baseUrl: "" };
  }

  const config = getConfig();
  const page = document.body;
  const loginForm = document.getElementById("landingLoginForm");
  const loginButton = document.getElementById("btn-login");
  const loginError = document.getElementById("login-error");
  const loginDialog = document.getElementById("authDialog");
  const loginTrigger = document.getElementById("btn-open-login");
  const serviceTrigger = document.getElementById("btn-open-widget");
  const logoutButton = document.getElementById("btn-logout");
  const serviceStatus = document.getElementById("serviceStatus");
  const sessionName = document.getElementById("user-name");
  const sessionAvatar = document.getElementById("user-avatar");

  let auth = loadAuth();
  let conversationId = readConversationId();
  let widgetLoading = false;
  let modePoll = null;
  let api = config.mock
    ? createMockApi()
    : createApi(window.fetch ? window.fetch.bind(window) : null, config.baseUrl);

  function setHumanHeader(text) {
    const holder = document.querySelector(".woot-widget-holder");
    if (!holder) return;
    let bar = holder.querySelector(".human-service-bar");
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "human-service-bar";
      bar.setAttribute("role", "status");
      holder.prepend(bar);
    }
    if (text) {
      bar.textContent = text;
      holder.classList.add("human-service");
    } else {
      bar.textContent = "";
      holder.classList.remove("human-service");
    }
  }

  async function refreshHumanBar() {
    if (!auth || !auth.token) {
      setHumanHeader("");
      return;
    }
    try {
      const response = await fetch("/me/conversation_mode", {
        headers: { Authorization: `Bearer ${auth.token}` }
      });
      if (!response.ok) {
        setHumanHeader("");
        return;
      }
      const body = await response.json();
      setHumanHeader(body && body.header ? body.header : "");
    } catch (error) {
      /* header is optional if the runtime is briefly unreachable */
    }
  }

  function startModePoll() {
    if (modePoll) return;
    refreshHumanBar();
    modePoll = window.setInterval(refreshHumanBar, 2000);
  }

  function stopModePoll() {
    if (modePoll) {
      window.clearInterval(modePoll);
      modePoll = null;
    }
    setHumanHeader("");
  }

  window.addEventListener("chatwoot:on-message", () => {
    refreshHumanBar();
  });

  function updateSession() {
    const authed = !!auth;
    page.dataset.authed = authed ? "true" : "false";
    const name = displayName(auth);
    if (sessionName) sessionName.textContent = name;
    if (sessionAvatar) sessionAvatar.textContent = name.slice(0, 1) || "客";
    if (loginTrigger) loginTrigger.textContent = authed ? "打开客服" : "登录";
    if (authed) startModePoll();
    else stopModePoll();
  }

  function updateServiceStatus(message, state) {
    if (!serviceStatus) return;
    serviceStatus.textContent = message || "";
    serviceStatus.classList.toggle("ready", state === "ready");
    serviceStatus.classList.toggle("error", state === "error");
  }

  function showLoginError(message) {
    if (!loginError) return;
    loginError.textContent = message;
    loginError.classList.add("visible");
  }

  function clearLoginError() {
    if (!loginError) return;
    loginError.textContent = "";
    loginError.classList.remove("visible");
  }

  function openLoginDialog() {
    clearLoginError();
    if (loginDialog && typeof loginDialog.showModal === "function") {
      loginDialog.showModal();
      window.setTimeout(() => document.getElementById("login-account")?.focus(), 120);
    }
  }

  async function bootstrapSession() {
    if (!auth || !auth.token) return false;
    api.setToken(auth.token);
    const result = await api.createSession(auth.user);
    const nextConversationId = result && (result.conversation_id || result.session_id || result.id);
    if (!nextConversationId) throw new Error("SESSION_MISSING");
    conversationId = nextConversationId;
    saveConversationId(conversationId);
    return true;
  }

  async function openChatwootWidget() {
    if (widgetLoading) return false;
    widgetLoading = true;
    updateServiceStatus("正在加载客服…");
    try {
      const configResponse = await fetch("/config");
      if (!configResponse.ok) {
        updateServiceStatus("客服服务暂不可用，请稍后重试", "error");
        return false;
      }
      const widgetConfig = await configResponse.json();
      const identityResponse = await fetch("/me/chatwoot_identity", {
        headers: { Authorization: `Bearer ${(auth && auth.token) || ""}` }
      });
      if (!identityResponse.ok) {
        if (identityResponse.status === 401) {
          clearAuth();
          saveConversationId(null);
          auth = null;
          conversationId = null;
          updateSession();
          openLoginDialog();
          showLoginError("登录已过期，请重新登录");
        } else {
          updateServiceStatus("客服身份校验不可用，请稍后重试", "error");
        }
        return false;
      }
      const identity = await identityResponse.json();
      const websiteToken = widgetConfig.chatwoot_website_token;
      const widgetBaseUrl = (widgetConfig.chatwoot_base_url || "").replace(/\/$/, "");
      if (!websiteToken || !widgetBaseUrl) {
        updateServiceStatus("客服服务暂不可用，请稍后重试", "error");
        return false;
      }

      window.chatwootSettings = {
        hideMessageBubble: true,
        position: "right",
        locale: "zh-CN",
        type: "standard",
        darkMode: "light"
      };

      if (!window.$chatwoot) {
        await new Promise((resolve, reject) => {
          const script = document.createElement("script");
          script.src = `${widgetBaseUrl}/packs/js/sdk.js`;
          script.defer = true;
          script.onload = resolve;
          script.onerror = reject;
          document.body.appendChild(script);
        });
      }

      if (!window.chatwootSDK) {
        updateServiceStatus("客服加载失败，请重试", "error");
        return false;
      }

      window.chatwootSDK.run({ websiteToken, baseUrl: widgetBaseUrl });

      if (window.$chatwoot) {
        await new Promise((resolve) => {
          const check = () => {
            if (document.querySelector(".woot-widget-holder") && document.querySelector(".woot--bubble-holder")) { resolve(); return; }
            requestAnimationFrame(check);
          };
          check();
        });
        const userId = auth && auth.user && auth.user.id ? auth.user.id : "chatwoot-user";
        window.$chatwoot.setUser(userId, {
          name: displayName(auth) || "用户",
          email: auth && auth.user && auth.user.id ? `${auth.user.id}@chengyuan.local` : undefined,
          identifier_hash: identity && identity.identifier_hash ? identity.identifier_hash : undefined
        });
        window.$chatwoot.toggleBubbleVisibility("show");
        window.$chatwoot.toggle("open");
        updateServiceStatus("客服已开启", "ready");
        return true;
      }

      updateServiceStatus("客服加载失败，请重试", "error");
      return false;
    } catch (error) {
      console.warn("Chatwoot widget failed to load", error);
      updateServiceStatus("客服加载失败，请重试", "error");
      return false;
    } finally {
      widgetLoading = false;
    }
  }

  async function enterService() {
    if (!auth || !auth.token) {
      openLoginDialog();
      return;
    }
    if (window.$chatwoot) {
      window.$chatwoot.toggle("open");
      return;
    }
    try {
      if (!conversationId) await bootstrapSession();
      await openChatwootWidget();
    } catch (error) {
      if (error.status === 401) {
        clearAuth();
        saveConversationId(null);
        auth = null;
        conversationId = null;
        updateSession();
        openLoginDialog();
        showLoginError("登录已过期，请重新登录");
        return;
      }
      console.warn(error);
      updateServiceStatus("服务会话创建失败，请重试", "error");
    }
  }

  async function doLogin(account, password) {
    clearLoginError();
    loginButton.disabled = true;
    loginButton.textContent = "登录中…";
    try {
      try {
        auth = await api.login(account, password);
      } catch (error) {
        if (!isNetworkFailure(error)) throw error;
        api = createMockApi();
        auth = await api.login(account, password);
        updateServiceStatus("后端未连接，已进入演示模式", "error");
      }
      saveAuth(auth);
      await bootstrapSession();
      updateSession();
      if (loginDialog && loginDialog.open) loginDialog.close();
      await openChatwootWidget();
    } catch (error) {
      showLoginError(`登录失败：${error.message}`);
    } finally {
      loginButton.disabled = false;
      loginButton.textContent = "登录";
    }
  }

  function logout() {
    clearAuth();
    saveConversationId(null);
    auth = null;
    conversationId = null;
    stopModePoll();
    updateSession();
    updateServiceStatus("", "");
    if (window.$chatwoot) {
      window.$chatwoot.reset();
      window.$chatwoot.toggleBubbleVisibility("hide");
      window.$chatwoot.toggle("close");
    }
  }

  loginForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    const account = document.getElementById("login-account").value.trim();
    const password = document.getElementById("login-password").value;
    if (!account || !password) {
      showLoginError("请输入手机号和密码");
      return;
    }
    doLogin(account, password);
  });

  loginTrigger?.addEventListener("click", () => {
    if (auth) enterService();
    else openLoginDialog();
  });

  serviceTrigger?.addEventListener("click", enterService);
  logoutButton?.addEventListener("click", logout);
  loginDialog?.addEventListener("click", (event) => {
    if (event.target === loginDialog) loginDialog.close();
  });

  updateSession();
  // Already logged in (localStorage): always re-open Widget.
  // conversation_id is sessionStorage-only and missing on a fresh tab/window;
  // enterService will bootstrapSession when needed.
  if (auth && auth.token && !window.$chatwoot && !widgetLoading) {
    enterService();
  }
  window.addEventListener("pageshow", () => {
    updateSession();
    if (auth && auth.token && !window.$chatwoot && !widgetLoading) {
      enterService();
    }
  });
})();




