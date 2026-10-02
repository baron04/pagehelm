#!/bin/sh
# Release the locally built yudao to a provisioned host, or restore its data from a snapshot.
#
# A release is a new directory under /opt/yudao/releases holding the server jar, its schema
# files, the admin UI built for the public site, the assistant's PageController and the
# compose files; `current` is switched to it, the stack is brought up, and the server must
# answer within the deadline or `current` goes back to the previous release. The database
# lives in its own Docker volume and is left alone, unless --restore names a snapshot: then
# it is replaced by that snapshot and every account is given a new random password at once
# (harden.py and harden.sql), written only to --credentials on this machine.
#
# usage: deploy.sh <ssh-target> [--restore <snapshot.sql.gz> --credentials <file>]
# needs: YUDAO_CACHE (server/, ui/dist-cloud, woa/, business-schema.sql), the built jar.
set -eu
TARGET=${1:?ssh target}; shift
RESTORE="" CREDENTIALS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --restore) RESTORE=$2; shift 2 ;;
    --credentials) CREDENTIALS=$2; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
: "${YUDAO_CACHE:?set YUDAO_CACHE}"
[ -z "$RESTORE" ] || [ -n "$CREDENTIALS" ] || { echo "--restore needs --credentials" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
DEPLOY=$(cd "$HERE/.." && pwd)
JAR=$YUDAO_CACHE/server/yudao-server/target/yudao-server.jar
for f in "$JAR" "$YUDAO_CACHE/ui/dist-cloud/index.html" "$YUDAO_CACHE/woa/page-controller.js" \
         "$YUDAO_CACHE/business-schema.sql"; do
  [ -f "$f" ] || { echo "missing build artifact: $f" >&2; exit 1; }
done
REPO=$(cd "$DEPLOY/../../.." && pwd)
for f in "$REPO/frontend/src/integrations/yudao/page-facts.js" \
         "$REPO/frontend/src/integrations/yudao/yudao-page-controller.js"; do
  [ -f "$f" ] || { echo "missing native page artifact: $f" >&2; exit 1; }
done
WHEELS=$(mktemp -d)
trap 'rm -rf "$WHEELS"' EXIT
# The assistant host: the core and the yudao adapter, as wheels the host builds its image from.
uv build --quiet --wheel --out-dir "$WHEELS" "$REPO"
uv build --quiet --wheel --out-dir "$WHEELS" "$REPO/examples/yudao"
RELEASE=$(date +%Y%m%d%H%M%S)
DIR=/opt/yudao/releases/$RELEASE
COMPOSE="docker compose -p yudao --env-file /opt/yudao/.env -f /opt/yudao/current/compose.yaml -f /opt/yudao/current/compose.cloud.yaml"

echo "release $RELEASE -> $TARGET"
ssh "$TARGET" "mkdir -p $DIR/server/yudao-server/target $DIR/server/sql/mysql $DIR/ui $DIR/woa $DIR/agent/wheels $DIR/source/ui"
rsync -az "$JAR" "$TARGET:$DIR/server/yudao-server/target/"
rsync -az "$YUDAO_CACHE/server/sql/mysql/ruoyi-vue-pro.sql" "$YUDAO_CACHE/server/sql/mysql/quartz.sql" \
  "$TARGET:$DIR/server/sql/mysql/"
rsync -az "$YUDAO_CACHE/business-schema.sql" "$DEPLOY/compose.yaml" "$DEPLOY/compose.cloud.yaml" "$TARGET:$DIR/"
rsync -az --delete "$YUDAO_CACHE/ui/dist-cloud/" "$TARGET:$DIR/ui/"
rsync -az --delete "$YUDAO_CACHE/woa/" "$TARGET:$DIR/woa/"
rsync -az "$HERE/yudao-loader.js" "$TARGET:$DIR/woa/"
rsync -az "$REPO/frontend/src/integrations/yudao/page-facts.js" \
  "$REPO/frontend/src/integrations/yudao/yudao-page-controller.js" "$TARGET:$DIR/woa/"
rsync -az "$WHEELS/" "$TARGET:$DIR/agent/wheels/"
rsync -az "$HERE/agent.Dockerfile" "$TARGET:$DIR/agent/"
# The source the assistant compiles its rules from and reads the UI's lists and tables in:
# the pages' .vue and .ts files, and every module's Java.
rsync -az --prune-empty-dirs --include='*/' --include='*.vue' --include='*.ts' --exclude='*' \
  "$YUDAO_CACHE/ui/src" "$TARGET:$DIR/source/ui/"
for module in $(cd "$YUDAO_CACHE/server" && ls -d yudao-module-*/src/main/java 2>/dev/null \
                 | sed 's#^yudao-module-##; s#/src/main/java$##'); do
  ssh "$TARGET" "mkdir -p $DIR/source/server/yudao-module-$module/src/main/java"
  rsync -az "$YUDAO_CACHE/server/yudao-module-$module/src/main/java/" \
    "$TARGET:$DIR/source/server/yudao-module-$module/src/main/java/"
done

ssh "$TARGET" sh -s -- "$RELEASE" <<'REMOTE'
set -eu
RELEASE=$1
cd /opt/yudao
PREVIOUS=$(readlink current 2>/dev/null || true)
ln -sfn "releases/$RELEASE" current.next && mv -T current.next current
export YUDAO_CACHE=/opt/yudao/current
# The assistant runs once the owner has put the model provider's settings on the host.
if [ -f /opt/yudao/agent.env ]; then export COMPOSE_PROFILES=agent; fi
# A container keeps the jar it was created with (its mount resolves `current` then), so a
# release with another jar recreates the server; one with the same jar leaves it running.
JAR=server/yudao-server/target/yudao-server.jar
RECREATE=""
if [ -z "$PREVIOUS" ] || [ "$(sha256sum < "$PREVIOUS/$JAR")" != "$(sha256sum < "current/$JAR")" ]; then
  RECREATE="--force-recreate"
fi
up() { [ -z "${COMPOSE_PROFILES:-}" ] || docker compose -p yudao --env-file /opt/yudao/.env -f current/compose.yaml -f current/compose.cloud.yaml build agent; \
       docker compose -p yudao --env-file /opt/yudao/.env -f current/compose.yaml -f current/compose.cloud.yaml up -d --remove-orphans; \
       [ -z "$RECREATE" ] || docker compose -p yudao --env-file /opt/yudao/.env -f current/compose.yaml -f current/compose.cloud.yaml up -d $RECREATE --no-deps server; }
healthy() {
  i=0
  while [ $i -lt 96 ]; do  # eight minutes: the server starts in two to four
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:48080/admin-api/system/tenant/get-id-by-name?name=x || true)
    [ "$code" = 200 ] && return 0
    i=$((i + 1)); sleep 5
  done
  return 1
}
up
if healthy; then
  echo "release $RELEASE is serving"
  ls -1dt releases/* | tail -n +4 | xargs -r rm -rf  # keep the last three
else
  echo "release $RELEASE did not start; back to ${PREVIOUS:-nothing}" >&2
  docker compose -p yudao logs --tail 40 server >&2 || true
  if [ -n "$PREVIOUS" ]; then ln -sfn "$PREVIOUS" current.next && mv -T current.next current && up && healthy || true; fi
  exit 1
fi
REMOTE

if [ -n "$RESTORE" ]; then
  echo "restoring $(basename "$RESTORE") and giving every account a new password"
  gunzip -c "$RESTORE" | ssh "$TARGET" 'docker exec -i yudao-mysql-1 sh -c '"'"'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" 2>/dev/null'"'"
  ssh "$TARGET" 'docker restart yudao-server-1 >/dev/null && i=0; until [ "$(curl -s -o /dev/null -w %{http_code} http://127.0.0.1:48080/admin-api/system/tenant/get-id-by-name?name=x)" = 200 ]; do i=$((i+1)); [ $i -lt 96 ] || exit 1; sleep 5; done'
  PORT=48180
  ssh -f -N -o ExitOnForwardFailure=yes -L "$PORT:127.0.0.1:48080" "$TARGET"
  trap 'rm -rf "$WHEELS"; pkill -f "ssh -f -N -o ExitOnForwardFailure=yes -L $PORT:127.0.0.1:48080" || true' EXIT
  (cd "$DEPLOY" && python3 harden.py --base "http://127.0.0.1:$PORT" --out "$CREDENTIALS")
  ssh "$TARGET" 'docker exec -i yudao-mysql-1 sh -c '"'"'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" ruoyi-vue-pro 2>/dev/null'"'" < "$DEPLOY/harden.sql"
  echo "restored; the new passwords are in $CREDENTIALS"
fi
