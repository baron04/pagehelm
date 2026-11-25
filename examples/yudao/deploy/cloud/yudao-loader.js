// The assistant on a public yudao site. yudao keeps the signed-in person's access token in
// localStorage (web-storage-cache: {"c", "e", "v": JSON.stringify(value)}), not in a cookie,
// so the runtime's bootstrap request would carry no identity. This loader adds the token and
// tenant to that one same-origin request only, and starts the runtime once someone is signed
// in. It holds no credentials of its own and changes no other request.
//
// yudao's access token lasts half an hour; its own pages renew it with the refresh token the
// moment a request of theirs is refused. Someone using only the assistant makes no such
// request. The Runtime asks for bootstrap before each new task as well as on its timer,
// so a refused bootstrap renews the token here the same way -- yudao's own
// refresh-token call, the result written back where yudao keeps it -- and asks once more.
// Only a refresh yudao itself refuses means signing in again.
(() => {
  if (window.top !== window || window.__WOA_YUDAO_LOADER__) return;
  const BOOTSTRAP = "/woa-agent-boot/bootstrap";
  const RUNTIME = "/woa-agent/v1/runtime/assistant.js";
  const PUBLIC_PATHS = [
    "/login",
    "/sso",
    "/social-login",
    "/auth-redirect",
    "/bind",
    "/register",
    "/oauthLogin",
    "/pms/kb/document/share",
  ];
  const SYNC_INTERVAL_MS = 250;

  const isPublicPath = (path) => {
    const value = String(path || "");
    return PUBLIC_PATHS.some((prefix) => value === prefix || value.startsWith(prefix + "/"));
  };

  const stored = (key) => {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return "";
      const record = JSON.parse(raw);
      if (record.e && record.e < Date.now()) return "";
      const value = JSON.parse(record.v);
      return value === null || value === undefined ? "" : String(value);
    } catch {
      return "";
    }
  };

  const store = (key, value) => {
    try {
      // Match the application's wsCache.set default. Server token lifetimes are unchanged.
      localStorage.setItem(key, JSON.stringify({c: Date.now(), e: 0, v: JSON.stringify(value)}));
    } catch {
      // Storage refused: the next bootstrap is refused again and says to sign in.
    }
  };

  const fetch = window.fetch.bind(window);
  // The server supplies the guard endpoint. Its identity is browser-only; credentials
  // are attached only to begin/end POSTs at that same-origin endpoint.
  let guardPath = "";
  const tenant = () => stored("tenantId") || "1";
  let renewing = null;
  // One renewal at a time, whoever asks: two would each retire the other's new token.
  const renew = () => renewing ??= (async () => {
    const refresh = stored("REFRESH_TOKEN");
    const actorTenant = tenant();
    if (!refresh) return false;
    try {
      const response = await fetch(
        "/admin-api/system/auth/refresh-token?refreshToken=" + encodeURIComponent(refresh),
        {method: "POST", headers: {"tenant-id": actorTenant}, credentials: "same-origin"});
      const body = await response.json();
      const data = body && body.code === 0 ? body.data : null;
      if (!data || !data.accessToken) return false;
      // A page sign-out, sign-in or its own refresh won the race: do not overwrite it.
      if (stored("REFRESH_TOKEN") !== refresh || tenant() !== actorTenant) return false;
      store("ACCESS_TOKEN", data.accessToken);
      if (data.refreshToken) store("REFRESH_TOKEN", data.refreshToken);
      return true;
    } catch {
      return false;
    }
  })().finally(() => { renewing = null; });

  const withIdentity = (init) => {
    const headers = new Headers(init.headers || {});
    headers.set("Authorization", "Bearer " + stored("ACCESS_TOKEN"));
    headers.set("tenant-id", tenant());
    return {...init, headers};
  };
  window.fetch = async (input, init = {}) => {
    let url;
    try {
      url = new URL(typeof input === "string" ? input : input.url, location.href);
    } catch {
      return fetch(input, init);
    }
    if (url.origin !== location.origin) return fetch(input, init);
    const method = String(init.method || (typeof input === "string" ? "GET" : input.method || "GET")).toUpperCase();
    if (guardPath && method === "POST" && !url.search && !url.hash &&
        (url.pathname === guardPath + "/begin" || url.pathname === guardPath + "/end")) {
      return fetch(input, withIdentity(init));
    }
    if (url.pathname !== BOOTSTRAP) return fetch(input, init);
    const attemptedToken = stored("ACCESS_TOKEN");
    const attemptedTenant = tenant();
    let response = await fetch(input, withIdentity(init));
    if (response.status === 401 && tenant() === attemptedTenant) {
      // The application's axios client or another bootstrap may already have refreshed.
      const changed = () => Boolean(stored("ACCESS_TOKEN")) && stored("ACCESS_TOKEN") !== attemptedToken;
      if (changed() || await renew() || changed()) {
        if (tenant() === attemptedTenant) response = await fetch(input, withIdentity(init));
      }
    }
    if (response.ok) {
      try {
        const config = (await response.clone().json()).config;
        const value = config && config.pageWriteGuard;
        const target = typeof value === "string" && value.startsWith("/") ? new URL(value, location.href) : null;
        guardPath = target && target.origin === location.origin && !target.search && !target.hash ? target.pathname : "";
      } catch { guardPath = ""; }
    }
    return response;
  };

  const hasToken = () => Boolean(stored("ACCESS_TOKEN") || stored("REFRESH_TOKEN"));

  const readYudaoState = () => {
    const appEl = document.getElementById("app");
    const vueApp = appEl && appEl.__vue_app__;
    const globals = vueApp && vueApp.config && vueApp.config.globalProperties;
    const stores = globals && globals.$pinia && globals.$pinia._s;
    const getStore = (name) => (stores && typeof stores.get === "function" ? stores.get(name) : null);
    const router = globals && globals.$router;
    const route = router && router.currentRoute && router.currentRoute.value;
    return {
      appEl,
      vueApp,
      userStore: getStore("admin-user"),
      appStore: getStore("app"),
      router,
      route,
    };
  };

  const hasVisibleFullscreenLoading = () => {
    const masks = document.querySelectorAll(".el-loading-mask.is-fullscreen");
    for (const mask of masks) {
      if (!mask.isConnected) continue;
      const style = typeof window.getComputedStyle === "function" ? window.getComputedStyle(mask) : null;
      if (!style) return true;
      const opacity = style.opacity === "" ? 1 : Number(style.opacity);
      if (style.display !== "none" && style.visibility !== "hidden" && !(opacity <= 0)) return true;
    }
    return false;
  };

  // True only after Yudao has finished authenticating, loading user/routes and dismissing
  // the splash/login loading overlays. Prevents showing the panel or launcher mid-login or
  // beside the initial `.app-loading` screen on refresh.
  const isLoginComplete = () => {
    if (document.readyState === "loading") return false;
    if (!hasToken() || isPublicPath(location.pathname)) return false;
    const {appEl, vueApp, userStore, appStore, route} = readYudaoState();
    if (!appEl || !vueApp || appEl.childElementCount === 0) return false;
    if (appEl.querySelector(".app-loading")) return false;
    if (!userStore || !userStore.isSetUser) return false;
    if (!route || !Array.isArray(route.matched) || route.matched.length === 0) return false;
    if (isPublicPath(route.path)) return false;
    if (appStore && appStore.pageLoading) return false;
    if (hasVisibleFullscreenLoading()) return false;
    return true;
  };

  // Once mounted, keep the assistant across normal authenticated SPA route changes (where
  // `appStore.pageLoading` briefly toggles), and tear down immediately on sign-out or return
  // to a login/public route.
  const isSessionStillAuthenticated = () => {
    if (!hasToken() || isPublicPath(location.pathname)) return false;
    const {appEl, vueApp, userStore, route} = readYudaoState();
    if (!appEl || !vueApp || appEl.querySelector(".app-loading")) return false;
    if (!userStore || !userStore.isSetUser) return false;
    if (route && isPublicPath(route.path)) return false;
    return true;
  };

  let mounted = false;
  let ownsBootstrapScript = false;
  let runtimeEverInjected = false;
  let loaderScript = null;
  let bootingModule = false;
  let disposed = false;
  let syncScheduled = false;
  let hookedRouter = null;
  let hookedUserStore = null;
  let hookedAppStore = null;
  let unhookRouterAfter = null;
  let unhookRouterBefore = null;
  let unhookUserStore = null;
  let unhookAppStore = null;
  let domObserver = null;
  let observedAppEl = null;
  let observedBody = null;
  let timer = null;

  const ensureBootstrapScript = () => {
    if (loaderScript && loaderScript.isConnected) return loaderScript;
    const existing = document.querySelector("script[data-woa-bootstrap]");
    if (existing) return existing;
    const script = document.createElement("script");
    script.type = "module";
    script.src = RUNTIME;
    script.dataset.woaBootstrap = BOOTSTRAP;
    // Ways into a new conversation, one per kind, in this application's own words.
    script.dataset.woaExamples = JSON.stringify([
      {kind: "query", text: "CRM 联系人一共有多少个？"},
      {kind: "find", text: "回款计划在哪里看？"},
      {kind: "operate", text: "打开合同管理页面"},
    ]);
    document.head.append(script);
    loaderScript = script;
    ownsBootstrapScript = true;
    return script;
  };

  const mountAssistant = () => {
    if (mounted || !document.head) return;
    // Respect a host/test page that already placed its own bootstrap tag before this loader ran.
    if (!ownsBootstrapScript && document.querySelector("script[data-woa-bootstrap]")) {
      mounted = true;
      return;
    }
    mounted = true;
    const alreadyEvaluated = runtimeEverInjected;
    runtimeEverInjected = true;
    ensureBootstrapScript();
    // An ES module script tag with the same URL does not re-execute after SPA sign-out +
    // re-login in the same document, so invoke the cached module's exported `autoBoot()`.
    if (alreadyEvaluated && !window.__WOA_ASSISTANT__ && !bootingModule) {
      bootingModule = true;
      import(/* @vite-ignore */ RUNTIME)
        .then((mod) => {
          if (!disposed && mounted && isSessionStillAuthenticated() && !window.__WOA_ASSISTANT__) {
            if (mod && typeof mod.autoBoot === "function") mod.autoBoot(window);
          }
        })
        .catch(() => {
          // Allow the next sync tick to retry a transient module import failure.
          if (mounted && !window.__WOA_ASSISTANT__) mounted = false;
        })
        .finally(() => { bootingModule = false; });
    }
  };

  const unmountAssistant = () => {
    if (!mounted) return;
    mounted = false;
    guardPath = "";
    if (!ownsBootstrapScript) return;
    if (loaderScript && loaderScript.isConnected) loaderScript.remove();
    loaderScript = null;
    const assistant = window.__WOA_ASSISTANT__;
    if (assistant && typeof assistant.destroy === "function") {
      assistant.destroy({clearSession: true});
    }
  };

  const attachAppHooks = () => {
    const {appEl, userStore, appStore, router} = readYudaoState();
    if (typeof MutationObserver === "function") {
      if (!domObserver) domObserver = new MutationObserver(() => scheduleSync());
      if (appEl && appEl !== observedAppEl) {
        observedAppEl = appEl;
        domObserver.observe(appEl, {childList: true});
      }
      if (document.body && document.body !== observedBody) {
        observedBody = document.body;
        domObserver.observe(document.body, {childList: true});
      }
    }
    if (router && router !== hookedRouter) {
      try { unhookRouterAfter && unhookRouterAfter(); } catch {}
      try { unhookRouterBefore && unhookRouterBefore(); } catch {}
      hookedRouter = router;
      unhookRouterAfter = typeof router.afterEach === "function"
        ? router.afterEach(() => scheduleSync()) : null;
      unhookRouterBefore = typeof router.beforeEach === "function"
        ? router.beforeEach((to) => { if (to && isPublicPath(to.path)) scheduleSync(); }) : null;
    }
    if (userStore && userStore !== hookedUserStore) {
      try { unhookUserStore && unhookUserStore(); } catch {}
      hookedUserStore = userStore;
      unhookUserStore = typeof userStore.$subscribe === "function"
        ? userStore.$subscribe(() => scheduleSync()) : null;
    }
    if (appStore && appStore !== hookedAppStore) {
      try { unhookAppStore && unhookAppStore(); } catch {}
      hookedAppStore = appStore;
      unhookAppStore = typeof appStore.$subscribe === "function"
        ? appStore.$subscribe(() => scheduleSync()) : null;
    }
  };

  const sync = () => {
    if (disposed) return;
    if (window.__WOA_YUDAO_LOADER__ !== loaderHandle) {
      dispose();
      return;
    }
    attachAppHooks();
    if (!mounted) {
      if (isLoginComplete()) mountAssistant();
    } else if (!isSessionStillAuthenticated()) {
      unmountAssistant();
    }
  };

  const scheduleSync = () => {
    if (disposed || syncScheduled) return;
    syncScheduled = true;
    queueMicrotask(() => {
      syncScheduled = false;
      sync();
    });
  };

  const onDomReady = () => scheduleSync();
  const onStateEvent = () => scheduleSync();

  const dispose = () => {
    if (disposed) return;
    disposed = true;
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
    if (domObserver) {
      domObserver.disconnect();
      domObserver = null;
    }
    try { unhookRouterAfter && unhookRouterAfter(); } catch {}
    try { unhookRouterBefore && unhookRouterBefore(); } catch {}
    try { unhookUserStore && unhookUserStore(); } catch {}
    try { unhookAppStore && unhookAppStore(); } catch {}
    document.removeEventListener("DOMContentLoaded", onDomReady);
    window.removeEventListener("storage", onStateEvent);
    window.removeEventListener("popstate", onStateEvent);
    window.removeEventListener("hashchange", onStateEvent);
    if (window.__WOA_YUDAO_LOADER__ === loaderHandle) delete window.__WOA_YUDAO_LOADER__;
  };

  const loaderHandle = {dispose, sync};
  window.__WOA_YUDAO_LOADER__ = loaderHandle;

  // The login page has no completed session yet and Yudao signs in/out via SPA routes
  // without reloading the page; watch auth, router and loading state continuously.
  window.addEventListener("storage", onStateEvent);
  window.addEventListener("popstate", onStateEvent);
  window.addEventListener("hashchange", onStateEvent);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", onDomReady, {once: true});
  }
  sync();
  timer = setInterval(sync, SYNC_INTERVAL_MS);
})();
