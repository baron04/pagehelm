# @woa/panel — the assistant panel

The panel the Web Operation Agent shows inside a host application's page: a floating window
or a right-edge drawer that takes a goal, shows what the agent did, and asks before anything
is written.

This directory builds one file, `assistant.js`, the one script a host page includes: the
boot supervisor and the Runtime (the panel's shell, the transport, the page observer
and executor) and this window's view, described below. It is what the Odoo plugin's script
tag loads, and nothing is fetched after it. It is served by the agent, minified, since it
carries React and loads on every page of the host application.

The Runtime still holds no view. It hands the panel a host and a container through
`src/runtime/panelBridge.ts`, the one Runtime module the lint lets reach the panel; nothing
else under `src/runtime/` may import React or the panel's components.

The panel is a view. It holds no session, makes no network call of its own, and keeps no timeline:
the embedded Runtime hands it the server's projection and it renders that. Everything it
renders *by* that the server also reads -- the text limits, the copy it sends, the state
names -- comes from two files the server owns:

| File | What it decides |
|---|---|
| `../src/web_operation_agent/web_runtime/panel_contract.json` | protocol version, panel states and tones, the progress-state map, text and paging limits, the labels and notices the server sends |
| `../src/web_operation_agent/web_runtime/browser_contract.json` | viewport extraction, observation-size, and bounded-scroll limits shared with the Python wire schema |

Those are imported here through the `@contract` alias, never copied. What only the browser
reads is this directory's alone:

| File | What it decides |
|---|---|
| `src/presentation.json` | the stacking order, the launcher's corner and size, the visibility gate on a confirmation card, which confirmations can be declined, and the panel's own words for a start that failed |
| `src/styles/design_tokens.json` | the palette and scale for both themes, with the contrast each pair must meet (`tests/tokens.unit.test.ts`) |
| `src/state/geometry.ts` | the window's sizes, placement, and floating or drawer mode |
| `trace/trace_replay.html` | the replay page the server fills with a past run's data; `npm run build` inlines the tokens into it |

Two sets of constants drifting apart is the defect this arrangement exists to prevent, and `src/web_operation_agent/tests/test_panel_contract.py`
fails if the panel starts declaring something the server does not send.

## Working on it

```bash
npm ci               # Node ^20.19 || >=22.12, which is what Vite 8 requires
npm run dev          # http://localhost:5173 — the panel over a stand-in host page
npm run storybook    # every state, including the ones a normal run rarely reaches
npm test             # unit tests: the markdown grammar, geometry, the host contract
npm run test:browser # Chromium checks of the built panel (e2e/); needs npm run build first
npm run lint
npm run build        # dist/runtime/assistant.js and the trace replay page
```

`npm run dev` serves `index.html`, a page that stands in for a host application, and mounts
the panel into it through the same `mountPanel` the Runtime uses — same shadow root, same
adopted stylesheet. `dev/host.ts` answers the `PanelHost` contract without a server, so if
the panel ever needs something that file cannot provide, the contract is wrong rather than
the harness. It fakes the transport and nothing else: approvals still expire, and a write
still blocks the composer until it is answered. The buttons along the top drive a scripted
read, a write with a confirmation, an indeterminate outcome, and a session taken over by
another page.

Neither `index.html` nor `dev/` reaches the product: the product build takes
`src/runtime/index.ts` as its only entry.

`npm run build` runs the library build: `vite.runtime.config.ts` produces
`dist/runtime/assistant.js`, a single ES module with its dynamic imports inlined, and the
trace replay page is built beside it. `python scripts/build_runtime.py` copies
`assistant.js` into the Python package, which is how the gateway comes to serve it; `python
scripts/build_panel.py` builds the frontend and copies the trace replay page. Both are
generated artifacts: they are not committed, and a checkout without Node has no assistant --
the gateway then refuses the request by name rather than serving something older.

The browser behaviour is tested here, in Chromium, against the built bundle: `e2e/` holds
Playwright tests that mount the panel from `dist/runtime/assistant.js` over a scripted host (`npm run build`, then
`npm run test:browser`; `npx playwright install chromium` once for the browser). That is
where the states, the keyboard paths, the narrow layouts and the strict-CSP construction are
asserted, and where a Storybook build is checked to show the shipped panel. Storybook is for
looking; those tests are the gate.

## Shape

```
src/
  runtime/    assistant.js's world: the panel's shell, the boot supervisor, the
              transport, the observer and executor -- no view; panelBridge.ts alone mounts it
  contract/   a typed view over panel_contract.json — no values of its own
  bridge/     the host contract, checked once at mount, never probed at a call site
  state/      the store subscription, window geometry, the reveal, the visibility gate
  markdown/   a closed grammar parsed to nodes; no HTML string exists anywhere
  components/ the window, the launcher, the thread, the confirmation card, the composer
  styles/     SCSS over the generated token properties
  mount.tsx   shadow root, adopted stylesheet, React root
dev/          a stub Runtime and a stand-in host page, for `npm run dev` only
stories/      Storybook, over the same host double
```

Four rules the code follows and the tests enforce:

- **No `innerHTML`, no inline `<style>`, no `eval`.** Model output is untrusted, and the
  host page may be running a strict CSP with Trusted Types. Markdown is parsed to nodes and
  built by React; the stylesheet is adopted as a constructed sheet, which `style-src` does
  not govern.
- **Only model-authored prose is revealed a character at a time.** A table the server
  projected from a typed query is a fact it computed, and animating it as though it were
  arriving would be a small, avoidable lie. `TimelineEntry.authored` decides, and it comes
  from the server.
- **The confirmation card owns nothing it could be wrong about.** Its text, its targets and
  its expiry come from the request the session published. What the panel adds is the refusal
  of a press the user could not have made or seen — and `SECURITY.md` states exactly how far
  that reaches.
- **Handwritten style quotes the scale instead of restating it.** A size, a radius, a
  line-height or a colour reaches the stylesheet only through a generated custom property;
  a percentage radius is allowed only where the thing is a circle, and a shadow's rgba only
  because a shadow is light rather than colour. The scan in
  `src/web_operation_agent/tests/test_panel_contract.py` refuses the one-off value, so the
  pass that put every number on the declared scale cannot quietly rot back.
