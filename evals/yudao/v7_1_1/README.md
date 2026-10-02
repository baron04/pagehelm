# v7.1.1 permission explanation correction

The v7.1 fixture, prompts and checks remain sealed. This grader extends v7.1:
task53 must explicitly say the actor lacks permission to read contracts, while still
giving the readable customer count. A generic "could not do this part" fails.
The count is bound to customers; equivalent "无权" and "未授权" wording is accepted.
All other v7.1 screenings and their semantic-review limitations remain.

Use `--fixture evals.yudao.v7_1.fixture --judge-version v7.1.1` for a fresh paid
qualification, or `python -m evals.yudao.v7_1_1.regrade --input LOCAL_RAW
--output NEW_SUMMARY.json` for zero-model regrading. Original grades and raw
artifacts are never overwritten. This is a grading patch, not a new holdout.
