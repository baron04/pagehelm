#!/usr/bin/env bash
#
# One entry point for every gate that must hold before a build is called qualified.
#
# CI runs this same script, so a gate cannot pass locally and be missing in CI, or vice
# versa. Two rules matter more than the list of suites:
#
#   * A gate that cannot run is never reported as a pass. The browser and live-Odoo gates
#     print SKIPPED or BLOCKED with the exact prerequisite that is absent, and `--require-*`
#     turns that absence into a failure for environments that must have it.
#   * Any required failure makes this script exit non-zero. There is no summary line that
#     says "qualified" while a section above it failed.
#
# Usage:
#   scripts/qualify.sh [--require-browser] [--require-live] [--skip-wheel] [--reference-odoo]
#
# Live-Odoo prerequisites (all of them, or the gate reports BLOCKED):
#   WOA_ODOO_BASE_URL WOA_ODOO_API_KEY_FILE WOA_ODOO_STORAGE_STATE
#   WOA_ODOO_PAGE_CONTROLLER_MODULE
#   optionally WOA_ODOO_SOURCE_ROOT + WOA_ODOO_SOURCE_RELEASE, which add the source gate
#   optionally WOA_ODOO_FIXTURE_CONTAINER + WOA_ODOO_FIXTURE_DATABASE +
#     WOA_ODOO_FIXTURE_SNAPSHOT + WOA_ODOO_FIXTURE_RECORD, which make the gate refuse to
#     run against a library that has drifted off its recorded baseline
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
. "${ROOT}/scripts/python.sh"

# What is being qualified, said first and last. A result is evidence for one commit in one
# checkout; a run recorded against the wrong one qualified nothing (P7.5: four evening runs
# were made in a worktree left on an old branch and recorded against main).
tested_commit="$(git -C "${ROOT}" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
tested_branch="$(git -C "${ROOT}" symbolic-ref --quiet --short HEAD 2>/dev/null || echo detached)"
if [[ -n "$(git -C "${ROOT}" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
  tested_tree="modified"
else
  tested_tree="clean"
fi
tested_what="commit ${tested_commit} (${tested_branch}), tracked files ${tested_tree}, checkout ${ROOT}"
printf 'Qualifying %s\n' "${tested_what}"

require_browser=0
require_live=0
skip_wheel=0
reference_odoo=0
for arg in "$@"; do
  case "${arg}" in
    --require-browser) require_browser=1 ;;
    --require-live) require_live=1 ;;
    --skip-wheel) skip_wheel=1 ;;
    --reference-odoo) reference_odoo=1 ;;
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

export PYTHONPATH="${ROOT}/src:${ROOT}/examples/odoo/src:${ROOT}/examples/yudao/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

# ---------------------------------------------------------------- offline gates
run_gate "core distribution boundary" 1 "${PYTHON}" scripts/check_core_boundary.py
# Offline on purpose: it reads what is committed. Regenerating the lock and the SBOM needs
# the index and is a deliberate release step, `scripts/lock_and_sbom.py --write`.
run_gate "dependency lock, SBOM and licence notice" 1 \
  "${PYTHON}" scripts/lock_and_sbom.py --check
# ---------------------------------------------------------------- frontend gate
# First among the suites that need it: the panel and `assistant.js` are build artifacts,
# and the product suite's Runtime conformance tests read `assistant.js`. Built after them,
# those tests would read whatever an earlier build left behind, or nothing at all.
# The assistant panel is built here, not committed. Like the PageAgent bundle, a checkout
# without the toolchain is BLOCKED with the prerequisite named; it never reuses an unverified
# build and never reports the panel path as passed.
printf '\n\033[1m── assistant panel ──\033[0m\n'
panel_ready=0
if ! command -v npm >/dev/null 2>&1; then
  record "assistant panel build" BLOCKED "npm is unavailable"
  [[ "${require_browser}" == "1" ]] && failed+=("assistant panel build")
elif [[ ! -d "${ROOT}/frontend/node_modules" ]]; then
  record "assistant panel build" BLOCKED "run npm ci in frontend"
  [[ "${require_browser}" == "1" ]] && failed+=("assistant panel build")
elif ( cd "${ROOT}/frontend" && npm run lint && npm test && npm run build ) \
     && "${PYTHON}" scripts/build_panel.py --skip-build \
     && "${PYTHON}" scripts/build_runtime.py --skip-build; then
  record "assistant panel build" PASS ""
  panel_ready=1
else
  record "assistant panel build" FAIL "lint, unit tests or bundle failed"
  failed+=("assistant panel build")
fi

run_gate "product suite" 1 "${PYTHON}" src/web_operation_agent/tests/run_all.py --without-browser
run_gate "Contacts contracts (no Chromium)" 1 "${PYTHON}" src/web_operation_agent/tests/run_all.py \
  --start-dir testapps/contacts/tests --top-level . --without-browser
if [[ "${reference_odoo}" == "1" || "${require_live}" == "1" ]]; then
  run_gate "Odoo reference adapter" 1 "${PYTHON}" -m unittest discover \
    -s examples/odoo/src/web_operation_agent_odoo/tests -t examples/odoo/src
else
  record "Odoo reference adapter" SKIPPED "explicit --reference-odoo only"
fi
run_gate "yudao example and host suite (offline)" 1 "${PYTHON}" -m unittest discover \
  -s examples/yudao/src/web_operation_agent_yudao/tests \
  -t examples/yudao/src
run_gate "Yudao evaluation apparatus (offline)" 1 "${PYTHON}" -m unittest discover \
  -s evals/yudao/tests -t .
# Historical comparison/evaluation tools are not product dependencies.
if [[ "${reference_odoo}" == "1" ]]; then
  run_gate "Odoo source qualification apparatus" 1 "${PYTHON}" -m unittest discover -s evals/odoo_source/tests -t .
  run_gate "Odoo evaluator apparatus" 1 "${PYTHON}" scripts/offline_suite.py --start-dir evals/odoo/tests --top-level .
fi
# Runtime needs this dependency, not the PageAgent comparison host or baseline bundle.
pageagent_ready=0
if [[ -f "${ROOT}/evals/pageagent/node_modules/@page-agent/page-controller/dist/lib/page-controller.js" ]]; then
  pageagent_ready=1
fi

# ---------------------------------------------------------------- packaging gate
source "${ROOT}/scripts/check_wheels.sh"

if [[ "${skip_wheel}" == "1" ]]; then
  record "wheel build and contents" SKIPPED "--skip-wheel"
else
  run_gate "wheel build and contents" 1 build_and_check_wheel
fi

# ---------------------------------------------------------------- browser gate
# Self-contained once the deterministic bundle and Chromium are available. Run with the
# suite's required flag whenever prerequisites are present, so a real
# test failure can never be downgraded to an optional skip.
chromium_ready=0
if ! "${PYTHON}" - <<'PY'
# Probed here rather than imported from a test module: the gate must not depend on any one
# example staying where it is, and a moved import used to read as "Chromium is unavailable".
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        playwright.chromium.launch(headless=True).close()
except Exception:
    raise SystemExit(1)
raise SystemExit(0)
PY
then
  record "synthetic Contacts Chromium qualification" BLOCKED "Chromium is unavailable"
  [[ "${require_browser}" == "1" ]] && failed+=("synthetic Contacts Chromium qualification")
else
  chromium_ready=1
  run_gate "synthetic Contacts Chromium qualification" 1 \
    env WOA_REQUIRE_MINIMAL_DEMO_BROWSER=1 PYTHONPATH="${ROOT}/src:${ROOT}" "${PYTHON}" \
    src/web_operation_agent/tests/run_all.py --start-dir testapps/contacts/tests --top-level . \
      --only-browser --strict-skips
fi

# The panel's own Chromium checks: every state, the confirmation card's keyboard and narrow
# layouts, and that it still constructs under a strict CSP with Trusted Types.
if [[ "${panel_ready}" != "1" ]]; then
  record "assistant panel Chromium qualification" BLOCKED "the panel was not built"
  [[ "${require_browser}" == "1" ]] && failed+=("assistant panel Chromium qualification")
elif [[ "${chromium_ready}" != "1" ]]; then
  record "assistant panel Chromium qualification" BLOCKED "Chromium is unavailable"
  [[ "${require_browser}" == "1" ]] && failed+=("assistant panel Chromium qualification")
else
  # Written beside the panel, against the built Runtime. Node Playwright brings its own
  # Chromium (npx playwright install). Storybook is an independent showcase.
  run_gate "assistant panel Chromium qualification" 1 \
    bash -c 'cd "$1/frontend" && npx playwright test' _ "${ROOT}"
fi

page_controller_module="${ROOT}/evals/pageagent/node_modules/@page-agent/page-controller/dist/lib/page-controller.js"
runtime_blocked() {
  local gate
  for gate in "embedded Runtime Chromium qualification" "assistant lifecycle faults (I1, I2)"; do
    record "${gate}" BLOCKED "$1"
    [[ "${require_browser}" == "1" ]] && failed+=("${gate}")
  done
}
if [[ "${pageagent_ready}" != "1" ]]; then
  runtime_blocked "the pinned PageController dependency was not qualified"
elif [[ ! -f "${page_controller_module}" ]]; then
  runtime_blocked "PageController module is absent: ${page_controller_module}"
elif [[ "${chromium_ready}" != "1" ]]; then
  runtime_blocked "Chromium is unavailable"
else
  run_gate "embedded Runtime Chromium qualification" 1 \
    env WOA_PAGE_CONTROLLER_MODULE="${page_controller_module}" "${PYTHON}" \
    src/web_operation_agent/tests/run_all.py --only-browser --strict-skips \
    --exclude "*.test_assistant_lifecycle.*"
  run_gate "assistant lifecycle faults (I1, I2)" 1 \
    env WOA_PAGE_CONTROLLER_MODULE="${page_controller_module}" "${PYTHON}" \
    src/web_operation_agent/tests/run_all.py --module web_operation_agent.tests.transport.test_assistant_lifecycle \
    --only-browser --strict-skips
fi

# ---------------------------------------------------------------- live Odoo
# The main application's gate is model-free on purpose: two scripted cases through the real
# product turn, in real Chromium, against a real Odoo. A pass says the apparatus holds; it
# says nothing about a model, which is what makes a failure here worth acting on.
printf '\n\033[1m── live Odoo qualification ──\033[0m\n'
odoo_vars=(WOA_ODOO_BASE_URL WOA_ODOO_API_KEY_FILE WOA_ODOO_STORAGE_STATE
           WOA_ODOO_PAGE_CONTROLLER_MODULE)
# Optional: with both set, the gate additionally requires the source treatment to be
# non-empty, which is the check that catches a compiled map nothing consumes.
odoo_source=()
while IFS= read -r value; do odoo_source+=("${value}"); done < <(
  "${PYTHON}" -c '
import os, sys
from pathlib import Path
from web_operation_agent.modeling.client import load_env
root = Path(sys.argv[1])
configured = os.environ.get("WOA_MODEL_ENV", "").strip()
loaded = load_env(Path(configured).expanduser() if configured else root / ".env")
for name in sys.argv[2:]:
    print(str(os.environ[name] if name in os.environ else loaded.get(name, "")).strip())
' "${ROOT}" WOA_ODOO_SOURCE_ROOT WOA_ODOO_SOURCE_RELEASE
)
# The resettable fixture. Without it the gate still runs, but nothing stops it measuring
# against a library some earlier run wrote to -- which is how G2 left a drifted baseline.
odoo_fixture=()
while IFS= read -r value; do odoo_fixture+=("${value}"); done < <(
  "${PYTHON}" -c '
import os, sys
from pathlib import Path
from web_operation_agent.modeling.client import load_env
root = Path(sys.argv[1])
configured = os.environ.get("WOA_MODEL_ENV", "").strip()
loaded = load_env(Path(configured).expanduser() if configured else root / ".env")
for name in sys.argv[2:]:
    print(str(os.environ[name] if name in os.environ else loaded.get(name, "")).strip())
' "${ROOT}" WOA_ODOO_FIXTURE_CONTAINER WOA_ODOO_FIXTURE_DATABASE \
    WOA_ODOO_FIXTURE_SNAPSHOT WOA_ODOO_FIXTURE_RECORD
)
odoo_values=()
while IFS= read -r value; do odoo_values+=("${value}"); done < <(
  "${PYTHON}" -c '
import os, sys
from pathlib import Path
from web_operation_agent.modeling.client import load_env
root = Path(sys.argv[1])
configured = os.environ.get("WOA_MODEL_ENV", "").strip()
loaded = load_env(Path(configured).expanduser() if configured else root / ".env")
for name in sys.argv[2:]:
    print(str(os.environ[name] if name in os.environ else loaded.get(name, "")).strip())
' "${ROOT}" "${odoo_vars[@]}"
)
odoo_missing=()
for ((index=0; index<${#odoo_vars[@]}; index++)); do
  [[ -z "${odoo_values[${index}]:-}" ]] && odoo_missing+=("${odoo_vars[${index}]}")
done
if (( ${#odoo_missing[@]} )); then
  echo "BLOCKED: no live Odoo environment here."
  printf '  unset: %s\n' "${odoo_missing[*]}"
  echo "  This gate did not run. It is not a product failure and not a pass."
  record "live Odoo qualification" BLOCKED "unset: ${odoo_missing[*]}"
  if [[ "${require_live}" == "1" ]]; then failed+=("live Odoo qualification"); fi
elif [[ "${chromium_ready}" != "1" ]]; then
  record "live Odoo qualification" BLOCKED "Chromium is unavailable"
  if [[ "${require_live}" == "1" ]]; then failed+=("live Odoo qualification"); fi
else
  echo "READY: every prerequisite is present; running the live qualification."
  odoo_out="$(mktemp -d)"
  odoo_source_args=()
  if [[ -n "${odoo_source[0]:-}" && -n "${odoo_source[1]:-}" ]]; then
    odoo_source_args=(--source-root "${odoo_source[0]}"
                      --source-release "${odoo_source[1]}")
  fi
  odoo_fixture_args=()
  if [[ -n "${odoo_fixture[0]:-}" && -n "${odoo_fixture[1]:-}" \
        && -n "${odoo_fixture[2]:-}" && -n "${odoo_fixture[3]:-}" ]]; then
    odoo_fixture_args=(--fixture-container "${odoo_fixture[0]}"
                       --fixture-database "${odoo_fixture[1]}"
                       --fixture-snapshot "${odoo_fixture[2]}"
                       --fixture-record "${odoo_fixture[3]}")
  else
    echo "  note: no resettable fixture configured; this run cannot tell a clean library"
    echo "        from one an earlier run wrote to."
  fi
  if "${PYTHON}" -m web_operation_agent_odoo.demo.qualify \
      --base-url "${odoo_values[0]}" --api-key "${odoo_values[1]}" \
      --storage-state "${odoo_values[2]}" \
      --page-controller-module "${odoo_values[3]}" \
      "${odoo_source_args[@]}" "${odoo_fixture_args[@]}" \
      --output "${odoo_out}/odoo-live.json"; then
    record "live Odoo qualification" PASS "artifact ${odoo_out}/odoo-live.json"
  else
    record "live Odoo qualification" FAIL "see ${odoo_out}"
    failed+=("live Odoo qualification")
  fi
  odoo_form_tls_args=()
  if [[ "${WOA_ODOO_FORM_IGNORE_BROWSER_HTTPS_ERRORS:-0}" == "1" ]]; then
    odoo_form_tls_args=(--ignore-browser-https-errors)
  fi
  if "${PYTHON}" -m web_operation_agent_odoo.demo.form_qualify \
      --base-url "${odoo_values[0]}" --api-key "${odoo_values[1]}" \
      --storage-state "${odoo_values[2]}" \
      --page-controller-module "${odoo_values[3]}" \
      "${odoo_form_tls_args[@]}" \
      --output "${odoo_out}/odoo-form-live.json"; then
    record "live Odoo form qualification" PASS \
      "artifact ${odoo_out}/odoo-form-live.json"
  else
    record "live Odoo form qualification" FAIL "see ${odoo_out}"
    failed+=("live Odoo form qualification")
  fi
fi

# ---------------------------------------------------------------- summary
printf '\n\033[1m── qualification summary ──\033[0m\n'
printf '  %s\n' "${tested_what}"
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
