# v7.1.2 separate count/list scope correction

Task29 asks how many process names contain 出差申请, and asks to list the resubmitted
one. A total of two plus that exact resubmission is sufficient; listing both related
records remains accepted. The earlier grader incorrectly required both names.
Wrong totals, an omitted resubmission, unrelated known records and unresolved
clarifications still fail. This is a disclosed linguistic rubric correction on seen
tasks, not a product gain or a general semantic judge. All other v7.1.1 rules remain.

Offline: `python -m evals.yudao.v7_1_2.regrade --input LOCAL_RAW --output NEW_SUMMARY.json`.
Earlier fixtures, graders, bindings and raw outputs stay immutable.
