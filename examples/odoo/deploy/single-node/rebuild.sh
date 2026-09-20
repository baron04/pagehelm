#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../../../.." && pwd)"
cd "$repo_root"

export WOA_PROJECT_NAME="${WOA_PROJECT_NAME:-woa-odoo-pilot}"
export WOA_PRIVATE_DIR="${WOA_PRIVATE_DIR:-$repo_root/.private/odoo-single-node}"
export WOA_PUBLIC_ORIGIN="${WOA_PUBLIC_ORIGIN:-https://localhost:18443}"
export WOA_HTTPS_PORT="${WOA_HTTPS_PORT:-18443}"
export WOA_ALLOW_MUTATIONS="${WOA_ALLOW_MUTATIONS:-1}"
export WOA_BUILD_REVISION="$(git rev-parse HEAD)"

compose=(docker compose -f examples/odoo/deploy/single-node/docker-compose.yml)

"${compose[@]}" build agent odoo
"${compose[@]}" up -d --no-deps --force-recreate agent
"${compose[@]}" --profile setup run --rm odoo-init
"${compose[@]}" up -d --no-deps --force-recreate odoo gateway

healthy=0
for _attempt in {1..60}; do
  if curl -fsSk "$WOA_PUBLIC_ORIGIN/assistant/healthz"; then
    healthy=1
    break
  fi
  sleep 2
done
if [[ "$healthy" != 1 ]]; then
  echo "assistant health check did not recover within 120 seconds" >&2
  exit 1
fi

"${compose[@]}" exec -T agent python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8766/readyz').read().decode())"
