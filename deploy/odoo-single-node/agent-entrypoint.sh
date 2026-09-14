#!/bin/bash
set -euo pipefail

install -d -m 0700 -o 10001 -g 10001 /var/lib/woa
chown -R 10001:10001 /var/lib/woa
install -d -m 0700 -o 10001 -g 10001 /run/woa-secrets
install -d -m 0700 -o 10001 -g 10001 /data /config
chown -R 10001:10001 /data /config
export HOME=/var/lib/woa
export XDG_DATA_HOME=/data
export XDG_CONFIG_HOME=/config
for name in session-secret delegations.json model.env; do
  install -m 0400 -o 10001 -g 10001 "/run/woa-input/${name}" "/run/woa-secrets/${name}"
done
for source in /run/woa-input/*.api-key.json; do
  [ -f "$source" ] || continue
  name="$(basename "$source")"
  install -m 0400 -o 10001 -g 10001 "$source" "/run/woa-secrets/$name"
done

scripted=""
if [ "${WOA_SCRIPTED:-0}" = "1" ]; then
  scripted="--scripted"
fi
mutations=""
if [ "${WOA_ALLOW_MUTATIONS:-0}" = "1" ]; then
  mutations="--allow-mutations"
fi
setpriv --reuid=10001 --regid=10001 --init-groups \
  python -m web_operation_agent_odoo.native_server \
  --rpc-base-url "${WOA_ODOO_RPC_URL:?}" \
  --public-origin "${WOA_PUBLIC_ORIGIN:?}" \
  --public-agent-url "${WOA_PUBLIC_ORIGIN}/assistant" \
  --delegation-dir /run/woa-secrets \
  --session-secret /run/woa-secrets/session-secret \
  --state-dir /var/lib/woa \
  --gateway-port 8766 \
  --bootstrap-host 0.0.0.0 \
  --bootstrap-port 8767 \
  --allow-private-http \
  --model-env /run/woa-secrets/model.env \
  --otlp-endpoint "${WOA_OTEL_EXPORTER_OTLP_ENDPOINT:-}" \
  --alert-webhook "${WOA_ALERT_WEBHOOK_URL:-}" \
  ${scripted} ${mutations} &
agent_pid=$!

setpriv --reuid=10001 --regid=10001 --init-groups \
  caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
caddy_pid=$!

shutdown() {
  kill -TERM "$agent_pid" "$caddy_pid" 2>/dev/null || true
  wait "$agent_pid" "$caddy_pid" 2>/dev/null || true
}
trap shutdown EXIT INT TERM
wait -n "$agent_pid" "$caddy_pid"
exit 1
