#!/bin/bash
set -euo pipefail

install -d -m 0700 -o 100 -g 101 /run/woa-private
install -m 0400 -o 100 -g 101 /run/woa-input/db-password /run/woa-private/db-password
install -m 0400 -o 100 -g 101 /run/woa-input/session-secret /run/woa-private/session-secret
install -m 0400 -o 100 -g 101 /run/woa-input/odoo.conf /run/woa-private/odoo.conf

export PASSWORD_FILE=/run/woa-private/db-password
export WOA_SESSION_SECRET_FILE=/run/woa-private/session-secret

# Odoo stores data-file views in PostgreSQL. Merely replacing the mounted addon files does
# not load a changed template, so the web client can keep serving an older assistant entry
# after an otherwise successful deployment. Run the addon upgrade before every ordinary
# server start: it is idempotent, happens while this container has no Odoo worker accepting
# requests, and makes a migration failure keep the service down instead of silently starting
# without the assistant.
if [[ "${1:-}" == "odoo" ]]; then
  export PGPASSWORD="$(cat /run/woa-private/db-password)"
  if [[ "$(psql -h "${HOST:-db}" -U "${USER:-odoo}" -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='woa_trial'")" == "1" ]]; then
    setpriv --reuid=100 --regid=101 --init-groups \
      /entrypoint.sh odoo --config=/run/woa-private/odoo.conf \
      --database=woa_trial --update=woa_runtime --stop-after-init
  fi
  unset PGPASSWORD
fi

exec setpriv --reuid=100 --regid=101 --init-groups /entrypoint.sh "$@"
