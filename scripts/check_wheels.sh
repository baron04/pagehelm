#!/usr/bin/env bash
# Build distributions and verify installation outside the checkout.
set -uo pipefail
ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "${ROOT}"
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "${ROOT}/.venv/bin/python" ]]; then PYTHON="${ROOT}/.venv/bin/python"; else PYTHON=python3; fi
fi
build_and_check_wheel() {
  # setuptools may otherwise copy deleted modules from an ignored build/lib tree into a new
  # wheel. A release gate must build from the current source tree, not incremental residue.
  rm -rf dist build src/*.egg-info
  rm -rf examples/odoo/dist examples/odoo/build \
    examples/odoo/src/*.egg-info
  rm -rf examples/yudao/dist examples/yudao/build examples/yudao/src/*.egg-info
  "${PYTHON}" -m pip wheel --no-deps --quiet --wheel-dir dist . || return 1
  "${PYTHON}" -m pip wheel --no-deps --quiet \
    --wheel-dir examples/odoo/dist examples/odoo \
    || return 1
  "${PYTHON}" -m pip wheel --no-deps --quiet \
    --wheel-dir examples/yudao/dist examples/yudao || return 1
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
  # The adapter import reaches core's declared runtime dependencies. Install those in
  # the isolated environment so this probe tests the packaged distributions rather
  # than failing because the smoke venv itself is missing PyYAML/jsonschema.
  env -u PYTHONPATH "${smoke_venv}/bin/python" -m pip install "${core_wheel}" \
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
     "query_customer_invoices", "prepare_customer_form", "discover_entities",
     "describe_entity", "query_entity_records", "resolve_record_reference",
     "find_page_entries", "resolve_goal_candidates"})
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
  local yudao_wheel
  yudao_wheel="$("${PYTHON}" -c \
    'import glob, os; print(os.path.abspath(glob.glob("examples/yudao/dist/*.whl")[0]))')"
  env -u PYTHONPATH "${smoke_venv}/bin/python" -m pip install --no-deps \
    "${yudao_wheel}" || return 1
  ( cd "${smoke_cwd}" && env -u PYTHONPATH \
      "${smoke_venv}/bin/python" -I - <<'PYUD'
from web_operation_agent_yudao.application import YudaoApplication
from web_operation_agent_yudao.client import YudaoAccount
from web_operation_agent_yudao.native_server import YudaoHost
from web_operation_agent_yudao.conventions import REFERENCE_CONVENTIONS
from web_operation_agent_yudao.repository import repository_source
from importlib.resources import files
app = YudaoApplication("http://127.0.0.1:48080", YudaoAccount("synthetic", "synthetic"), {})
assert app.business_tools.tool_names == frozenset({
    "discover_entities", "describe_entity", "query_entity_records",
    "resolve_record_reference", "find_page_entries", "resolve_goal_candidates"})
assert app.business_tools.mutation_tool_names == frozenset()
assert REFERENCE_CONVENTIONS.is_file() and YudaoHost
assert files("web_operation_agent_yudao").joinpath("policies/repository.json").is_file()
assert not repository_source(None, "").runtime_source
print("the Yudao wheel composes generic reads with its conventions and source policy outside the checkout")
PYUD
  ) || return 1
  rm -rf "${smoke_venv}" "${smoke_cwd}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  build_and_check_wheel
fi
