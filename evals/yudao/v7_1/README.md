# Versioned v7.1 corrections

The original `../v7/` fixture, tasks, source, rubric and manifest remain sealed.
This overlay keeps all 64 prompts, actor-visible data, source bytes and the clock.
It is a seen regression suite, not a new holdout. Expected state stays evaluator-only.

Corrections:

- Task 19 asks for the owner and submission status; zero amount identifies the record
  and need not be displayed as another answer field.
- List screenings compare complete known record names, matching longer names first.
  Truncated results and unknown enumerated records fail. Task 36 additionally requires
  the maximum with an explicit monetary unit, every tie and no frequency answer.
- Task 38 accepts a truthful scoped absence or zero count, including `refused` status,
  only after a contract read and without navigation. Permission refusals still fail.
- Page tasks require the existing ready, reached, matching rendered-page receipt.
  This is a browser transport double, not proof of native UI operation.
- Restricted contract reads now raise the adapter's typed `YudaoReadRefused(code=403)`;
  customer reads and mutation prohibitions retain their original behavior.

These are bounded deterministic checks, not full natural-language semantic validation.
Known-name matching does not prove every inline name, field association, grouping or
source paraphrase is correct. Source citation bytes are checked; relevance and entailment
still require semantic review. Do not call the screened pass rate audited accuracy.

```bash
# Python 3.12, PYTHONPATH=src:examples/yudao/src:examples/odoo/src:.
python -m unittest evals.yudao.v7_1.test_judge evals.yudao.qualification.test_judge
python -m evals.yudao.v7_1.regrade --input LOCAL_FROZEN_RAW --output NEW_SUMMARY.json
# Explicit paid evaluation; isolated synthetic API/source, private ledger per lane.
python -m evals.yudao.qualification.run --model-env .env --profile fast \
  --fixture evals.yudao.v7_1.fixture --judge-version v7.1 --output NEW_LOCAL_OUTPUT
```

Offline regrading calls no model and does not overwrite historical artifacts. A historical
harness error remains a failure; changed fixture behavior must be rerun, not regraded
into a success. Keep old and corrected scores side by side and separate score-policy
changes from product changes. Fresh output bindings record the grader version and digest.
