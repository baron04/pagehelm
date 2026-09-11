#!/bin/sh
set -eu

EXAMPLE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$EXAMPLE_DIR/../.." && pwd)
if [ -n "${WOA_DEMO_PYTHON:-}" ]; then
  DEMO_PYTHON=$WOA_DEMO_PYTHON
elif [ -x "$REPO_DIR/.venv/bin/python" ]; then
  DEMO_PYTHON="$REPO_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  DEMO_PYTHON=python3.12
else
  DEMO_PYTHON=python3
fi

if ! command -v "$DEMO_PYTHON" >/dev/null 2>&1; then
  echo "Python 3.12 runtime is unavailable: $DEMO_PYTHON" >&2
  echo "Create .venv or set WOA_DEMO_PYTHON to the intended interpreter." >&2
  exit 2
fi

if [ -z "${WOA_MODEL_ENV:-}" ] && [ -f "$REPO_DIR/.env" ]; then
  WOA_MODEL_ENV="$REPO_DIR/.env"
  export WOA_MODEL_ENV
fi

PYTHONPATH="$REPO_DIR:$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

if [ "${1:-}" = "--check" ]; then
  shift
  exec "$DEMO_PYTHON" "$EXAMPLE_DIR/src/web_operation_agent_twenty/demo/preflight.py" "$@"
fi

if [ "${1:-}" = "--login" ]; then
  shift
  exec "$DEMO_PYTHON" "$EXAMPLE_DIR/src/web_operation_agent_twenty/demo/login.py" "$@"
fi

exec "$DEMO_PYTHON" "$EXAMPLE_DIR/src/web_operation_agent_twenty/demo/demo.py" "$@"
