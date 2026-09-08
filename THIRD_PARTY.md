# Third-party components and licenses

This file describes the third-party boundary of the current Python distribution,
browser integration, and development workflow. Web Operation Agent itself is
licensed under the [MIT License](LICENSE).

The inventory is based on [pyproject.toml](pyproject.toml), imports in
`src/web_operation_agent/`, package data, and the built wheel. It does not treat
optional test applications or their dependency trees as product dependencies.

## Distributed Python package

The wheel contains Web Operation Agent's Python modules, policy and Skill JSON,
the embedded browser Runtime, and the trace replay template. It does not contain
browser binaries, PageController, test applications, or development tools.

The only non-standard-library package imported by the shipping Python modules
and declared as a direct runtime dependency is Playwright.

| Component | Constraint | License | Source | Use |
| --- | --- | --- | --- | --- |
| [Playwright for Python](https://github.com/microsoft/playwright-python) | `>=1.52,<1.53` | Apache-2.0 | PyPI and the linked source repository | Browser worker and browser-build attestation; installed separately from this wheel |

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
| `@page-agent/page-controller` | 1.12.2 | MIT | [alibaba/page-agent](https://github.com/alibaba/page-agent) | Supplied by the application deployment at an absolute HTTP(S) module URL; not tracked in this repository and not included in the wheel |

`src/web_operation_agent/web_runtime/embedded.js` dynamically imports
`PageController` and `getElementByIndex` from that module. It does not embed
the PageController implementation, and the shipping package does not import the
`page-agent` planner package. The Runtime and compatibility tests target
PageController 1.12.2, but the current module URL does not itself attest the
served file's version or digest.

If a public demo, container, or frontend bundle serves `page-controller.js`,
it must pin and verify the exact asset and ship that npm release's MIT license
and copyright notice. If optional chunks or visual-mask support are enabled,
the release inventory must also cover every additional browser resource loaded
by that build.

The other shipped frontend resource,
`src/web_operation_agent/web_runtime/trace_replay.html`, is a project-maintained
standalone HTML/CSS/JavaScript template with no external scripts, stylesheets,
fonts, or images.

## Build and CI tools

These tools are not included in the wheel:

| Tool | Constraint | License/source | Use |
| --- | --- | --- | --- |
| [setuptools](https://github.com/pypa/setuptools) | `>=77`; exact version not locked | MIT | PEP 517 build backend |
| [Node.js](https://github.com/nodejs/node) | Version not pinned by project metadata | Node.js is MIT; each distribution carries additional third-party notices | Executes browser Runtime tests; the product has no npm dependency manifest |
| [actions/checkout](https://github.com/actions/checkout) | `v4` tag | Exact revision and license files must be captured at the release gate | CI checkout action |
| [actions/setup-python](https://github.com/actions/setup-python) | `v5` tag | Exact revision and license files must be captured at the release gate | CI Python setup action |

A Docker/Compose-compatible runtime is an optional local-fixture prerequisite.
The project does not select or redistribute a particular Docker implementation;
its license and terms depend on the implementation chosen by the developer.

## Optional local fixtures

The following applications are exercised through HTTP APIs and browser UI. Their
source, images, and dependency trees are not included in this repository or the
Python wheel.

| Application | Qualified version | License | Source |
| --- | ---: | --- | --- |
| Twenty CRM | 2.39.0 | Primarily AGPL-3.0 with the Twenty Application Exception; individually marked files may use MIT or commercial terms | [twentyhq/twenty](https://github.com/twentyhq/twenty) |
| Apache OFBiz | 24.09.07 | Apache-2.0 | [apache/ofbiz-framework](https://github.com/apache/ofbiz-framework) |

Adapters and Skills in this repository are independent project code that call
these applications' documented interfaces. They do not copy or import either
application's source code.

## Release gate

Before publishing a wheel, container, or hosted demo:

1. Build and install from a clean Python 3.12 environment; save the resolver
   report or SBOM and verify every resolved artifact's `METADATA`,
   `LICENSE*`, and `NOTICE*`.
2. Inspect the final wheel and source distribution for undeclared vendored code,
   test applications, credentials, generated screenshots, and cache content.
3. Pin the PageController asset by version and digest and include its exact MIT
   notice anywhere that asset is served or redistributed.
4. Include the exact Playwright driver, browser, base-image, and operating-system
   notices when any of those artifacts are bundled.
5. Replace floating CI action tags with reviewed commit SHAs and record the
   corresponding upstream license files.
