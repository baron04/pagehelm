/** @odoo-module **/

(() => {
  if (window.top !== window || window.__WOA_NATIVE_BOOTSTRAP_STARTED__) return;
  window.__WOA_NATIVE_BOOTSTRAP_STARTED__ = true;

  // This file runs inside Odoo's own JavaScript. Anything it lets escape -- an exception
  // thrown in a timer, a promise nobody awaited that rejects -- lands in Odoo's global
  // error handler, which opens its "Oops!" dialog over the person's work. A timer that
  // threw every two seconds stacked those dialogs until the page was black. So every
  // callback this file hands to the browser goes through `contained`: a failure is written
  // to the console for whoever has to diagnose it, and goes no further.
  const report = (where, error) => {
    try { console.warn(`[woa] ${where}:`, error); } catch { /* nothing left to do */ }
  };
  const contained = (where, fn) => (...args) => {
    try {
      const result = fn(...args);
      if (result && typeof result.then === "function") {
        result.then(undefined, (error) => report(where, error));
      }
      return result;
    } catch (error) {
      report(where, error);
      return undefined;
    }
  };

  let runtimeInstanceId;
  try {
    runtimeInstanceId = sessionStorage.getItem("woa.runtime.instance.v1");
    if (!runtimeInstanceId) {
      runtimeInstanceId = crypto.randomUUID().replaceAll("-", "");
      sessionStorage.setItem("woa.runtime.instance.v1", runtimeInstanceId);
    }
  } catch {
    runtimeInstanceId = crypto.randomUUID().replaceAll("-", "");
  }

  const bootstrap = async () => {
    const assertionResponse = await fetch("/woa/session-assertion", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({runtime_instance_id: runtimeInstanceId}),
    });
    if (!assertionResponse.ok) {
      throw new Error(`assistant identity failed (${assertionResponse.status})`);
    }
    const assertion = await assertionResponse.json();
    const response = await fetch("/assistant/bootstrap", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(assertion),
    });
    if (!response.ok) throw new Error(`assistant bootstrap failed (${response.status})`);
    return response.json();
  };

  let renewalTimer = 0;
  const scheduleRenewal = (runtime, payload, delay = null) => {
    clearTimeout(renewalTimer);
    const wait = delay ?? Math.max(1000, Number(payload.renew_at || 0) - Date.now());
    renewalTimer = setTimeout(contained("renewal", async () => {
      if (runtime.stopped) return;
      if (!runtime.canRenewSession()) {
        scheduleRenewal(runtime, payload, 5000);
        return;
      }
      try {
        const renewed = await bootstrap();
        runtime.renewSession(renewed.config);
        scheduleRenewal(runtime, renewed);
      } catch {
        // The Runtime retains its original expiry timer and fails closed if the trusted
        // identity cannot be refreshed. A bounded retry must not escape into Odoo's global
        // error handler or turn an assistant outage into a host-application popup.
        if (!runtime.stopped) scheduleRenewal(runtime, payload, 5000);
      }
    }), Math.min(wait, 2_147_483_647));
  };

  // The assistant that cannot start still has to be visible. Every earlier way startup
  // could fail -- a refused bootstrap, an outage, a page whose stored state the server no
  // longer honours -- ended in the same place for the person at the page: no entry at all,
  // no reason, nothing to press. This is the standing entry that says which way it failed
  // and retries; the panel's own states take over the moment the runtime does start.
  const ENTRY_STYLE = `
    :host { all: initial }
    .entry {
      position: fixed; inset-block-end: 20px; inset-inline-end: 20px;
      display: flex; flex-direction: column; align-items: flex-end; gap: 8px;
      font: 13px/1.5 system-ui, sans-serif; color: #1f2430;
    }
    .badge {
      inline-size: 48px; block-size: 48px; border-radius: 50%;
      border: 1px solid rgba(15, 18, 25, .14); background: #fff;
      box-shadow: 0 6px 18px rgba(15, 18, 25, .16);
      cursor: pointer; font-size: 18px; color: #6b7280;
    }
    .note {
      max-inline-size: 260px; padding: 10px 12px; border-radius: 10px;
      background: #fff; border: 1px solid rgba(15, 18, 25, .14);
      box-shadow: 0 6px 18px rgba(15, 18, 25, .16);
    }
    .note p { margin: 0 0 8px }
    .note button {
      font: inherit; padding: 4px 10px; border-radius: 7px; cursor: pointer;
      border: 1px solid rgba(15, 18, 25, .2); background: #f6f7f9;
    }
    @media (prefers-color-scheme: dark) {
      .entry { color: #e7e9ee }
      .badge, .note { background: #20242c; border-color: rgba(255, 255, 255, .16) }
      .note button { background: #2b303a; border-color: rgba(255, 255, 255, .2);
                     color: inherit }
    }`;

  //: What a failed start means, in the words of the person reading it. The status code
  //: stays at the end for whoever has to report it, but it is not the message: "409" tells
  //: nobody anything, and it was the first thing they saw.
  const PLAIN = {
    401: "登录状态已变化，请重新登录后再使用助手",
    403: "当前账号未获授权使用助手",
    404: "助手服务未就绪",
    409: "助手正忙，正在重新连接",
    429: "助手已达并发上限，稍后会自动重试",
  };
  const sentence = (reason) => {
    const plain = PLAIN[Number(reason)] || "助手暂时不可用，正在重试";
    return Number(reason) ? `${plain}（${reason}）。` : `${plain}。`;
  };

  let unavailable = null;
  const showUnavailable = (reason, retry) => {
    if (unavailable) {
      unavailable.reason.textContent = sentence(reason);
      return;
    }
    const host = document.createElement("div");
    host.id = "woa-assistant-unavailable";
    host.style.setProperty("z-index", "2147483000", "important");
    const shadow = host.attachShadow({mode: "open"});
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(ENTRY_STYLE);
    shadow.adoptedStyleSheets = [sheet];
    const entry = document.createElement("div");
    entry.className = "entry";
    const note = document.createElement("div");
    note.className = "note";
    note.hidden = true;
    const reasonLine = document.createElement("p");
    reasonLine.textContent = sentence(reason);
    const again = document.createElement("button");
    again.type = "button";
    again.textContent = "重试";
    again.addEventListener("click", contained("retry", () => {
      again.disabled = true;
      retry();
    }));
    note.append(reasonLine, again);
    const badge = document.createElement("button");
    badge.type = "button";
    badge.className = "badge";
    badge.textContent = "助手";
    badge.setAttribute("aria-label", "助手暂时不可用");
    badge.addEventListener("click", contained("entry", () => { note.hidden = !note.hidden; }));
    entry.append(note, badge);
    shadow.appendChild(entry);
    (document.body || document.documentElement).appendChild(host);
    unavailable = {host, reason: reasonLine, again};
  };
  const clearUnavailable = () => {
    unavailable?.host.remove();
    unavailable = null;
  };

  //: A short, bounded ladder: a restarted agent is back within seconds, and past this the
  //: person decides when to try again rather than the page retrying forever.
  const RETRY_MS = [1000, 3000, 8000];
  //: How often we look at a running Runtime to see whether its session has ended under it.
  const WATCH_MS = 2000;

  let watchTimer = 0;
  //: A session ends for reasons the page can simply undo -- it expired, the agent
  //: restarted, the browser was quiet long enough to be reaped -- and for one it must
  //: not: the assistant is now open in another page, and reconnecting here would have the
  //: two pages taking it from each other in turn. Everything else reconnects on its own,
  //: because "会话已结束，请刷新页面" asks the person to repair something they did not break.
  const watchRuntime = (runtime) => {
    clearInterval(watchTimer);
    watchTimer = setInterval(contained("watch", () => {
      if (!runtime.stopped) return;
      // Stop watching before doing anything else, so that whatever happens next happens
      // once. The watcher that crashed here kept running and crashed again every tick.
      clearInterval(watchTimer);
      let state = "";
      try {
        state = runtime.panelHost?.snapshot?.().connection?.state || "";
      } catch {
        state = "";
      }
      if (state === "replaced") return;
      try { runtime.stop(); } catch (error) { report("stop", error); }
      // The page was claimed by the Runtime this loader started and has just stopped, so
      // the loader frees it before starting the next -- as the Runtime itself does after a
      // back-forward cache restore. A Runtime that kept the claim after stopping is how
      // the next start came back `null`.
      if (window.__WOA_EMBEDDED_RUNTIME_STARTED__ === runtime
          || window.__WOA_EMBEDDED_RUNTIME_STARTED__ === true) {
        window.__WOA_EMBEDDED_RUNTIME_STARTED__ = false;
      }
      attempt = 0;
      void restart();
    }), WATCH_MS);
  };
  let settle;
  const ready = new Promise(resolve => { settle = resolve; });
  let attempt = 0;
  let pending = 0;

  const failed = (error) => {
    const reason = String(error?.message || error).match(/\((\d{3})\)/)?.[1] || "";
    window.__WOA_RUNTIME_ERROR__ = String(error?.message || error);
    try {
      showUnavailable(reason, () => { attempt = 0; void restart(); });
    } catch (entryError) {
      report("entry", entryError);
    }
    if (attempt < RETRY_MS.length) {
      pending = setTimeout(() => { void restart(); }, RETRY_MS[attempt]);
      attempt += 1;
    } else if (unavailable) {
      unavailable.again.disabled = false;
    }
    return null;
  };

  const start = async () => {
    clearTimeout(pending);
    let payload;
    let runtime;
    try {
      payload = await bootstrap();
      const module = await import(payload.runtime_module);
      runtime = await module.startEmbeddedRuntime(payload.config);
    } catch (error) {
      return failed(error);
    }
    if (!runtime) {
      // No Runtime came back. It is not one to watch or renew -- treating it as one is what
      // read `null.stopped` every two seconds -- and it is not a reason to show nothing
      // either: the person gets the standing entry and the retries, as for any start that
      // did not produce an assistant.
      return failed(new Error("the assistant runtime did not start"));
    }
    clearUnavailable();
    scheduleRenewal(runtime, payload);
    watchRuntime(runtime);
    window.__WOA_RUNTIME_ERROR__ = "";
    settle(runtime);
    return runtime;
  };
  const restart = contained("start", start);
  void restart();
  // The public Promise resolves with the Runtime whenever startup succeeds -- including a
  // later retry -- and is never rejected, so an assistant outage cannot reach Odoo's own
  // global error handler as a popup.
  ready.catch(() => {});
  window.__WOA_RUNTIME_READY__ = ready;
})();
