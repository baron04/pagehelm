# @woa/panel — the assistant panel

The panel the Web Operation Agent shows inside a host application's page: a floating window
that takes a goal, shows what the agent did, and asks before anything is written.

It is a view. It holds no session, makes no network call of its own, and keeps no timeline:
the embedded Runtime hands it the server's projection and it renders that. Everything it
renders *by* -- the bounds, the copy, the state names, the palette -- comes from two files
the server owns:

| File | What it decides |
|---|---|
| `../src/web_operation_agent/web_runtime/panel_contract.json` | protocol version, panel states and tones, the progress-state map, bounds, every user-visible string |
| `../src/web_operation_agent/web_runtime/design_tokens.json` | the palette and scale, for both themes, with the contrast each pair must meet |

Those are imported here through the `@contract` alias, never copied. Two sets of constants
drifting apart is the defect this arrangement exists to prevent, and
`src/web_operation_agent/tests/test_panel_contract.py` fails if the panel starts declaring
something the server does not send.

## Working on it

```bash
npm ci
npm run dev          # Vite, against a stub host
npm run storybook    # every state, including the ones a normal run rarely reaches
npm test             # unit tests: the markdown grammar, geometry, the host contract
npm run lint
npm run build        # dist/panel/panel.js
```

`npm run build` produces one ES module. `python scripts/build_panel.py` runs that build and
copies the result into the Python package with a manifest, which is how the gateway comes to
serve it. The built panel is a generated artifact: it is not committed, and a checkout
without Node has no panel — the gateway then refuses the request by name rather than serving
something older.

The browser behaviour is tested from the Python side, in Chromium, against the built
bundle: `src/web_operation_agent/tests/test_panel_ui.py`. That is where the states, the
keyboard paths, the narrow layouts and the strict-CSP construction are asserted. Storybook
is for looking; those tests are the gate.

## Shape

```
src/
  contract/     a typed view over panel_contract.json — no values of its own
  bridge/       the host contract, checked once at mount, never probed at a call site
  state/        the store subscription, window geometry, the reveal, the visibility gate
  markdown/     a closed grammar parsed to nodes; no HTML string exists anywhere
  components/   the window, the launcher, the thread, the confirmation card, the composer
  styles/       SCSS over the generated token properties
  mount.tsx     shadow root, adopted stylesheet, React root
```

Three rules the code follows and the tests enforce:

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
