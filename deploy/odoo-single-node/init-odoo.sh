#!/bin/bash
set -euo pipefail

database="${WOA_ODOO_DATABASE:-woa_trial}"
if [[ ! "$database" =~ ^[A-Za-z0-9_.-]{1,63}$ ]]; then
  echo "invalid Odoo database name" >&2
  exit 2
fi
install -d -m 0700 -o 100 -g 101 /run/woa-private
for name in db-password session-secret odoo.conf odoo-admin-login-password; do
  install -m 0400 -o 100 -g 101 "/run/woa-input/${name}" "/run/woa-private/${name}"
done
db_password="$(cat /run/woa-private/db-password)"
export PGPASSWORD="$db_password"

if [[ "$(psql -h db -U odoo -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='$database'")" != "1" ]]; then
  setpriv --reuid=100 --regid=101 --init-groups \
    odoo --config=/run/woa-private/odoo.conf --database="$database" \
    --init=sale_management,account,woa_runtime --without-demo=all --stop-after-init
fi

setpriv --reuid=100 --regid=101 --init-groups \
  odoo shell --config=/run/woa-private/odoo.conf --database="$database" <<'PY'
password = open('/run/woa-private/odoo-admin-login-password').read().strip()
if len(password) < 20:
    raise RuntimeError('admin login password is too short')
admin = env.ref('base.user_admin')
admin.write({'login': 'admin', 'password': password})
env.cr.commit()
PY

echo "Odoo database is initialized; the admin login password remains in its 0600 secret file."
