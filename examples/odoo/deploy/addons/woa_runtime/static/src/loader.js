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
    renewalTimer = setTimeout(async () => {
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
    }, Math.min(wait, 2_147_483_647));
  };

  const start = async () => {
    const payload = await bootstrap();
    const module = await import(payload.runtime_module);
    const runtime = await module.startEmbeddedRuntime(payload.config);
    scheduleRenewal(runtime, payload);
    return runtime;
  };
  const ready = start();
  // Keep the public Promise rejectable for diagnostics and tests, while marking the
  // rejection handled so Odoo never converts an assistant startup fault into its popup.
  ready.catch(() => {});
  window.__WOA_RUNTIME_READY__ = ready;
})();
