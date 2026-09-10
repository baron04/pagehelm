#!/usr/bin/env bash
#
# One entry point for every gate that must hold before a build is called qualified.
#
# CI runs this same script, so a gate cannot pass locally and be missing in CI, or vice
# versa. Two rules matter more than the list of suites:
#
#   * A gate that cannot run is never reported as a pass. The browser and live-Twenty gates
#     print SKIPPED or BLOCKED with the exact prerequisite that is absent, and `--require-*`
#     turns that absence into a failure for environments that must have it.
#   * Any required failure makes this script exit non-zero. There is no summary line that
#     says "qualified" while a section above it failed.
#
# Usage:
#   scripts/qualify.sh [--require-browser] [--require-live] [--skip-wheel]
#
# Live-Twenty prerequisites (all of them, or the gate reports BLOCKED):
#   WOA_TWENTY_BASE_URL WOA_TWENTY_STORAGE_STATE WOA_TWENTY_OAUTH_TOKEN
#   WOA_TWENTY_OAUTH_REGISTRATION WOA_TWENTY_PAGE_CONTROLLER_MODULE
#   WOA_TWENTY_DB_CONTAINER WOA_TWENTY_DB_NAME WOA_TWENTY_DB_USER
#   WOA_TWENTY_WORKSPACE_SCHEMA
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "${ROOT}/.venv/bin/python" ]]; then PYTHON="${ROOT}/.venv/bin/python"; else PYTHON="python3"; fi
fi

require_browser=0
require_live=0
skip_wheel=0
for arg in "$@"; do
  case "${arg}" in
    --require-browser) require_browser=1 ;;
    --require-live) require_live=1 ;;
    --skip-wheel) skip_wheel=1 ;;
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: ${arg}" >&2; exit 2 ;;
  esac
done
[[ "${WOA_REQUIRE_PAGEAGENT_BROWSER:-}" == "1" ]] && require_browser=1

failed=()
results=()

record() { results+=("$1|$2|$3"); }

run_gate() {   # run_gate <name> <required:0|1> <command...>
  local name="$1" required="$2"; shift 2
  printf '\n\033[1m── %s ──\033[0m\n' "${name}"
  if "$@"; then
    record "${name}" PASS ""
    return 0
  else
    local code=$?
    if [[ "${required}" == "1" ]]; then
      record "${name}" FAIL "exit ${code}"
      failed+=("${name}")
    else
      record "${name}" SKIPPED "exit ${code}, not required here"
    fi
    return "${code}"
  fi
}

export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

# ---------------------------------------------------------------- offline gates
run_gate "product suite" 1 "${PYTHON}" src/web_operation_agent/tests/run_all.py
run_gate "evals/twenty foundation" 1 "${PYTHON}" -m unittest discover -s evals/twenty/tests
run_gate "PageAgent host and runner (offline)" 1 "${PYTHON}" -m unittest \
  evals.pageagent.test_host evals.pageagent.test_business_host \
  evals.pageagent.test_twenty_runner evals.pageagent.test_qualify_twenty_runner
run_gate "WOA runner and schedule driver (offline)" 1 "${PYTHON}" -m unittest \
  evals.woa.test_chat_port evals.woa.test_assembler evals.woa.test_twenty_runner \
  evals.woa.test_qualify_schedule

# ---------------------------------------------------------------- PageAgent bundle gate
# `npm ci` is environment preparation. Reproducibility verification and the build itself are
# qualification gates, so they live here rather than in the CI workflow. A local checkout
# without the installed pinned dependencies is BLOCKED; it must never reuse an unverified old
# bundle and report the browser path as passed.
printf '\n\033[1m── PageAgent deterministic bundle ──\033[0m\n'
pageagent_ready=0
if ! command -v npm >/dev/null 2>&1; then
  record "PageAgent deterministic bundle" BLOCKED "npm is unavailable"
  [[ "${require_browser}" == "1" ]] && failed+=("PageAgent deterministic bundle")
elif [[ ! -d "${ROOT}/evals/pageagent/node_modules" ]]; then
  record "PageAgent deterministic bundle" BLOCKED \
    "run npm ci in evals/pageagent"
  [[ "${require_browser}" == "1" ]] && failed+=("PageAgent deterministic bundle")
elif ( cd "${ROOT}/evals/pageagent" && npm test && npm run build ); then
  record "PageAgent deterministic bundle" PASS ""
  pageagent_ready=1
else
  record "PageAgent deterministic bundle" FAIL "deterministic build failed"
  failed+=("PageAgent deterministic bundle")
fi

# ---------------------------------------------------------------- packaging gate
build_and_check_wheel() {
  # setuptools may otherwise copy deleted modules from an ignored build/lib tree into a new
  # wheel. A release gate must build from the current source tree, not incremental residue.
  rm -rf dist build src/*.egg-info
  "${PYTHON}" -m pip wheel --no-deps --quiet --wheel-dir dist . || return 1
  "${PYTHON}" - <<'PY' || return 1
import glob, zipfile
paths = glob.glob("dist/*.whl")
assert len(paths) == 1, paths
names = zipfile.ZipFile(paths[0]).namelist()
required = ["web_operation_agent/policies/model_options.json",
            "web_operation_agent/policies/ofbiz.json",
            "web_operation_agent/policies/prices.json",
            "web_operation_agent/integrations/ofbiz.py",
            "web_operation_agent/integrations/ofbiz_knowledge.py",
            "web_operation_agent/skills/twenty/companies-query.json",
            "web_operation_agent/skills/twenty/companies-create.json",
            "web_operation_agent/web_runtime/embedded.js",
            "web_operation_agent/web_runtime/trace_replay.html",
            "web_operation_agent_demo/assets/page_controller.js"]
missing = [name for name in required if name not in names]
assert not missing, f"wheel is missing {missing}"
retired = ["web_operation_agent/browser_worker.py", "web_operation_agent/cli.py"]
present = [name for name in retired if name in names]
assert not present, f"wheel contains retired browser executors: {present}"
assert "web_operation_agent/policies/enforcement.json" not in names, \
    "the enforcement policy must not ship in the wheel"
leaked = [name for name in names if "/tests/" in name]
assert not leaked, f"tests must not ship in the wheel: {leaked[:5]}"
print(f"wheel contents verified: {paths[0]}")
PY
  # Importing from outside the checkout is the part a path-shadowed test cannot prove.
  local wheel smoke_venv smoke_cwd
  wheel="$("${PYTHON}" -c 'import glob, os; print(os.path.abspath(glob.glob("dist/*.whl")[0]))')"
  smoke_venv="$(mktemp -d)/venv"
  smoke_cwd="$(mktemp -d)"
  "${PYTHON}" -m venv "${smoke_venv}" || return 1
  # Without -u PYTHONPATH pip finds the checkout's own metadata on the path, reports the
  # requirement as already satisfied, and installs nothing -- so the import below would prove
  # only that the checkout is importable, which is the opposite of what this gate is for.
  env -u PYTHONPATH "${smoke_venv}/bin/python" -m pip install --no-deps "${wheel}" \
    | grep -v "^\[notice\]" || return 1
  env -u PYTHONPATH "${smoke_venv}/bin/python" -c \
    "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('web_operation_agent') else 1)" \
    || { echo "the wheel did not install into the smoke venv" >&2; return 1; }
  ( cd "${smoke_cwd}" && env -u PYTHONPATH WOA_CHECKOUT="${ROOT}" \
      "${smoke_venv}/bin/python" -I - <<'PY'
import os, sys
from pathlib import Path
import web_operation_agent
from web_operation_agent.mutation import SQLiteMutationJournal
from web_operation_agent.sourcetree import SourceTree
from web_operation_agent.twenty import TwentyApplicationAdapter
from web_operation_agent_demo import MinimalContactsAdapter

installed = Path(web_operation_agent.__file__).resolve()
assert Path(sys.prefix).resolve() in installed.parents, installed
assert Path(os.environ["WOA_CHECKOUT"]).resolve() not in installed.parents, installed
root = Path.cwd().resolve()
assert SourceTree.load("ofbiz", root).root == root
common = dict(base_url="http://127.0.0.1:3000", token_path="tokens.json",
              registration_path="registration.json")
assert TwentyApplicationAdapter(**common).business_tools.tool_names == frozenset(
    {"query_companies"})
assert (TwentyApplicationAdapter(
    **common, allow_mutations=True,
    mutation_journal=SQLiteMutationJournal(root / "mutations.sqlite3"),
).business_tools.tool_names == frozenset({"query_companies", "create_company"}))
assert MinimalContactsAdapter
print("installed wheel imports and composes outside the checkout")
PY
  ) || return 1
  rm -rf "${smoke_venv}" "${smoke_cwd}"
}

if [[ "${skip_wheel}" == "1" ]]; then
  record "wheel build and contents" SKIPPED "--skip-wheel"
else
  run_gate "wheel build and contents" 1 build_and_check_wheel
fi

# ---------------------------------------------------------------- browser gate
# Self-contained once the deterministic bundle and Chromium are available; no Twenty is
# involved. Run with the suite's required flag whenever prerequisites are present, so a real
# test failure can never be downgraded to an optional skip.
if ! "${PYTHON}" - <<'PY'
from web_operation_agent.tests.test_minimal_demo import _chromium_launches
raise SystemExit(0 if _chromium_launches() else 1)
PY
then
  record "minimal demo Chromium qualification" BLOCKED "Chromium is unavailable"
  [[ "${require_browser}" == "1" ]] && failed+=("minimal demo Chromium qualification")
else
  run_gate "minimal demo Chromium qualification" 1 \
    env WOA_REQUIRE_MINIMAL_DEMO_BROWSER=1 "${PYTHON}" \
    src/web_operation_agent/tests/run_all.py -k test_minimal_demo.py
fi

browser_bundle="${WOA_PAGEAGENT_BUNDLE:-${ROOT}/.artifacts/evals/pageagent/pageagent-baseline.js}"
if [[ "${pageagent_ready}" != "1" ]]; then
  record "PageAgent Chromium qualification" BLOCKED \
    "the deterministic PageAgent bundle was not qualified"
  [[ "${require_browser}" == "1" ]] && failed+=("PageAgent Chromium qualification")
elif [[ ! -f "${browser_bundle}" ]]; then
  record "PageAgent Chromium qualification" BLOCKED "bundle is absent: ${browser_bundle}"
  [[ "${require_browser}" == "1" ]] && failed+=("PageAgent Chromium qualification")
elif ! "${PYTHON}" - <<'PY'
from evals.pageagent.test_twenty_runner_browser import _chromium_launches
raise SystemExit(0 if _chromium_launches() else 1)
PY
then
  record "PageAgent Chromium qualification" BLOCKED "Chromium is unavailable"
  [[ "${require_browser}" == "1" ]] && failed+=("PageAgent Chromium qualification")
else
  run_gate "PageAgent Chromium qualification" 1 \
    env WOA_REQUIRE_PAGEAGENT_BROWSER=1 "${PYTHON}" -m unittest \
    evals.pageagent.test_twenty_runner_browser
fi

# ---------------------------------------------------------------- live gate
# Reported as READY only when it actually ran a live qualification. A missing prerequisite is
# BLOCKED and names what is missing; it is never PASS, and never a product failure.
printf '\n\033[1m── live Twenty qualification ──\033[0m\n'
live_vars=(WOA_TWENTY_BASE_URL WOA_TWENTY_STORAGE_STATE WOA_TWENTY_OAUTH_TOKEN
           WOA_TWENTY_OAUTH_REGISTRATION WOA_TWENTY_PAGE_CONTROLLER_MODULE
           WOA_TWENTY_DB_CONTAINER WOA_TWENTY_DB_NAME WOA_TWENTY_DB_USER
           WOA_TWENTY_WORKSPACE_SCHEMA)
missing=()
for name in "${live_vars[@]}"; do
  [[ -z "${!name:-}" ]] && missing+=("${name}")
done

if (( ${#missing[@]} )); then
  echo "BLOCKED: no live Twenty environment here."
  printf '  unset: %s\n' "${missing[*]}"
  echo "  This gate did not run. It is not a product failure and not a pass."
  record "live Twenty qualification" BLOCKED "unset: ${missing[*]}"
  if [[ "${require_live}" == "1" ]]; then failed+=("live Twenty qualification"); fi
else
  echo "READY: every prerequisite is present; running the live qualification."
  live_out="$(mktemp -d)"
  live_ok=1
  "${PYTHON}" -m evals.woa.qualify_live \
    --base-url "${WOA_TWENTY_BASE_URL}" --storage-state "${WOA_TWENTY_STORAGE_STATE}" \
    --oauth-token "${WOA_TWENTY_OAUTH_TOKEN}" \
    --oauth-registration "${WOA_TWENTY_OAUTH_REGISTRATION}" \
    --page-controller-module "${WOA_TWENTY_PAGE_CONTROLLER_MODULE}" \
    --audit-ledger "${live_out}/woa-audit.jsonl" \
    --container "${WOA_TWENTY_DB_CONTAINER}" --database "${WOA_TWENTY_DB_NAME}" \
    --db-user "${WOA_TWENTY_DB_USER}" --workspace-schema "${WOA_TWENTY_WORKSPACE_SCHEMA}" \
    --output "${live_out}/woa-live.json" || live_ok=0
  if [[ "${live_ok}" == "1" ]]; then
    record "live Twenty qualification" PASS "artifact ${live_out}/woa-live.json"
  else
    record "live Twenty qualification" FAIL "see ${live_out}"
    failed+=("live Twenty qualification")
  fi
fi

# ---------------------------------------------------------------- summary
printf '\n\033[1m── qualification summary ──\033[0m\n'
for row in "${results[@]}"; do
  IFS='|' read -r name status note <<<"${row}"
  printf '  %-10s %s%s\n' "${status}" "${name}" "${note:+  (${note})}"
done

if (( ${#failed[@]} )); then
  printf '\nNOT QUALIFIED: %s\n' "${failed[*]}"
  exit 1
fi
printf '\nQualified for the gates that ran above. Any BLOCKED or SKIPPED gate is untested,\n'
printf 'not passed.\n'
exit 0
