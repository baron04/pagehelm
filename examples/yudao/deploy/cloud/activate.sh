#!/bin/sh
# Run over SSH after upload. A failed build never switches current; a failed activation
# restores the previous release. No schema migration, data restore or volume removal.
set -eu
RELEASE=${1:?release}
ORIGIN=${2:-}
cd /opt/yudao
exec 9>.deploy.lock
flock -n 9 || { echo 'another deployment is activating' >&2; exit 1; }
PREVIOUS=$(readlink current 2>/dev/null || true)
ACTIVE="releases/$RELEASE"
export YUDAO_CACHE="/opt/yudao/$ACTIVE"
export COMPOSE_PROFILES=""
if [ -f agent.env ]; then export COMPOSE_PROFILES=agent; fi
compose() {
  docker compose -p yudao --env-file /opt/yudao/.env \
    -f "$ACTIVE/compose.yaml" -f "$ACTIVE/compose.cloud.yaml" "$@"
}
# Build before changing the symlink so a build error leaves the running release alone.
if [ -n "$COMPOSE_PROFILES" ] && ! compose build agent; then
  echo "release $RELEASE build failed; current is unchanged" >&2
  exit 1
fi
JAR=server/yudao-server/target/yudao-server.jar
RECREATE=""
if [ -z "$PREVIOUS" ] || [ "$(sha256sum < "$PREVIOUS/$JAR")" != "$(sha256sum < "$ACTIVE/$JAR")" ]; then
  RECREATE=--force-recreate
fi
EXPECTED=$(cat "$ACTIVE/runtime.sha256")
ln -sfn "$ACTIVE" current.next && mv -T current.next current
ACTIVE=current
export YUDAO_CACHE=/opt/yudao/current
up() {
  compose up -d --remove-orphans || return 1
  if [ -n "$RECREATE" ]; then
    compose up -d --force-recreate --no-deps server || return 1
  fi
}
asset_matches() {
  actual=$(curl --fail --silent --show-error --max-time 10 "$1" | sha256sum | cut -d ' ' -f 1)
  [ -n "$EXPECTED" ] && [ "$actual" = "$EXPECTED" ]
}
healthy() {
  deadline=$(($(date +%s) + 480))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl --fail --silent --max-time 5 -o /dev/null \
         'http://127.0.0.1:48080/admin-api/system/tenant/get-id-by-name?name=x'; then
      if [ -z "$COMPOSE_PROFILES" ] || {
        curl --fail --silent --max-time 5 -o /dev/null http://127.0.0.1:48091/readyz &&
        curl --fail --silent --max-time 5 -o /dev/null http://127.0.0.1:48092/healthz &&
        { [ -z "$EXPECTED" ] || asset_matches http://127.0.0.1:48091/v1/runtime/assistant.js; }
      }; then
        if [ -z "$ORIGIN" ] || {
          curl --fail --silent --max-time 10 -o /dev/null "$ORIGIN/" &&
          { [ -z "$COMPOSE_PROFILES" ] || [ -z "$EXPECTED" ] || asset_matches "$ORIGIN/woa-agent/v1/runtime/assistant.js"; }
        }; then return 0; fi
      fi
    fi
    sleep 5
  done
  return 1
}
if up && healthy; then
  echo "release $RELEASE is serving; backend, assistant and runtime verified"
  # Keep current, the rollback target, and one other release, even after failed uploads.
  kept=0
  for dir in $(ls -1dt releases/*); do
    [ "$dir" = "releases/$RELEASE" ] && continue
    [ "$dir" = "$PREVIOUS" ] && continue
    kept=$((kept + 1))
    [ "$kept" -le 1 ] || rm -rf "$dir"
  done
  exit 0
fi
echo "release $RELEASE failed; restoring ${PREVIOUS:-nothing}" >&2
if [ -n "$PREVIOUS" ]; then
  ln -sfn "$PREVIOUS" current.next && mv -T current.next current
  # Older releases may not yet have a manifest; check their services without claiming
  # that the new release's runtime hash belongs to them.
  EXPECTED=""
  if [ -f current/runtime.sha256 ]; then EXPECTED=$(cat current/runtime.sha256); fi
  if { [ -z "$COMPOSE_PROFILES" ] || compose build agent; } && up && healthy; then
    echo "restored $PREVIOUS" >&2
  else
    echo "rollback health check failed; inspect $PREVIOUS on the host" >&2
  fi
else
  rm -f current
fi
exit 1
