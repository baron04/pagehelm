/** @odoo-module **/

(() => {
  if (window.top !== window || window.__WOA_NATIVE_BOOTSTRAP_STARTED__) return;
  window.__WOA_NATIVE_BOOTSTRAP_STARTED__ = true;

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

  window.__WOA_RUNTIME_READY__ = fetch("/woa/session-assertion", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({runtime_instance_id: runtimeInstanceId}),
  }).then(response => {
    if (!response.ok) throw new Error(`assistant identity failed (${response.status})`);
    return response.json();
  }).then(assertion => fetch("/assistant/bootstrap", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(assertion),
  })).then(response => {
    if (!response.ok) throw new Error(`assistant bootstrap failed (${response.status})`);
    return response.json();
  }).then(payload => import(payload.runtime_module).then(
    module => module.startEmbeddedRuntime(payload.config)));
})();
