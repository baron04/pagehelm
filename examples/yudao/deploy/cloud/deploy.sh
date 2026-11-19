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
[ -z "$RESTORE" ] || [ -n "$CREDENTIALS" ] || { echo "--restore needs --credentials" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
DEPLOY=$(cd "$HERE/.." && pwd)
REPO=$(cd "$DEPLOY/../../.." && pwd)
ROOT=$REPO
. "$ROOT/scripts/python.sh"
: "${YUDAO_CACHE:=$REPO/.q0-cache/yudao}"
export YUDAO_CACHE
export RSYNC_RSH='ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3'
ssh() { command ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 "$@"; }
JAR=$YUDAO_CACHE/server/yudao-server/target/yudao-server.jar
for f in "$JAR" "$YUDAO_CACHE/ui/dist-cloud/index.html" "$YUDAO_CACHE/woa/page-controller.js" \
         "$YUDAO_CACHE/business-schema.sql" "$YUDAO_CACHE/server/sql/mysql/ruoyi-vue-pro.sql" \
         "$YUDAO_CACHE/server/sql/mysql/quartz.sql"; do
  [ -f "$f" ] || { echo "missing build artifact: $f" >&2; exit 1; }
done
for f in "$REPO/frontend/dist/integrations/yudao/page-facts.js" \
         "$REPO/frontend/dist/integrations/yudao/yudao-page-controller.js" \
         "$REPO/frontend/dist/integrations/yudao/form-surface.js" \
         "$REPO/src/web_operation_agent/web_runtime/assistant.js" \
         "$REPO/src/web_operation_agent/web_runtime/trace_replay.html"; do
  [ -f "$f" ] || { echo "missing native page artifact: $f" >&2; exit 1; }
done
STAGING=$(mktemp -d)
WHEELS=$STAGING/wheels
mkdir -p "$WHEELS"
trap 'rm -rf "$STAGING"' EXIT
# The assistant host: the core and the yudao adapter, as wheels the host builds its image from.
uv build --python "$PYTHON" --quiet --wheel --out-dir "$WHEELS" "$REPO"
uv build --python "$PYTHON" --quiet --wheel --out-dir "$WHEELS" "$REPO/examples/yudao"
RELEASE=$(date -u +%Y%m%d%H%M%S)-$(openssl rand -hex 3)
DIR=/opt/yudao/releases/$RELEASE
COMPOSE="docker compose -p yudao --env-file /opt/yudao/.env -f /opt/yudao/current/compose.yaml -f /opt/yudao/current/compose.cloud.yaml"
"$PYTHON" "$HERE/manifest.py" "$REPO" "$STAGING" "$RELEASE"

echo "release $RELEASE -> $TARGET"
# The server must carry the read-only window (patches/server); a jar without it would let
# the assistant's host refuse every page operation, since its window is never confirmed.
unzip -l "$JAR" | grep -q 'cn/iocoder/yudao/server/woa/WoaPageGuard.class' \
  || { echo "the server jar was built without patches/server (see the deploy README)" >&2; exit 1; }
PREV_BASES=$(ssh "$TARGET" "
  set -eu
  mkdir -p '$DIR/server/yudao-server/target' '$DIR/server/sql/mysql' '$DIR/ui' '$DIR/source/ui' '$DIR/source/server'
  prev=\$(cd -P '/opt/yudao/current' 2>/dev/null && pwd -P || true)
  if [ -n \"\$prev\" ]; then
    for sub in server/yudao-server/target server/sql/mysql ui source/ui source/server; do
      [ -d \"\$prev/\$sub\" ] && printf '%s=%s\n' \"\$sub\" \"\$prev/\$sub\"
    done
  fi
")

rsync_cached() {
  sub=$1; shift
  base=$(printf '%s\n' "$PREV_BASES" | awk -v key="$sub=" 'index($0, key) == 1 { print substr($0, length(key) + 1); exit }')
  if [ -n "$base" ]; then
    rsync -rlptz --link-dest="$base" "$@"
  else
    rsync -rlptz "$@"
  fi
}

# Stage the small per-release bundle locally so one rsync replaces eight separate transfers.
BUNDLE=$STAGING/bundle
mkdir -p "$BUNDLE/agent"
cp -p "$YUDAO_CACHE/business-schema.sql" "$DEPLOY/compose.yaml" "$DEPLOY/compose.cloud.yaml" \
  "$STAGING/release.json" "$STAGING/runtime.sha256" "$BUNDLE/"
cp -Rp "$YUDAO_CACHE/woa" "$BUNDLE/woa"
cp -fp "$HERE/yudao-loader.js" \
  "$REPO/frontend/dist/integrations/yudao/page-facts.js" \
  "$REPO/frontend/dist/integrations/yudao/yudao-page-controller.js" \
  "$REPO/frontend/dist/integrations/yudao/form-surface.js" "$BUNDLE/woa/"
cp -Rp "$WHEELS" "$BUNDLE/agent/wheels"
cp -p "$HERE/agent.Dockerfile" "$BUNDLE/agent/"
cp -Rp "$HERE/native-policy" "$BUNDLE/native-policy"
rsync -rlptz "$BUNDLE/" "$TARGET:$DIR/"

# Reuse unchanged cached artifacts from the active release via --link-dest (and avoid UID/GID mismatch).
rsync_cached server/yudao-server/target "$JAR" "$TARGET:$DIR/server/yudao-server/target/"
rsync_cached server/sql/mysql "$YUDAO_CACHE/server/sql/mysql/ruoyi-vue-pro.sql" \
  "$YUDAO_CACHE/server/sql/mysql/quartz.sql" "$TARGET:$DIR/server/sql/mysql/"
rsync_cached ui --delete "$YUDAO_CACHE/ui/dist-cloud/" "$TARGET:$DIR/ui/"
# The source the assistant compiles its rules from and reads the UI's lists and tables in:
# the pages' .vue and .ts files, and every module's Java.
rsync_cached source/ui --prune-empty-dirs --include='*/' --include='*.vue' --include='*.ts' --exclude='*' \
  "$YUDAO_CACHE/ui/src" "$TARGET:$DIR/source/ui/"
(
  cd "$YUDAO_CACHE/server"
  set -- $(find . -path '*/yudao-module-*/src/main/java' -type d 2>/dev/null | sed 's#^\./##' | sort)
  if [ "$#" -gt 0 ]; then
    rsync_cached source/server -R "$@" "$TARGET:$DIR/source/server/"
  fi
)

ssh "$TARGET" sh -s -- "$RELEASE" "${WOA_PUBLIC_ORIGIN:-}" < "$HERE/activate.sh"

if [ -n "$RESTORE" ]; then
  echo "restoring $(basename "$RESTORE") and giving every account a new password"
  [ -s "$RESTORE" ] && gzip -t "$RESTORE"
  gunzip -c "$RESTORE" | ssh "$TARGET" 'docker exec -i yudao-mysql-1 sh -c '"'"'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" 2>/dev/null'"'"
  ssh "$TARGET" 'docker exec yudao-mysql-1 sh -c '"'"'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" ruoyi-vue-pro -e "DELETE FROM system_oauth2_access_token; DELETE FROM system_oauth2_refresh_token;" 2>/dev/null'"'"
  ssh "$TARGET" 'docker exec yudao-redis-1 redis-cli FLUSHDB >/dev/null'
  ssh "$TARGET" 'docker restart yudao-server-1 >/dev/null && i=0; until [ "$(curl -s -o /dev/null -w %{http_code} http://127.0.0.1:48080/admin-api/system/tenant/get-id-by-name?name=x)" = 200 ]; do i=$((i+1)); [ $i -lt 96 ] || exit 1; sleep 5; done'
  PORT=48180
  command ssh -f -N -o ExitOnForwardFailure=yes -L "$PORT:127.0.0.1:48080" \
    -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "$TARGET"
  trap 'rm -rf "$STAGING"; pkill -f "ssh -f -N -o ExitOnForwardFailure=yes -L $PORT:127.0.0.1:48080" || true' EXIT
  (cd "$DEPLOY" && "$PYTHON" harden.py --base "http://127.0.0.1:$PORT" --out "$CREDENTIALS")
  ssh "$TARGET" 'docker exec -i yudao-mysql-1 sh -c '"'"'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" ruoyi-vue-pro 2>/dev/null'"'" < "$DEPLOY/harden.sql"
  echo "restored; the new passwords are in $CREDENTIALS"
fi
