# Contributing

Web Operation Agent is an alpha project. Contributions should strengthen the application-neutral server/runtime boundary, make the narrow supported paths easier to reproduce, or add evidence for a clearly stated product claim.

Read [README.md](README.md) for the implemented scope and [SECURITY.md](SECURITY.md) before changing browser transport, identity, approvals, or write behavior.

## Set up a development environment

The package supports Python 3.12 and 3.13. A POSIX shell is used by the documented commands. Node.js is needed to execute all embedded-runtime tests.

```bash
python3 -m venv .venv    # Python 3.12 or 3.13
. .venv/bin/activate
python -m pip install --upgrade pip
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 python -m pip install --editable .
```

Most unit and protocol tests do not launch a browser. To run browser-backed tests locally, install the matching Chromium binary separately:

```bash
python -m playwright install chromium
```

Some PageController integration tests also require that upstream component. Point
`WOA_PAGE_CONTROLLER_MODULE` at the installed module explicitly when running them:

```bash
WOA_PAGE_CONTROLLER_MODULE=/absolute/path/to/page-controller.js \
  python src/web_operation_agent/tests/run_all.py -k test_embedded_runtime.py
```

The test runner skips those tests when the variable is unset or does not name a file;
report the skip instead of treating it as a pass.

## Run tests

Run the guarded product suite:

```bash
python src/web_operation_agent/tests/run_all.py
```

Run a focused module by filename pattern:

```bash
python src/web_operation_agent/tests/run_all.py -k test_product.py
python src/web_operation_agent/tests/run_all.py -k test_twenty.py
```

The runner redirects accidental test usage records to scratch storage and verifies that the real model-spend ledger remains unchanged. Unit tests must not make paid model calls or contact production services.

For packaging changes, build a wheel:

```bash
python -m pip wheel --no-deps --wheel-dir dist .
```

The continuous-integration workflow installs the package, checks required package resources, imports the installed wheel outside the checkout, and runs the product suite.

Run the independent Twenty evaluator foundation tests separately:

```bash
python -m unittest discover -s evals/twenty/tests -v
```

These tests are offline and use a fake command runner. They do not show that the fixture
commands work against a real Docker-backed Twenty deployment.

## Design expectations

Keep the following boundaries intact:

- the server owns planning, credentials, knowledge, policy, approvals, and audit state;
- the browser runtime observes and invokes a fixed set of generic page tools;
- application behavior enters through adapters, Skills, typed tools, and application-owned verification;
- live page and API facts override static knowledge;
- mutations are opt-in, explicitly confirmed, single-dispatch operations;
- an uncertain mutation is terminal and must not be automatically retried.

When adding an application adapter, start with the smallest useful vertical. Keep credentials outside the repository, use a delegated test actor, return only the fields the Agent needs, expose a bounded route set, and leave mutations disabled by default.

## Tests for sensitive changes

Add focused failure-path coverage when changing:

- session capabilities, origin checks, message validation, or redaction;
- action claiming, reconnection, freshness, cancellation, or terminal delivery;
- write proposals, approval consumption, duplicate checks, dispatch, or readback;
- Skill parsing, tool schemas, packaged resources, or application adapters;
- ledger records or public serialization.

Prefer deterministic state checks over model self-assessment. Tests should establish the postcondition and the set of state that must remain unchanged.

## Documentation

Keep public documentation concise and reproducible. Update both [README.md](README.md) and [README.zh-CN.md](README.zh-CN.md) when a user-facing capability, command, requirement, or limitation changes.

Do not commit raw run data, credentials, downloaded applications, local cache paths, or disposable design notes. A benchmark result belongs in the public repository only when its task set, baseline, metric, environment, and stopping rule are reproducible.

## Submitting a change

Before handing off a change:

1. Inspect `git status` and the full diff.
2. Run the focused tests and the full product suite.
3. Build the wheel when package code or resources changed.
4. Record skipped checks and environment-dependent coverage.
5. Keep the change focused; do not include unrelated cleanup or generated output.
6. Describe the concrete before/after behavior and the validation performed.

Report security-sensitive findings through the private process in [SECURITY.md](SECURITY.md), not through a public issue containing exploit details.
