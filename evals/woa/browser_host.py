"""Real-browser runtime host for the Web Operation Agent Twenty runner.

The host is the trusted Playwright process from the public example, reduced to one run: it
serves the embedded runtime from a loopback gateway bound to the runner's own session
registry, injects the bootstrap script into the authenticated page, and submits one goal
through the panel API. It holds the browser session cookie and never sends it to the model,
the runtime or any artifact.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.twenty.evidence import visible_page_projection
from web_operation_agent.embedded_gateway import EmbeddedGateway


class PlaywrightRuntimeHost:
    """Drive one authenticated first-party page for a single evaluation run."""

    def __init__(self, *, channel, grant, registry, origin: str, entry_path: str,
                 storage_state_path: Path, page_controller_module: str,
                 navigation_mode: str = "spa", headless: bool = True,
                 browser_executable: Path | None = None, timeout_s: float = 60.0):
        self.channel = channel
        self.grant = grant
        self.origin = origin
        self.entry_path = entry_path
        self.storage_state_path = Path(storage_state_path)
        self.page_controller_module = page_controller_module
        self.navigation_mode = navigation_mode
        self.headless = headless
        self.browser_executable = (
            Path(browser_executable) if browser_executable is not None else None)
        self.timeout_s = timeout_s
        self.gateway = EmbeddedGateway(registry, sse_wait_s=0.1)
        self.errors: list[str] = []
        self.page_switch_confirmations = 0
        self.confirmation_failures: list[str] = []
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None

    @property
    def current_url(self) -> str:
        """Read the document's own location.

        A first-party SPA route change uses ``history.pushState``, and Playwright's cached
        ``page.url`` can still hold the previous route immediately afterwards. Page evidence
        must describe the document the user is actually looking at, so the URL is read from
        the page at the same moment as the visible projection.
        """
        if self.page is None:
            return ""
        try:
            value = self.page.evaluate("() => location.href")
        except Exception:
            return self.page.url
        return value if isinstance(value, str) and value else self.page.url

    @property
    def current_path(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.current_url).path

    def start(self) -> "PlaywrightRuntimeHost":
        from playwright.sync_api import sync_playwright

        self.gateway.start()
        bootstrap = self.gateway.bootstrap_script(
            self.grant, page_controller_module=self.page_controller_module,
            navigation_mode=self.navigation_mode)
        self._playwright = sync_playwright().start()
        launch: dict[str, Any] = {"headless": self.headless}
        if self.browser_executable is not None:
            launch["executable_path"] = str(self.browser_executable)
        self._browser = self._playwright.chromium.launch(**launch)
        self._context = self._browser.new_context(
            storage_state=str(self.storage_state_path),
            viewport={"width": 1440, "height": 1_000})
        self._context.add_init_script(script=bootstrap)
        self.page = self._context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(type(error).__name__))
        timeout_ms = int(self.timeout_s * 1_000)
        self.page.goto(self.origin + self.entry_path,
                       wait_until="domcontentloaded", timeout=timeout_ms)
        self.page.wait_for_function(
            "window.__WOA_RUNTIME_READY__ !== undefined", timeout=timeout_ms)
        self.page.evaluate("async () => { await window.__WOA_RUNTIME_READY__; return true; }")
        return self

    def observe_now(self):
        """The panel observes before every task; the runner uses ``submit_task`` instead."""
        raise NotImplementedError("the embedded runtime observes inside submitTask")

    #: The panel's own card, read the way a person sees it: kind, or "" when none is up.
    _CARD_KIND_JS = """() => {
      const host = document.querySelector('#woa-assistant');
      const root = host && host.shadowRoot;
      const card = root && root.querySelector('section.woa-approval');
      return card ? (card.dataset.kind || 'write') : '';
    }"""
    _CARD_GONE_JS = ("() => !document.querySelector('#woa-assistant').shadowRoot"
                     ".querySelector('section.woa-approval')")

    def answer_page_switch_confirmations(self, done, *, visible_wait_ms: int = 700) -> None:
        """Press the page-switch card while the turn runs, as the person who asked would.

        The product asks before it leaves the current page, so a case that opens a record
        cannot complete unless someone answers (ruling D7; the Odoo apparatus answers the
        same card the same way). A write card is never answered here: the live schedule runs
        no write case, and a write decision belongs to a case's own oracle, not to the
        apparatus. Runs on the thread that created the page while the turn executes on
        another, and returns as soon as that turn is done.
        """
        while not done.wait(0.1):
            try:
                kind = self.page.evaluate(self._CARD_KIND_JS)
            except Exception:
                return
            if kind != "navigation":
                continue
            try:
                self.page.wait_for_timeout(visible_wait_ms)
                self.page.locator("#woa-assistant").get_by_test_id(
                    "woa-approve").click(timeout=10_000)
                self.page.wait_for_function(self._CARD_GONE_JS, timeout=15_000)
            except Exception as exc:
                # The apparatus could not do what the person does. Recorded, not swallowed:
                # the run then fails on the product's own terminal reason, and this says why.
                self.confirmation_failures.append(type(exc).__name__)
                return
            self.page_switch_confirmations += 1

    def submit_task(self, text: str) -> str:
        self.page.evaluate(
            "task => window.__WOA_RUNTIME_READY__.then(runtime => runtime.submitTask(task))",
            text)
        return ""

    def storage_state(self) -> dict[str, Any]:
        return self._context.storage_state()

    def visible_projection(self) -> str:
        return visible_page_projection(self.page)

    def stop(self) -> None:
        for close in (
                lambda: self._context.close() if self._context is not None else None,
                lambda: self._browser.close() if self._browser is not None else None,
                lambda: self._playwright.stop() if self._playwright is not None else None,
                self.gateway.close):
            try:
                close()
            except Exception:
                pass


def playwright_runtime_factory(*, storage_state_path: Path, page_controller_module: str,
                               navigation_mode: str = "spa", headless: bool = True,
                               browser_executable: Path | None = None,
                               timeout_s: float = 60.0):
    """Return a runner ``runtime_factory`` bound to one authenticated browser profile."""
    def factory(*, channel, grant, registry, origin, entry_path):
        return PlaywrightRuntimeHost(
            channel=channel, grant=grant, registry=registry, origin=origin,
            entry_path=entry_path, storage_state_path=storage_state_path,
            page_controller_module=page_controller_module,
            navigation_mode=navigation_mode, headless=headless,
            browser_executable=browser_executable, timeout_s=timeout_s)
    return factory


def sample_visible_page(runtime: PlaywrightRuntimeHost) -> str:
    return runtime.visible_projection()
