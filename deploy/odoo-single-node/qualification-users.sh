#!/bin/bash
set -euo pipefail

if [[ "${WOA_QUALIFICATION:-}" != "1" ]]; then
  echo "synthetic users are restricted to the qualification profile" >&2
  exit 2
fi
install -d -m 0700 -o 100 -g 101 /run/woa-private /run/woa-generated
for name in db-password session-secret odoo.conf writer-login-password reader-login-password; do
  install -m 0400 -o 100 -g 101 "/run/woa-input/${name}" "/run/woa-private/${name}"
done
export PASSWORD_FILE=/run/woa-private/db-password
setpriv --reuid=100 --regid=101 --init-groups \
  odoo shell --config=/run/woa-private/odoo.conf --database=woa_trial \
  --no-http --log-level=warn < /usr/local/lib/woa-qualification-users.py
install -d -m 0700 "/run/woa-output/delegated-keys"
for name in writer.api-key.json reader.api-key.json; do
  install -m 0600 "/run/woa-generated/${name}" "/run/woa-output/delegated-keys/${name}"
done
install -m 0600 /run/woa-generated/delegations.json /run/woa-output/delegations.json
echo "Synthetic writer and reader credentials are ready for the isolated qualification stack."
