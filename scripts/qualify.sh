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
#   scripts/qualify.sh [--require-browser] [--require-live] [--skip-wheel]
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

export PYTHONPATH="${ROOT}/src:${ROOT}/examples/odoo/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

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
elif ( cd "${ROOT}/frontend" && npm run lint && npm test && npm run build \
       && npm run build-storybook ) \
     && "${PYTHON}" scripts/build_panel.py --skip-build \
     && "${PYTHON}" scripts/build_runtime.py --skip-build; then
  record "assistant panel build" PASS ""
  panel_ready=1
else
  record "assistant panel build" FAIL "lint, unit tests, bundle or Storybook failed"
  failed+=("assistant panel build")
fi

run_gate "product suite" 1 "${PYTHON}" src/web_operation_agent/tests/run_all.py
run_gate "ordinary-browser host (offline)" 1 "${PYTHON}" -m unittest \
  testapps.contacts.tests.test_native_server.NativeContactsHostTests
run_gate "odoo example suite" 1 "${PYTHON}" -m unittest discover \
  -s examples/odoo/src/web_operation_agent_odoo/tests \
  -t examples/odoo/src
# P8 source qualification stays model-free.  The full 40-task run needs a deployment-owned
# extracted source tree and is therefore an operated gate; these tests keep the frozen
# manifest, citation drift probe, and cross-release diff apparatus in every repository run.
run_gate "Odoo source qualification apparatus (offline)" 1 "${PYTHON}" -m unittest discover \
  -s evals/odoo_source/tests -t .
# Citation resolution needs the deployed release's source, so the authoritative run of it is
# the live gate's `every_skill_citation_resolves_in_this_release`, not a skippable test.
# The P7.4 Odoo apparatus: ledger, outcome axes, gateway, task sets and seals, evaluator
# extraction, statistics and report. Offline by design; the live fixture qualification runs
# separately through `python -m evals.odoo.qualify` against the isolated evaluation stack.
# Sharded across processes: almost all of this suite waits on loopback servers rather than
# using a core, and the runner refuses its own result unless the shards ran every test the
# sequential discovery found. Run it with --shards 1 to reproduce a failure in one process.
run_gate "evals/odoo apparatus (offline)" 1 "${PYTHON}" scripts/offline_suite.py \
  --start-dir evals/odoo/tests --top-level . --shards "${OFFLINE_SHARDS:-4}"
# The structural apparatus and scorer, model-free. It belongs with the offline gates rather
# than the browser ones: the apparatus half needs no browser at all, and the probe half
# skips itself with a named prerequisite when the PageAgent bundle is absent.
run_gate "PageAgent host (offline)" 1 "${PYTHON}" -m unittest \
  evals.pageagent.test_host evals.pageagent.test_business_host

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
  rm -rf examples/odoo/dist examples/odoo/build \
    examples/odoo/src/*.egg-info
  "${PYTHON}" -m pip wheel --no-deps --quiet --wheel-dir dist . || return 1
  "${PYTHON}" -m pip wheel --no-deps --quiet \
    --wheel-dir examples/odoo/dist examples/odoo \
    || return 1
  "${PYTHON}" scripts/check_wheel_contents.py || return 1

  # Importing from outside the checkout is the part a path-shadowed test cannot prove. Core
  # is installed and probed alone first -- it must compose and run with no application
  # distribution present -- then the Odoo distribution is added on top of it.
  local core_wheel odoo_wheel smoke_venv smoke_cwd
  core_wheel="$("${PYTHON}" -c 'import glob, os; print(os.path.abspath(glob.glob("dist/*.whl")[0]))')"
  odoo_wheel="$("${PYTHON}" -c \
    'import glob, os; print(os.path.abspath(glob.glob("examples/odoo/dist/*.whl")[0]))')"
  smoke_venv="$(mktemp -d)/venv"
  smoke_cwd="$(mktemp -d)"
  "${PYTHON}" -m venv "${smoke_venv}" || return 1
  # Without -u PYTHONPATH pip finds the checkout's own metadata on the path, reports the
  # requirement as already satisfied, and installs nothing -- so the import below would prove
  # only that the checkout is importable, which is the opposite of what this gate is for.
  env -u PYTHONPATH "${smoke_venv}/bin/python" -m pip install --no-deps "${core_wheel}" \
    | grep -v "^\[notice\]" || return 1
  env -u PYTHONPATH "${smoke_venv}/bin/python" -c \
    "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('web_operation_agent') else 1)" \
    || { echo "the core wheel did not install into the smoke venv" >&2; return 1; }
  ( cd "${smoke_cwd}" && env -u PYTHONPATH WOA_CHECKOUT="${ROOT}" \
      "${smoke_venv}/bin/python" -I - <<'PY'
import os, sys
from pathlib import Path
import web_operation_agent
from web_operation_agent.business.mutation import SQLiteMutationJournal
from web_operation_agent.state.store import SQLiteSessionStore
from web_operation_agent.state.quota import SQLiteProviderQuota
from web_operation_agent.business.operations import reconcile
from web_operation_agent.sourcetree import SourceTree

installed = Path(web_operation_agent.__file__).resolve()
assert Path(sys.prefix).resolve() in installed.parents, installed
assert Path(os.environ["WOA_CHECKOUT"]).resolve() not in installed.parents, installed
assert SQLiteMutationJournal and SQLiteSessionStore and SQLiteProviderQuota
assert reconcile and SourceTree
# The framework ships no application and no example. Any of these resolving here would mean
# the core wheel is carrying something a framework has no business carrying.
for name in ("web_operation_agent_odoo", "web_operation_agent_demo", "examples", "testapps"):
    import importlib.util
    assert importlib.util.find_spec(name) is None, \
        f"core alone must not resolve {name}; applications and examples install separately"
print("core wheel alone imports and composes, and carries no application or example")
PY
  ) || return 1
  env -u PYTHONPATH "${smoke_venv}/bin/python" -m pip install --no-deps \
    "${odoo_wheel}" | grep -v "^\[notice\]" || return 1
  ( cd "${smoke_cwd}" && env -u PYTHONPATH \
      "${smoke_venv}/bin/python" -I - <<'PY'
from pathlib import Path
root = Path.cwd().resolve()
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent_odoo import ODOO_ROUTE_PORT, ODOO_SOURCE_POLICY
from web_operation_agent_odoo.adapter import OdooApplicationAdapter

# The main application must compose from its own wheel, with its packaged Skills and source
# policy present -- a missing package-data entry is exactly the failure a path-shadowed test
# cannot see.
assert ODOO_ROUTE_PORT.compose_contract_route("odoo", "sales") == "/odoo/sales"
assert ODOO_SOURCE_POLICY["repository_id"] == "odoo/odoo"
(root / "odoo-key.json").write_text(
    '{"database": "woa", "login": "grower", "api_key": "0123456789abcdef0123"}')
odoo = OdooApplicationAdapter(base_url="http://127.0.0.1:8069",
                              api_key_path=root / "odoo-key.json")
assert odoo.business_tools.tool_names == frozenset(
    {"query_sale_orders", "query_quotations", "query_customers", "query_products",
     "query_customer_invoices", "prepare_customer_form", "discover_odoo_models",
     "describe_odoo_model", "query_odoo_records"})
assert odoo.business_tools.mutation_tool_names == frozenset()
# The packaged write Skills have to be present in the wheel too, or a mutation-enabled
# deployment fails at composition time with a missing package-data entry -- exactly the
# failure a path-shadowed test cannot see.
from web_operation_agent.business.mutation import SQLiteMutationJournal as _Journal
writing = OdooApplicationAdapter(
    base_url="http://127.0.0.1:8069", api_key_path=root / "odoo-key.json",
    allow_mutations=True, mutation_journal=_Journal(root / "odoo-mutations.sqlite3"))
assert writing.business_tools.mutation_tool_names == frozenset(
    {"create_customer", "archive_customer", "confirm_quotation", "invoice_sale_order"})
assert odoo.source_capability().session_kwargs()["source_affordance_mode"] == "off"
assert isinstance(odoo.source_capability(), SourceCapability)
print("the Odoo distribution installs on top of core and composes outside the checkout")
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
    -m unittest testapps.contacts.tests.test_contacts_demo \
      testapps.contacts.tests.test_native_server.NativeContactsBrowserTests
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
  run_gate "assistant panel Chromium qualification" 1 "${PYTHON}" -m unittest \
    web_operation_agent.tests.transport.test_panel_ui
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
    env WOA_PAGE_CONTROLLER_MODULE="${page_controller_module}" "${PYTHON}" -m unittest \
    web_operation_agent.tests.transport.test_embedded_runtime.EmbeddedRuntimeTests.test_real_pagecontroller_survives_navigation_without_repeating_action \
    web_operation_agent.tests.transport.test_embedded_runtime.EmbeddedRuntimeTests.test_same_origin_iframe_form_navigation_is_observed_without_top_level_change \
    web_operation_agent.tests.transport.test_embedded_runtime.EmbeddedRuntimeTests.test_real_confirmation_card_posts_authenticated_user_decision \
    web_operation_agent.tests.transport.test_embedded_runtime.EmbeddedRuntimeTests.test_the_timeline_survives_a_full_page_navigation
  # D21's two invariants under injected faults: the host page never hears the assistant fail
  # (I1), and the assistant is never silently absent (I2).
  run_gate "assistant lifecycle faults (I1, I2)" 1 \
    env WOA_PAGE_CONTROLLER_MODULE="${page_controller_module}" "${PYTHON}" -m unittest \
    web_operation_agent.tests.transport.test_assistant_lifecycle
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
