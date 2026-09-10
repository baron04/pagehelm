# Minimal Contacts demo

This example runs a complete first-party Web Operation Agent loop without Twenty, OAuth, a source checkout or a database. It starts a synthetic Contacts application and typed API on loopback, opens Chromium, injects the embedded assistant, and routes every observation, action and result through the real session gateway and server ReAct executor.

Install the project and Chromium once:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --editable ".[demo]"
python -m playwright install chromium
```

Run the fixed zero-cost walkthrough:

```bash
web-operation-agent-demo --scripted
```

The walkthrough loads the Contacts Skill, queries the current typed API, opens the exact route returned for Grace Hopper, observes the resulting page, and stops. `--scripted` is an apparatus check: it proves the product wiring and browser loop execute, but says nothing about model quality.

To use a real OpenAI-compatible provider, set `AGENT_OPENAI_BASE_URL`, `AGENT_OPENAI_API_KEY`, and `AGENT_OPENAI_MODEL_PRO`, then run:

```bash
web-operation-agent-demo
```

The browser remains open for interactive tasks until you close it. Provider calls are written to a private spend ledger inside the printed temporary run directory. Use only synthetic data in this demo.

For a non-visual qualification run:

```bash
web-operation-agent-demo --scripted --headless
```

Application-specific code is confined to `web_operation_agent_demo`: the adapter, Skill, typed tool, synthetic app, and its same-origin PageController module. That small controller exists only to make the synthetic page self-contained; it is not a replacement for the qualified upstream PageController used by real integrations. The generic Agent, gateway, browser protocol, panel and policy code are imported unchanged from `web_operation_agent`.
