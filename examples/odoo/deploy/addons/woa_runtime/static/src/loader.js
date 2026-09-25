/** @odoo-module **/

(() => {
  if (window.top !== window || window.__WOA_BOOTSTRAP_SHELL__) return;

  const tag = document.querySelector("script[data-woa-runtime][data-woa-bootstrap]");
  const runtime = tag?.dataset.woaRuntime;
  if (!runtime) return;
  const runtimeUrl = new URL(runtime, location.href);
  if (runtimeUrl.origin !== location.origin) return;

  const host = document.createElement("div");
  host.id = "woa-assistant-bootstrap-shell";
  host.setAttribute("data-page-agent-ignore", "true");
  host.style.setProperty("z-index", "2147483646", "important");
  const shadow = host.attachShadow({mode: "open"});
  const css = `
    :host { all: initial }
    .shell { position: fixed; right: 20px; bottom: 20px; display: flex;
      flex-direction: column; align-items: flex-end; gap: 10px;
      font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #20242c; }
    .launcher { width: 52px; height: 52px; border-radius: 50%; border: 1px solid #d8dde6;
      background: #fff; color: #343b48; box-shadow: 0 8px 24px rgba(15,23,42,.18);
      cursor: pointer; font: 600 13px/1 system-ui, sans-serif; }
    .panel { width: min(360px, calc(100vw - 32px)); box-sizing: border-box; padding: 16px;
      border: 1px solid #d8dde6; border-radius: 14px; background: #fff;
      box-shadow: 0 14px 40px rgba(15,23,42,.2); }
    .panel[hidden] { display: none }
    .title { display: flex; align-items: center; gap: 8px; margin: 0 0 12px;
      font-weight: 650; font-size: 15px; }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: #d97706; }
    .message { margin: 0; color: #4b5563; white-space: pre-wrap; }
    .retry { margin-top: 14px; padding: 7px 13px; border: 1px solid #cbd2dc;
      border-radius: 8px; background: #f7f8fa; color: #20242c; cursor: pointer; font: inherit; }
    .retry:disabled { cursor: wait; opacity: .65 }
    .launcher:focus-visible, .retry:focus-visible { outline: 2px solid #2563eb;
      outline-offset: 2px; }
    @media (prefers-color-scheme: dark) {
      .shell { color: #edf0f5 } .launcher, .panel { background: #20242c;
        border-color: #454d5b; color: #edf0f5 } .message { color: #c6ccd6 }
      .retry { background: #2a303a; border-color: #586172; color: #edf0f5 }
    }
  `;
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(css);
    shadow.adoptedStyleSheets = [sheet];
  } catch {
    const style = document.createElement("style");
    style.textContent = css;
    shadow.append(style);
  }

  const shell = document.createElement("div");
  shell.className = "shell";
  const panel = document.createElement("section");
  panel.className = "panel";
  panel.hidden = true;
  panel.setAttribute("role", "status");
  const title = document.createElement("h2");
  title.className = "title";
  const dot = document.createElement("span");
  dot.className = "dot";
  const titleText = document.createElement("span");
  titleText.textContent = "AI 助手";
  title.append(dot, titleText);
  const message = document.createElement("p");
  message.className = "message";
  message.textContent = "正在连接 AI 助手…";
  const retry = document.createElement("button");
  retry.className = "retry";
  retry.type = "button";
  retry.textContent = "重试";
  retry.hidden = true;
  panel.append(title, message, retry);
  const launcher = document.createElement("button");
  launcher.className = "launcher";
  launcher.type = "button";
  launcher.textContent = "AI";
  launcher.setAttribute("aria-label", "打开 AI 助手");
  shell.append(panel, launcher);
  shadow.append(shell);
  (document.body || document.documentElement).append(host);

  const contained = (fn) => (...args) => {
    try {
      const result = fn(...args);
      result?.catch?.((error) => console.warn("[woa] bootstrap shell:", error));
    } catch (error) {
      console.warn("[woa] bootstrap shell:", error);
    }
  };
  launcher.addEventListener("click", contained(() => { panel.hidden = !panel.hidden; }));

  let stopped = false;
  let attempt = 0;
  let pending = 0;
  // Retry for as long as this page exists. The interval is capped so a long outage does not
  // turn every open tab into a request storm, but there is deliberately no give-up timer.
  const delays = [1000, 3000, 8000];
  // `assistant.js` mounts its panel the moment it runs; from then on the panel is the entry.
  const ready = () => document.getElementById("woa-assistant-root");
  const handoff = () => {
    if (!ready()) return false;
    stopped = true;
    clearTimeout(pending);
    observer.disconnect();
    host.remove();
    return true;
  };
  const observer = new MutationObserver(handoff);
  observer.observe(document.documentElement, {childList: true, subtree: true});

  const explain = async (error) => {
    try {
      const response = await fetch(runtimeUrl, {
        credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) return `助手启动资源不可用（HTTP ${response.status}）。`;
    } catch {
      return "无法连接助手服务，请检查网络或服务状态。";
    }
    return `助手启动失败：${error instanceof Error ? error.message : String(error)}`;
  };

  const start = async () => {
    if (stopped) return;
    clearTimeout(pending);
    retry.disabled = true;
    retry.hidden = true;
    message.textContent = attempt ? "正在重新连接 AI 助手…" : "正在连接 AI 助手…";
    try {
      const url = new URL(runtimeUrl);
      if (attempt) url.searchParams.set("woa_retry", String(Date.now()));
      await import(/* @vite-ignore */ url.href);
      if (handoff()) return;
      message.textContent = "助手正在初始化…";
    } catch (error) {
      message.textContent = await explain(error);
      retry.hidden = false;
      retry.disabled = false;
      // Do not open the error card over the application. The launcher is always present;
      // the person sees the explanation only after choosing to open the assistant.
      const delay = delays[Math.min(attempt, delays.length - 1)];
      attempt += 1;
      pending = window.setTimeout(contained(start), delay);
    }
  };
  retry.addEventListener("click", contained(() => { attempt = 0; return start(); }));

  window.__WOA_BOOTSTRAP_SHELL__ = {
    retry: start,
    destroy: () => {
      stopped = true;
      clearTimeout(pending);
      observer.disconnect();
      host.remove();
      delete window.__WOA_BOOTSTRAP_SHELL__;
    },
  };
  contained(start)();
})();
