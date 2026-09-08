"""The thin browser runtime: one page, a fixed generic tool set, nothing else.

Runs as its own process and speaks JSON lines on stdin/stdout. That is not decoration: it
is how "the browser holds no source, no plan, no model key, no ControlPlan and no
evaluator" becomes a fact a test can check rather than a claim in a document. This module
imports only the data-only `schemas`/`privacy` boundaries and Playwright from the product;
`tests/test_thin_boundary.py` asserts that closure stays that way.

The browser side remains responsible for a small set of runtime safety checks:

  * schema and argument validation, so a malformed command is refused here rather than
    turning into a stray click;
  * the origin boundary -- a navigation off the application's origin is refused;
  * stable element references and their expiry, so a ref minted before a navigation cannot
    silently resolve against a different page;
  * the navigation lifecycle, reported rather than guessed;
  * masking, so a password field's value never leaves this process;
  * session hand-off redaction at the reply boundary, so ephemeral URL credentials stay
    with the live browser document;
  * a structured ActionReceipt for every command, including the failures.

Authentication is application-owned. A caller may launch the context from an explicit
Playwright storage-state file, but this generic worker never detects login pages, reads
credentials, submits a login form, or automatically retries authentication.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from .privacy import redact_session_handoffs as _redact_session_handoffs
from .schemas import (SCHEMA_VERSIONS, ActionCommand, ActionReceipt, ElementRef,
                      PageObservation, SchemaError)

MAX_ELEMENTS = int(os.environ.get("WOA_MAX_ELEMENTS", "260"))
# Enterprise screens can be slow on a cold cache. The per-navigation limit accommodates
# initial data loading while the run-level wall clock still bounds the whole task.
NAV_TIMEOUT_MS = int(os.environ.get("WOA_NAV_TIMEOUT_MS", "90000"))
ACTION_TIMEOUT_MS = int(os.environ.get("WOA_ACTION_TIMEOUT_MS", "25000"))
# How long to keep looking for a navigation the caller said to expect.
NAV_SETTLE_S = float(os.environ.get("WOA_NAV_SETTLE_S", "3"))
#: How long `load` may be waited for once the document is parsed. Cosmetic by comparison, and
#: measured to be the most expensive constant in the browser: see `_settle`.
SETTLE_TIMEOUT_MS = int(os.environ.get("WOA_SETTLE_TIMEOUT_MS", "6000"))

# One pass over the DOM: tag every element the agent could act on with a ref, and read back
# the small amount of structure the server needs. Values of masked inputs are never read.
COLLECT_JS = r"""
(maxElements) => {
  const out = [];
  const seen = new Set();
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const label = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    if (el.labels && el.labels.length) return el.labels[0].innerText.trim();
    const t = (el.getAttribute('title') || el.getAttribute('alt') ||
               el.getAttribute('placeholder') || el.value || el.innerText || '').trim();
    return t.replace(/\s+/g, ' ').slice(0, 120);
  };
  const roleOf = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'form') return 'form';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'image') return 'button';
      if (t === 'checkbox' || t === 'radio') return t;
      if (t === 'password') return 'password';
      return 'textbox';
    }
    return tag;
  };
  document.querySelectorAll('[data-woa-ref]').forEach(e => e.removeAttribute('data-woa-ref'));
  const nodes = document.querySelectorAll(
    'a[href], button, input, select, textarea, form, [role=button]');
  let i = 0, truncated = false;
  for (const el of nodes) {
    if (out.length >= maxElements) { truncated = true; break; }
    if (!vis(el)) continue;
    const role = roleOf(el);
    const ref = 'e' + (++i);
    el.setAttribute('data-woa-ref', ref);
    const attrs = {};
    const name = el.getAttribute('name'); if (name) attrs.name = name;
    const id = el.getAttribute('id'); if (id) attrs.id = id;
    if (role === 'link') attrs.href = (el.getAttribute('href') || '').slice(0, 300);
    if (role === 'form') {
      attrs.action = (el.getAttribute('action') || '').slice(0, 300);
      attrs.method = (el.getAttribute('method') || 'get').toLowerCase();
    }
    if (role === 'select') {
      attrs.options = Array.from(el.options).slice(0, 40)
        .map(o => (o.value ?? '') + '=' + (o.text || '').trim().slice(0, 60));
    }
    if (el.type === 'checkbox' || el.type === 'radio') attrs.checked = String(el.checked);
    // A password field's value is never read; its presence is all the server needs to know.
    if (role !== 'password' && (role === 'textbox') && el.value) {
      attrs.value = String(el.value).slice(0, 120);
    }
    if (el.disabled) attrs.disabled = 'true';
    // Which form a control belongs to. A page can carry several forms with identically
    // named fields, and without this the server cannot tell them apart or say which
    // submit goes with which value.
    if (role !== 'form' && el.form) {
      const owner = el.form.getAttribute('name') || el.form.getAttribute('id') || '';
      if (owner) attrs.form = owner;
    }
    const key = role + '|' + label(el) + '|' + (attrs.name || attrs.id || attrs.href || '');
    if (seen.has(key) && role === 'link') continue;
    seen.add(key);
    out.push({ref, role, name: role === 'password' ? '' : label(el),
              tag: el.tagName.toLowerCase(), attrs});
  }
  const landmarks = [];
  document.querySelectorAll('h1, h2, h3, caption, legend, .h1, .head1, .boxhead')
    .forEach(h => { const t = (h.innerText || '').trim().replace(/\s+/g, ' ');
                    if (t && landmarks.length < 40) landmarks.push(t.slice(0, 120)); });
  const messages = [];
  document.querySelectorAll(
    '.errorMessage, .eventMessage, .alert, #content-messages, .messages, .errorMessageBlock')
    .forEach(m => { const t = (m.innerText || '').trim().replace(/\s+/g, ' ');
                    if (t && messages.length < 12) messages.push(t.slice(0, 400)); });
  return {elements: out, landmarks, messages, truncated,
          title: document.title, url: location.href};
}
"""


class OriginRefused(Exception):
    pass


class ThinBrowser:
    def __init__(self, base_url: str, headless: bool = True):
        self.base_url = base_url.rstrip("/")
        self.origin = _origin(base_url)
        self.headless = headless
        self.epoch = 0
        self.live_refs: set[str] = set()
        self._pw = self._browser = self._ctx = self.page = None
        self.console: list[str] = []

    # -- lifecycle ---------------------------------------------------------------------
    def start(self, entry_path: str = "/", storage_state_path: str | None = None) -> dict:
        if storage_state_path is not None and not isinstance(storage_state_path, str):
            raise TypeError("storage_state_path must be a string path")
        if storage_state_path is not None:
            storage_state = Path(storage_state_path)
            if not storage_state.is_absolute():
                raise ValueError("storage_state_path must be absolute")
            if not storage_state.is_file():
                raise FileNotFoundError("storage-state file does not exist")
            try:
                parsed_state = json.loads(storage_state.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise ValueError("storage-state file is not valid UTF-8 JSON") from None
            if not isinstance(parsed_state, dict):
                raise ValueError("storage-state file must contain a JSON object")
            del parsed_state
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        context_options = {"viewport": {"width": 1280, "height": 900}}
        if storage_state_path is not None:
            context_options["storage_state"] = storage_state_path
        try:
            self._ctx = self._browser.new_context(**context_options)
        except Exception as exc:
            if storage_state_path is not None:
                raise RuntimeError(
                    f"browser could not load the storage-state file ({type(exc).__name__})"
                ) from None
            raise
        self._ctx.set_default_timeout(ACTION_TIMEOUT_MS)
        self.page = self._ctx.new_page()
        self.page.on("console", lambda m: self.console.append(f"{m.type}: {m.text[:300]}"))
        self.page.on("pageerror", lambda e: self.console.append(f"pageerror: {str(e)[:300]}"))
        self._goto(self.base_url + entry_path)
        return {"ok": True, "url": self.page.url}

    def close(self) -> dict:
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:
                pass
        return {"ok": True}

    def _goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        self._settle()

    def _settle(self) -> None:
        """Wait for the document, then give subresources a short, bounded chance to finish.

        Waiting on `load` outright makes every navigation hostage to the slowest image, font
        or analytics beacon on the page -- a 45s timeout that says nothing about whether the
        agent's action worked. The document is what the next observation reads, so that is
        what is waited for, with the full action budget; `load` is cosmetic by comparison and
        gets a short grace.

        A page that has not reached `load` within the short grace may be waiting on a slow or
        unavailable subresource. Extending that wait consumes the action budget without adding
        DOM state that the next observation needs.
        """
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("load", timeout=SETTLE_TIMEOUT_MS)
        except Exception:
            pass

    # -- observation -------------------------------------------------------------------
    def observe(self) -> PageObservation:
        """Mint a new epoch. Every ref from an earlier epoch is dead from this point on."""
        raw = self.page.evaluate(COLLECT_JS, MAX_ELEMENTS)
        self.epoch += 1
        self.live_refs = {e["ref"] for e in raw["elements"]}
        return PageObservation(
            schema_version=SCHEMA_VERSIONS["PageObservation"], epoch=self.epoch,
            url=raw["url"], title=raw["title"], landmarks=raw["landmarks"],
            elements=[ElementRef(ref=e["ref"], role=e["role"], name=e["name"], tag=e["tag"],
                                 epoch=self.epoch, attrs=e["attrs"]) for e in raw["elements"]],
            messages=raw["messages"], truncated=raw["truncated"])

    def _locator(self, ref: str, epoch: int | None):
        if epoch is not None and epoch != self.epoch:
            raise _ReceiptError("stale_ref",
                                f"ref {ref} was minted in epoch {epoch}, the page is now at "
                                f"epoch {self.epoch}; observe again")
        if ref not in self.live_refs:
            raise _ReceiptError("unknown_ref", f"no element {ref} in the current observation")
        loc = self.page.locator(f'[data-woa-ref="{ref}"]')
        if loc.count() == 0:
            raise _ReceiptError("stale_ref", f"element {ref} is no longer in the document")
        return loc.first

    # -- actions -----------------------------------------------------------------------
    def execute(self, cmd: ActionCommand) -> ActionReceipt:
        t0 = time.time()
        url_before, epoch_before = self.page.url, self.epoch
        ok, code, detail = True, "", ""
        try:
            self._dispatch(cmd)
        except _ReceiptError as e:
            ok, code, detail = False, e.code, e.detail
        except OriginRefused as e:
            ok, code, detail = False, "origin_refused", str(e)
        except Exception as e:                                  # playwright & friends
            name = type(e).__name__
            first = str(e).splitlines()[0][:300]
            ok = False
            if "Timeout" in name:
                # The label describes Playwright's failing operation, not proof of
                # non-dispatch: a Locator timeout may also happen after the click.
                code = ("element_not_actionable" if "Locator." in first or "locator." in first
                        else "navigation_timeout")
            elif any(m in first for m in ("not an <input>", "not an <select>", "not editable",
                                          "Element is not", "intercepts pointer events",
                                          "not visible", "not enabled")):
                # The model aimed a text fill at a dropdown, or clicked something covered.
                # Saying which is far more useful to it than a generic page error.
                code = "element_not_actionable"
            else:
                code = "page_error"
            detail = f"{name}: {first}"
        url_after = self.page.url
        obs = None
        try:
            obs = self.observe()
            url_after = obs.url
        except Exception as e:
            if ok:
                ok, code, detail = False, "page_error", f"observation failed: {type(e).__name__}: {e}"
        return ActionReceipt(
            schema_version=SCHEMA_VERSIONS["ActionReceipt"], command_id=cmd.command_id,
            action=cmd.action, args_echo=_mask(cmd.args), ok=ok, error_code=code,
            error_detail=detail, url_before=url_before, url_after=url_after,
            navigated=_norm(url_before) != _norm(url_after),
            epoch_before=epoch_before, epoch_after=self.epoch,
            elapsed_ms=int((time.time() - t0) * 1000), relogin=False, observation=obs)

    def _dispatch(self, cmd: ActionCommand) -> None:
        a, args = cmd.action, cmd.args
        if a == "observe":
            return
        if a == "goto":
            url = args.get("url") or ""
            if not url:
                raise _ReceiptError("bad_arguments", "goto needs a url")
            full = url if url.startswith("http") else self.base_url + ("" if url.startswith("/") else "/") + url
            if _origin(full) != self.origin:
                raise OriginRefused(f"{_origin(full)} is outside the application origin {self.origin}")
            self._goto(full)
            return
        if a == "back":
            self.page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            self._settle()
            return
        if a == "wait_for":
            self.page.wait_for_timeout(min(int(args.get("ms", 1000)), 10000))
            return
        ref = args.get("ref")
        if not ref:
            raise _ReceiptError("bad_arguments", f"{a} needs a ref from the current observation")
        loc = self._locator(ref, args.get("epoch"))
        if a == "click":
            self._with_navigation(cmd, lambda: loc.click(timeout=ACTION_TIMEOUT_MS))
        elif a == "fill":
            if "value" not in args:
                raise _ReceiptError("bad_arguments", "fill needs a value")
            loc.fill(str(args["value"]), timeout=ACTION_TIMEOUT_MS)
        elif a == "select":
            if "value" not in args:
                raise _ReceiptError("bad_arguments", "select needs a value")
            try:
                loc.select_option(value=str(args["value"]), timeout=ACTION_TIMEOUT_MS)
            except Exception:
                loc.select_option(label=str(args["value"]), timeout=ACTION_TIMEOUT_MS)
        elif a == "submit":
            self._with_navigation(cmd, lambda: loc.evaluate(
                "el => (el.tagName === 'FORM' ? el : el.form).requestSubmit "
                "? (el.tagName === 'FORM' ? el : el.form).requestSubmit() "
                ": (el.tagName === 'FORM' ? el : el.form).submit()"))
        else:                                                   # unreachable: schema-checked
            raise _ReceiptError("no_such_action", a)

    def _with_navigation(self, cmd: ActionCommand, act) -> None:
        """Run an action that may navigate, and settle the lifecycle before returning.

        `expect_navigation` is treated as a hint about how long to wait, never as a promise
        to be enforced. Arming Playwright's navigation waiter and failing when it does not
        fire turns an action that worked -- a submit answered in place, a link that rewrote
        the page -- into a 45-second timeout that says nothing about the task. What the
        server needs is the settled page and an honest `navigated` flag on the receipt, and
        both survive either outcome.

        Settling happens once, at the end, on whichever document the action left behind, so a
        navigation cannot pay the same load timeout twice.
        """
        before = _norm(self.page.url)
        act()
        if cmd.expect_navigation:
            deadline = time.time() + NAV_SETTLE_S
            while time.time() < deadline and _norm(self.page.url) == before:
                self.page.wait_for_timeout(100)
        self._settle()


class _ReceiptError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def _norm(url: str) -> str:
    return url.split("#", 1)[0]


MASKED_KEYS = {"password", "passwd", "pwd", "secret", "token", "apikey", "api_key"}


def _mask(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        out[k] = "***" if k.lower() in MASKED_KEYS else v
    return out


# --------------------------------------------------------------------------------------
# JSON-lines worker
# --------------------------------------------------------------------------------------

def main(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    br: ThinBrowser | None = None

    def reply(obj):
        # This is the single browser -> server serialization boundary.  Redacting here
        # covers start URLs, observations, nested receipts, console lines and error text
        # without changing the live DOM that ref-based clicks operate on.
        stdout.write(json.dumps(_redact_session_handoffs(obj), ensure_ascii=False) + "\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            kind = msg.get("kind")
            if kind == "start":
                br = ThinBrowser(msg["base_url"], headless=msg.get("headless", True))
                reply({"kind": "started", **br.start(msg.get("entry_path", "/"),
                                                     msg.get("storage_state_path"))})
            elif kind == "command":
                if br is None:
                    reply({"kind": "error", "error": "browser has not been started"})
                    continue
                cmd = ActionCommand.from_json(msg["command"])
                reply({"kind": "receipt", "receipt": br.execute(cmd).to_json()})
            elif kind == "observe":
                if br is None:
                    reply({"kind": "error", "error": "browser has not been started"})
                    continue
                reply({"kind": "observation", "observation": br.observe().to_json()})
            elif kind == "console":
                reply({"kind": "console", "lines": list(br.console) if br else []})
            elif kind == "close":
                reply({"kind": "closed", **(br.close() if br else {"ok": True})})
                return 0
            else:
                reply({"kind": "error", "error": f"unknown message kind {kind!r}"})
        except SchemaError as e:
            reply({"kind": "error", "error": f"schema: {e}"})
        except Exception as e:
            reply({"kind": "error", "error": f"{type(e).__name__}: {e}",
                   "traceback": traceback.format_exc()[-1200:]})
    if br is not None:
        br.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
