# Frozen v7 and model qualification

The completed first-use and three-model results, limitations, paid accounting,
and independent semantic review are recorded in [RESULTS.zh-CN.md](RESULTS.zh-CN.md).

The product baseline is `420eed3a`. This experiment adds evaluation code and
synthetic application surfaces; it does not change the product algorithm or
the installed model. The question author did not inspect old task files or
trajectories. See `../v7/AUTHORSHIP.zh-CN.md` for the disclosed preflight and
blindness limitations. These are first-use questions, not a rigorously blind
external benchmark.

The fixture has 64 requests and a preselected, difficult 30-request comparison
subset. Its clock is fixed at 2026-09-30 in both the model context and adapter.
Each request owns a fresh executor and actor-local synthetic client. Only
ordinary API/menu metadata and the `SOURCE` mapping enter the product; rubric,
oracles, expected states and forbidden patterns remain evaluator-only.

`run.py` invokes the existing product composition, with a browser transport
double. It measures server turn time, not UI end-to-end latency or real DOM
operation success. All required facts, status, route, no-navigation constraints,
and literal source evidence are screened. Regex matches are necessary checks,
not semantic proof; the report separately records rubric-based answer review.
Source citations are independently hashed against the frozen synthetic source.
Failures and exceptions stay in the denominator. Existing output bindings must
match before a run can resume; paid interrupted attempts must not be relabelled
as untouched first attempts.

The original fixture and judge remain available. Versioned known grading and
permission-fixture corrections are in [`../v7_1/README.md`](../v7_1/README.md).
Use the explicit corrected fixture and `--judge-version v7.1` together. Offline
regrading retains old answers and scores and makes no model calls; corrected
fixture behavior requires a separately labelled fresh run.

The reported first-use qualification retained the then-current Qwen thinking
budget. Reproduce it from `60c3721c`; `--profile production` reads the current
product options, which may have changed since that archive. The separate
three-model comparison uses reviewed non-thinking profiles and identical
product messages, tools, token ceilings and deadlines. It tests this candidate
configuration, not vendors' unrestricted defaults. Five-question blocks rotate
model order; two lanes impose the same concurrency pattern on each model.
Provider caches and load are not controlled, and the subset is not a statistical
claim about all application queries.

```bash
# Python 3.12; PYTHONPATH=src:examples/yudao/src:examples/odoo/src:.
python evals/yudao/v7/offline_check.py
python -m unittest evals.yudao.qualification.test_profiles evals.yudao.qualification.test_judge
python -m evals.yudao.qualification.run --model-env .env --profile production --output .artifacts/v7-first
python -m evals.yudao.qualification.matrix --qwen-env .env --deepseek-env .env.deepseek --output .artifacts/v7-matrix
```

Paid runs are intentional and use private per-arm/lane spend ledgers. Unknown
provider usage/cost remains unknown. DeepSeek prices are dated official CNY
off-peak rates, registered only inside this evaluator; dispatch stops during
weekday peak hours rather than using the wrong rate. Usage-reported fees are
list-price calculations, not an account-invoice or promotional-credit audit.
Compatibility probes are also counted. Private raw outputs never enter Git.

The saved code archive, source/fixture hashes, options and paid call accounting
bind the results. No v7 first-run outcome may alter its frozen questions or
judge. Further product fixes need a separately labelled experiment.
