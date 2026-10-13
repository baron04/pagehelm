# Third-party components and licenses

This file describes the third-party boundary of the current Python distribution,
browser integration, and development workflow. Web Operation Agent itself is
licensed under the [MIT License](LICENSE).

The inventory is based on [pyproject.toml](pyproject.toml), imports in
`src/web_operation_agent/`, package data, and the built wheel. It does not treat
optional test applications or their dependency trees as product dependencies.

**This file is checked, not just written.** `scripts/lock_and_sbom.py --write`
resolves every distribution's runtime closure and both npm lockfiles into
[`requirements.lock`](requirements.lock) (exact versions with sha256 hashes,
installable with `--require-hashes`) and [`sbom.json`](sbom.json) (CycloneDX 1.5,
521 components). `--check` runs offline in `scripts/qualify.sh` and fails when the
lock, the SBOM, the npm lockfiles, the pinned browser asset and this document stop
agreeing. Anything whose bytes reach a user must be named here by name; build-only
tooling is inventoried in the SBOM and covered by the blanket paragraph below.

## Distributed Python package

The wheel contains Web Operation Agent's Python modules, policy and Skill JSON,
the embedded browser Runtime, and the trace replay template. It does not contain
browser binaries, PageController, test applications, or development tools.

The only non-standard-library package imported by the shipping Python modules
and declared as a direct runtime dependency is Playwright. The wheel does,
however, carry one built JavaScript bundle with third-party code compiled into
it; see *The assistant panel bundle* below.

| Component | Constraint | License | Source | Use |
| --- | --- | --- | --- | --- |
| [PyYAML](https://github.com/yaml/pyyaml) (`PyYAML`) | `>=6.0.2,<7` | MIT | PyPI and the linked source repository | Safe YAML parser for standard Skill metadata; aliases, tags and duplicate keys are rejected |
| [Playwright for Python](https://github.com/microsoft/playwright-python) (`playwright`) | `>=1.52,<1.53` | Apache-2.0 | PyPI and the linked source repository | Browser worker and browser-build attestation; installed separately from this wheel |

Playwright 1.52 metadata declares `pyee>=13,<14` and
`greenlet>=3.1.1,<4.0.0`; pyee in turn requires `typing-extensions`.
Their licenses and sources are:

| Transitive package | License | Source |
| --- | --- | --- |
| `pyee` | MIT | [jfhbrook/pyee](https://github.com/jfhbrook/pyee) |
| `greenlet` | MIT AND PSF-2.0 | [python-greenlet/greenlet](https://github.com/python-greenlet/greenlet) |
| `typing-extensions` | PSF-2.0 | [python/typing_extensions](https://github.com/python/typing_extensions) |

These are resolver ranges, not a release lock. Exact versions must be recorded
from the final clean Python 3.12 installation.

Playwright's wheel includes its LICENSE, driver LICENSE, and
`ThirdPartyNotices.txt`. Browsers installed by `playwright install` are
separate artifacts. A container or installer that redistributes a Playwright
driver or browser must also redistribute the notices and credits supplied with
those exact artifacts.

## Browser-side integration

| Component | Qualified version | License | Source | Distribution boundary |
| --- | ---: | --- | --- | --- |
| `@page-agent/page-controller` | 1.12.4 | MIT | [alibaba/page-agent](https://github.com/alibaba/page-agent) | Supplied by the application deployment at an absolute HTTP(S) module URL; not tracked in this repository and not included in the wheel |

The assistant Runtime (`assistant.js`, built from `frontend/src/runtime/`) dynamically
imports `PageController` and `getElementByIndex` from that module. It does not embed
the PageController implementation, and the shipping package does not import the
`page-agent` planner package.

The module URL does not attest what answered it, so the version is stated once, by
this package, in
[`src/web_operation_agent/policies/page_controller.json`](src/web_operation_agent/policies/page_controller.json):
the npm release, its tarball integrity, and the sha256 of **every file a deployment
has to serve**. `web_operation_agent.page_controller.verify_served_directory` compares a
served directory against that pin, and `test_page_controller.py` fails when the Runtime
name, this table, or the evaluation lockfile restates the version differently.

The row above read 1.12.2 until that check existed, as did the Runtime's own name,
while every qualified environment served 1.12.4 byte for byte. The failure mode was
not a broken build: it was a correct-looking licence inventory for software nobody
was running.

Three files are served, not one:

| Served file | sha256 | Why it ships |
| --- | --- | --- |
| `page-controller.js` | `8d7e4cee8b3435ac431f2a5b15ab5fa74080162a93f799ce0f6e93f43efb10a3` | The module the page imports |
| `SimulatorMask-Dq2GJqJc.js` | `26f70ee9cb6352ff4ef87a1c063af7533df89d6de79ad83d5fc03e19eb0e0205` | The entry file lazily imports it; a deployment serving only the entry breaks when that import resolves |
| `LICENSE` | `393ad563ce5dd0bbe283ec40f9f5d631817262bc9bbd5eb17ed038a9d9f44803` | Serving the asset redistributes it, and MIT requires the notice to travel with it |

`examples/odoo/deploy` provisions exactly these from the pinned tarball and refuses to
continue when a digest disagrees. Nothing in this repository redistributes the asset:
it is fetched at provisioning time and is not tracked in Git.

The other shipped frontend resource,
`src/web_operation_agent/web_runtime/trace_replay.html`, is a project-maintained
standalone HTML/CSS/JavaScript template with no external scripts, stylesheets,
fonts, or images.

## The assistant bundle

`scripts/build_runtime.py` compiles `frontend/` into
`src/web_operation_agent/web_runtime/assistant.js` -- the Runtime and the panel in one
file -- which the wheel carries and the gateway serves. The bundle is generated, never committed, and a wheel built without it
is refused by `scripts/check_wheel_contents.py` rather than shipping quietly -- but the
bytes it contains are a redistribution, and these are the components inside them:

| Component | Locked version | License | Source | Use |
| --- | ---: | --- | --- | --- |
| [React](https://github.com/facebook/react) | 19.3.0 | MIT | npm | The panel's rendering runtime; `react-dom` and its `scheduler` are compiled in with it |
| [react-dom](https://github.com/facebook/react) | 19.3.0 | MIT | npm | DOM renderer for the panel's Shadow DOM root |
| [lucide-react](https://github.com/lucide-icons/lucide) | 0.475.0 | ISC | npm | The icons the panel draws |

The bundle also carries three pictures, inlined as data URLs, of one drawn helper in a blue
suit: `frontend/src/assets/assistant-avatar.webp`, her face on the launcher and in the title
bar, cropped close from the half-length picture, and `frontend/src/assets/assistant-hero.webp`, her half-length figure on a new
conversation, with `frontend/src/assets/assistant-hero-dark.webp`, the same figure cut out of
its pale disc for the dark theme. They are not third-party material. They come from the repository owner's own
design file for the panel (2026-09-26), made with OpenAI's image generation, as the owner's
earlier picture for the same place was; under OpenAI's Terms of Use the output is assigned
to the person who generated it. They were resized for the panel and converted to WebP. They
are generated drawings, not photographs of anyone. A replacement must come with the same
record: who made it, with what, when, and on what terms.

Each of these emits its own `@license` banner into the bundle, so the notices travel
with the bytes; esbuild-style banner stripping must not be enabled for this build.
`frontend/package-lock.json` pins them by version and integrity, and `sbom.json`
carries every transitive component of that tree.

Everything else under `frontend/` is build-only -- Vite, TypeScript, ESLint, Storybook,
Vitest, jsdom, Sass, the type packages and the test harnesses. None of it is compiled
into the bundle or installed with the wheel, all of it is inventoried in `sbom.json`
with its declared licence, and the release gate does not require a paragraph for each.

## Evaluation-only PageAgent baseline

The independent baseline under `evals/pageagent/` has its own npm lock and is not included in the Python wheel or product Runtime. It builds an ignored browser artifact for paired evaluation from exact MIT-licensed releases:

| Component | Locked version | npm integrity | Use |
| --- | ---: | --- | --- |
| `@page-agent/core` | 1.12.4 | `sha512-Ixiv3xtjBF9ystUadNtHcmt4DDPbIis9gphxe3DB36a3Kt93K8lxDMyVltIJx986KkBJ4RGjNZ4/Dv63hJdDlg==` | Official PageAgent planning loop and custom-tool surface |
| `@page-agent/page-controller` | 1.12.4 | `sha512-I8se6/QMhg1wMZp52fkX37xbv/lkUnBihtadcoZKOQ9xvL562/sd141mu5ANPGvaCosPlXxJQ3HGIwA2VM4bwQ==` | Official DOM observation and actions for the baseline |
| `zod` | 4.5.4 | `sha512-sC95tT5iHHH9gtpj6A81kh+NEaRAUFN+qlUPDUbRfOMvNf5QCBqsb3WgvnpVtK5Y+4UfA6KqufotuTvMGiTlsA==` | Peer schema runtime required by PageAgentCore |
| `esbuild` | 0.28.2 | `sha512-HKVLS8dvII+xoKW9kmqxbRKrnWEXfJJr/FZhhJmiqIB0e053QNYFqOBouTMO/k5sID4MvCiUCvv8b9M4h32wIA==` | Evaluation bundle build only |

The tracked bridge imports these packages and delegates provider and custom business-tool calls to harness-owned bindings; it does not copy upstream implementation source into this repository. Generated bundles, `node_modules`, raw traces and provider payloads remain outside Git. Anyone redistributing the generated baseline bundle must include the PageAgent, ai-motion, chalk, Zod and other exact lockfile notices required by that distribution; the current manifest check covers the four direct qualification components and is not yet a complete generated SBOM.

## Build and CI tools

These tools are not included in the wheel:

| Tool | Constraint | License/source | Use |
| --- | --- | --- | --- |
| [setuptools](https://github.com/pypa/setuptools) | `>=77`; exact version not locked | MIT | PEP 517 build backend |
| [Node.js](https://github.com/nodejs/node) | CI uses major version 22; exact patch not locked | Node.js is MIT; each distribution carries additional third-party notices | Executes browser Runtime tests and builds the isolated PageAgent evaluation baseline |
| [actions/setup-node](https://github.com/actions/setup-node) | `49933ea5288caeca8642d1e84afbd3f7d6820020` (v4) | MIT | CI Node setup and npm cache |
| [actions/checkout](https://github.com/actions/checkout) | `11d5960a326750d5838078e36cf38b85af677262` (v4) | MIT | CI checkout action |
| [actions/setup-python](https://github.com/actions/setup-python) | `a26af69be951a213d495a4c3e4e4022e16d87065` (v5) | MIT | CI Python setup action |

Pinned to commit SHAs rather than tags. A tag is a mutable pointer, and these actions
install dependencies and run the qualification; `@v4` resolving to different code next
month would change what CI executed without changing this repository.

A Docker/Compose-compatible runtime is an optional local-fixture prerequisite.
The project does not select or redistribute a particular Docker implementation;
its license and terms depend on the implementation chosen by the developer.

## Optional local fixtures

The following applications are exercised through HTTP APIs and browser UI. Their
source, images, and dependency trees are not included in this repository or the
Python wheel.

| Application | Qualified version | License | Source |
| --- | ---: | --- | --- |

Adapters and Skills in this repository are independent project code that call
these applications' documented interfaces. They do not copy or import either
application's source code.

## Release gate

The first, third and fifth items below used to be instructions nobody executed. They are
now mechanical, and `scripts/qualify.sh` runs them:

1. **Done, and checked.** [`requirements.lock`](requirements.lock) records the resolved
   runtime closure of all four distributions with a sha256 per artifact, and
   [`sbom.json`](sbom.json) inventories those plus every npm component with its declared
   licence and the field that declared it. `scripts/lock_and_sbom.py --check` refuses a
   build where the lock, the SBOM, the npm lockfiles, the pinned browser asset and this
   document disagree.
2. Still a person's job before publishing: inspect the final wheel and source
   distribution for undeclared vendored code, test applications, credentials, generated
   screenshots, and cache content. `scripts/check_wheel_contents.py` covers the shapes it
   knows about; it does not replace looking.
3. **Done, and checked.** The product PageController asset is pinned by version *and* by
   the sha256 of every file a deployment serves, in
   [`policies/page_controller.json`](src/web_operation_agent/policies/page_controller.json);
   the MIT notice is one of those files, so it is fetched and served alongside the code.
   `test_page_controller.py` fails when the Runtime name, this document or the evaluation
   lockfile restates the version differently. A complete generated inventory for the
   PageAgent evaluation bundle is in `sbom.json`.
4. Still a person's job: include the exact Playwright driver, browser, base-image, and
   operating-system notices when any of those artifacts are bundled. Browsers installed by
   `playwright install` are separate artifacts and are outside `sbom.json` by design.
5. **Done.** CI actions are pinned to reviewed commit SHAs and recorded above.
