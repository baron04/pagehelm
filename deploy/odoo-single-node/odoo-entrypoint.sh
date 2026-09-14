#!/bin/bash
set -euo pipefail

install -d -m 0700 -o 100 -g 101 /run/woa-private
install -m 0400 -o 100 -g 101 /run/woa-input/db-password /run/woa-private/db-password
install -m 0400 -o 100 -g 101 /run/woa-input/session-secret /run/woa-private/session-secret
install -m 0400 -o 100 -g 101 /run/woa-input/odoo.conf /run/woa-private/odoo.conf

export PASSWORD_FILE=/run/woa-private/db-password
export WOA_SESSION_SECRET_FILE=/run/woa-private/session-secret
exec setpriv --reuid=100 --regid=101 --init-groups /entrypoint.sh "$@"
