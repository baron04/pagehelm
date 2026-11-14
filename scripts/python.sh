# Source after setting ROOT to the checkout containing this script.
if [ -z "${PYTHON:-}" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON="$ROOT/.venv/bin/python"
  elif command -v python3.13 >/dev/null 2>&1; then
    PYTHON=$(command -v python3.13)
  else
    echo '需要 Python 3.13；请按 README 创建 .venv。' >&2
    exit 2
  fi
fi
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'; then
  echo '需要 Python 3.13；请更新 .venv 或指定兼容的 PYTHON。' >&2
  exit 2
fi
export PYTHON
