// The assistant on a public yudao site. yudao keeps the signed-in person's access token in
// localStorage (web-storage-cache: {"c", "e", "v": JSON.stringify(value)}), not in a cookie,
// so the runtime's bootstrap request would carry no identity. This loader adds the token and
// tenant to that one same-origin request only, and starts the runtime once someone is signed
// in. It holds no credentials of its own and changes no other request.
//
// yudao's access token lasts half an hour; its own pages renew it with the refresh token the
// moment a request of theirs is refused. Someone using only the assistant makes no such
// request, so a refused bootstrap renews the token here the same way -- yudao's own
// refresh-token call, the result written back where yudao keeps it -- and asks once more.
// Only a refresh yudao itself refuses means signing in again.
(() => {
  if (window.top !== window || window.__WOA_YUDAO_LOADER__) return;
  window.__WOA_YUDAO_LOADER__ = true;
  const BOOTSTRAP = "/woa-agent-boot/bootstrap";
  const RUNTIME = "/woa-agent/v1/runtime/assistant.js";

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
      const raw = localStorage.getItem(key);
      const record = raw ? JSON.parse(raw) : {c: Date.now(), e: 0};
      record.v = JSON.stringify(value);
      localStorage.setItem(key, JSON.stringify(record));
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
    if (!refresh) return false;
    try {
      const response = await fetch(
        "/admin-api/system/auth/refresh-token?refreshToken=" + encodeURIComponent(refresh),
        {method: "POST", headers: {"tenant-id": tenant()}, credentials: "same-origin"});
      const body = await response.json();
      const data = body && body.code === 0 ? body.data : null;
      if (!data || !data.accessToken) return false;
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
    let response = await fetch(input, withIdentity(init));
    if (response.status === 401 && await renew()) response = await fetch(input, withIdentity(init));
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

  const start = () => {
    if (!stored("ACCESS_TOKEN")) return false;
    if (document.querySelector("script[data-woa-bootstrap]")) return true;
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
    return true;
  };
  // The login page has no token yet; the admin UI signs in without reloading the page.
  const begin = () => {
    if (!start()) {
      const timer = setInterval(() => { if (start()) clearInterval(timer); }, 1500);
    }
  };
  if (document.head) begin();
  else document.addEventListener("DOMContentLoaded", begin, {once: true});
})();
