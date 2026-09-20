# PageAgent evaluation bridge

This directory contains the generic PageAgent bridge used by the independent Odoo
evaluation. It owns the deterministic JavaScript bundle and the server-held completion and
business-tool hosts; it does not own application credentials, fixtures, task sets, or
expected answers.

`host.py` binds an opaque run token to one bounded completion client. `business_host.py`
derives tool schemas from the same `BusinessSkill` and `BusinessToolRuntime` contracts used
by Web Operation Agent. `bridge.js` is the small browser boundary, and `build.mjs` produces
the pinned deterministic bundle under `.artifacts/`.

Run the offline checks and build with:

```bash
PYTHONPATH=. python -m unittest evals.pageagent.test_host evals.pageagent.test_business_host
cd evals/pageagent && npm test && npm run build
```

The Odoo evaluator composes this generic bridge with its own fixture and sealed tasks. Keep
application-specific runner policy in `evals/odoo/`, not here.
