#!/bin/bash
set -euo pipefail

install -d -m 0700 -o 10001 -g 10001 /var/lib/woa
chown -R 10001:10001 /var/lib/woa
install -d -m 0700 -o 10001 -g 10001 /run/woa-secrets
export HOME=/var/lib/woa
for name in session-secret delegations.json model.env; do
  install -m 0400 -o 10001 -g 10001 "/run/woa-input/${name}" "/run/woa-secrets/${name}"
done
install -d -m 0700 -o 10001 -g 10001 /run/woa-secrets/delegated-keys
for source in /run/woa-input/delegated-keys/*.api-key.json; do
  [ -f "$source" ] || continue
  name="$(basename "$source")"
  install -m 0400 -o 10001 -g 10001 "$source" "/run/woa-secrets/delegated-keys/$name"
done

scripted=""
if [ "${WOA_SCRIPTED:-0}" = "1" ]; then
  scripted="--scripted"
fi
mutations=""
if [ "${WOA_ALLOW_MUTATIONS:-0}" = "1" ]; then
  mutations="--allow-mutations"
fi
args=(python -m web_operation_agent_odoo.native_server \
  --rpc-base-url "${WOA_ODOO_RPC_URL:?}" \
  --public-origin "${WOA_PUBLIC_ORIGIN:?}" \
  --public-agent-url "${WOA_PUBLIC_ORIGIN}/assistant" \
  --delegation-dir /run/woa-secrets \
  --session-secret /run/woa-secrets/session-secret \
  --state-dir /var/lib/woa \
  --gateway-host 0.0.0.0 \
  --trusted-gateway-network \
  --gateway-port 8766 \
  --bootstrap-host 0.0.0.0 \
  --bootstrap-port 8767 \
  --allow-private-http \
  --model-env /run/woa-secrets/model.env \
  --otlp-endpoint "${WOA_OTEL_EXPORTER_OTLP_ENDPOINT:-}" \
  --alert-webhook "${WOA_ALERT_WEBHOOK_URL:-}")
if [ -n "$scripted" ]; then args+=("$scripted"); fi
if [ -n "$mutations" ]; then args+=("$mutations"); fi
exec setpriv --reuid=10001 --regid=10001 --init-groups "${args[@]}"
