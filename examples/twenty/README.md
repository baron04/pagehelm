# Twenty CRM example

This example embeds the thin browser runtime in a Chromium session connected to an already provisioned Twenty CRM instance. One server-side Agent handles sequential messages from the assistant panel. It can query current company data and follow API-returned company routes.

Use an isolated Twenty workspace with synthetic records. Supply every application-specific location explicitly:

```bash
export WOA_TWENTY_BASE_URL=http://127.0.0.1:3020
export WOA_TWENTY_STORAGE_STATE="$HOME/.config/web-operation-agent/twenty-storage-state.json"
export WOA_TWENTY_OAUTH_TOKEN="$HOME/.config/web-operation-agent/twenty-oauth-token.json"
export WOA_TWENTY_OAUTH_REGISTRATION="$HOME/.config/web-operation-agent/twenty-oauth-client.json"
export WOA_TWENTY_PAGE_CONTROLLER_MODULE=http://127.0.0.1:3020/woa/page-controller.js
export WOA_DEMO_STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/web-operation-agent"
export WOA_AUDIT_LEDGER="$WOA_DEMO_STATE_ROOT/twenty-audit.jsonl"
export WOA_SPEND_LEDGER="$WOA_DEMO_STATE_ROOT/model-spend.jsonl"
export WOA_OUTPUT_DIR="$WOA_DEMO_STATE_ROOT/twenty-sessions"
export WOA_MUTATION_JOURNAL="$WOA_DEMO_STATE_ROOT/twenty-mutations.sqlite3"
export WOA_MODEL_ENV="$HOME/.config/web-operation-agent/model.env"

./demo.sh
```

The model environment file contains `AGENT_OPENAI_BASE_URL`, `AGENT_OPENAI_API_KEY`, and `AGENT_OPENAI_MODEL_PRO`. Keep it outside the repository and restrict it to the local user.

The application files may also be passed as command-line options; run `./demo.sh --help` for the complete list. Command-line values take precedence over their `WOA_*` counterparts.

The default session is read-only. To make the company-create Skill available, start a visible session with `./demo.sh --allow-mutations`. Mutation-enabled sessions require `WOA_MUTATION_JOURNAL`; the private SQLite journal durably consumes each confirmed proposal before the HTTP write is sent. A process crash or uncertain acknowledgement leaves the proposal non-retryable instead of risking a duplicate write. Every proposed creation still pauses for a fresh confirmation in the panel. The example does not provision or restore the Twenty instance, so an approved creation remains in that isolated workspace.

Use `--task` more than once to exercise a sequential conversation and close the browser when you are finished. Session summaries report Agent terminal states such as `finished`, `cancelled`, or `indeterminate`; they are operational records, not business-goal certification.
