# Synthetic tool-loop development evaluation

This is a **40-case development set authored by the implementation agent**, not an
independent holdout or the five user-reported examples. It uses synthetic customers,
contracts, suppliers, workflow instances, source declarations and an injection sample.
Cases cover source questions (10), counts (4), combined conditions (4), field values (5),
sums (3), tied extrema (1), lists (1), empty results (2), mixed requests (4), pages (2)
and refusal boundaries (4). API/menu metadata reuses synthetic adapter test schemas;
records, documents, questions and expected checks are authored here.

The evaluator passes only the question to the production executor (`AgentTurnExecutor` by default, or the native
`GoalTurnExecutor` composition with `--executor hybrid`), using
`yudao_turn_config` and actor-bound typed tools. Expected checks stay in the evaluator.
The browser is the existing product transport double; these timings are server turns,
not UI end-to-end latency. Checks require all expected regex groups, exact page routes,
no business API reads for source-only cases, and no injected `PWNED` answer. Refusal
checks conservatively require a refusal/partial/input-needed terminal state; a truthful
source explanation with internal undetermined coverage may therefore fail this check.
These checks are useful regressions, not semantic accuracy certification.

Paid execution is explicit:

```sh
NO_PROXY='*' PYTHONPATH=src:examples/yudao/src:. \
python -m evals.yudao.tool_loop.run --model-env .env \
  --executor hybrid --output .artifacts/tool-loop-dev --lane 1/2
```

Run the second lane with `--lane 2/2`. For comparisons, set `PYTHONPATH` to each archived
checkout and run this same runner and fixture in both arms. Keep model/options, actor,
clock, data, source and lane count fixed. `--case` selects a diagnostic case;
`--options` reads a JSON model-options override for an explicit experiment. Ordinary
unit tests do not execute this runner or call paid models.

Each lane has its own private spend ledger. Results include calls, serialized input
bytes, terminal states, selected evidence and timings; provider usage supplies input,
output, cached and reasoning tokens and registered-rate cost. Unknown usage after a
provider timeout remains unmetered; recorded cost is not an invoice or a bound on such
unknown charges. Fixture, metadata and runner digests bind a run. Raw output belongs
under ignored `.artifacts/`, never in Git.

The previous sealed 63-case set remains separate. `evals.yudao.run_goals --executor goal`
uses the goal baseline; `--executor agent` retains the generic loop for comparison; `--executor hybrid`
uses the native host's staged business declarations and bounded raw source answers.
The agent and hybrid modes use the deployment configuration and raw source lookup;
the goal baseline retains compiled rules. The existing sealed taskset and judge are unchanged. Use a resettable local
application and the sealed synthetic account, never production customer data.

Latest recovery results and all paid investigation costs are recorded in
[the recovery report](RECOVERY.zh-CN.md); earlier regression results remain in
[the comparison report](RESULTS.zh-CN.md).
