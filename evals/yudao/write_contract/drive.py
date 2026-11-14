"""Drive the contract over real native forms in a real browser, with no model.

For each form: open it on an existing record as the assistant would (inside the read-only
window), change one field through the page's own controls, read the whole form data, build the
card, arm a permit that judges every request exactly as it would for a real submit, press the
form's own submit control once, and record what the application's page tried to send. A dry
run sends nothing: the permit refuses after judging, so no data changes.

The steps are the Runtime's, performed here with the same page-side reader
(`readElementFormModel`) and the same guard endpoint; the contract code is the product's.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time

from playwright.sync_api import sync_playwright

from web_operation_agent.execution.submit_contract import (
    ContractRefused, FormData, SubmitPermit, build_card, diff_models)

SUBMIT = re.compile(r"^\s*(确\s*定|保\s*存|提\s*交)\s*$")
EDIT_LABELS = ("编辑", "修改", "处置")

IDENTITY = """
(() => {
  const read = key => { try { return JSON.parse(JSON.parse(localStorage.getItem(key)).v) } catch { return '' } }
  window.__w46 = {
    headers: () => ({'Content-Type': 'application/json', Authorization: 'Bearer ' + read('ACCESS_TOKEN'), 'tenant-id': String(read('tenantId') || '1')}),
    guard: async step => {
      const response = await window.fetch('/woa/page-guard/' + step, {method: 'POST', credentials: 'same-origin',
        headers: window.__w46.headers(), body: JSON.stringify({ttl_s: 120})})
      let body = null
      try { body = await response.json() } catch {}
      return {status: response.status, body}
    },
    form: async selector => {
      const module = await import('/woa/page-facts.js')
      const element = document.querySelector(selector)
      return element ? (module.readElementFormModel(element) ?? null) : null
    },
    open: async path => {
      const module = await import('/woa/page-facts.js')
      return await module.openPage(path)
    },
    row: async selector => {
      const module = await import('/woa/page-facts.js')
      const element = document.querySelector(selector)
      return element ? (module.readElementRowRecord(element) ?? null) : null
    },
  }
})()
"""

# Marks the first text control a person would retype: not a picker, a number or a date.
MARK_FIELD = """
(dialog) => {
  const root = document.querySelector(dialog)
  if (!root) return null
  const skip = '.el-select,.el-date-editor,.el-cascader,.el-input-number,.el-tree-select,.el-switch,.el-radio-group,.el-checkbox-group,.el-upload,.el-color-picker,.el-slider,.el-rate,.el-time-select'
  for (const item of root.querySelectorAll('.el-form-item')) {
    if (item.closest('.el-table')) continue
    const control = Array.from(item.querySelectorAll('input.el-input__inner[type="text"],textarea.el-textarea__inner'))
      .find(node => !node.closest(skip) && !node.readOnly && !node.disabled && node.getBoundingClientRect().width > 0)
    if (control) {
      control.setAttribute('data-w46', '1')
      const label = (item.querySelector('.el-form-item__label')?.textContent || '').replace(/\\s+/g, ' ').trim()
      return {label, value: control.value}
    }
  }
  return null
}
"""

# A deliberately plain filler for a new record: every visible required control gets some value.
FILL_REQUIRED = """
(dialog) => {
  const root = document.querySelector(dialog)
  const filled = []
  if (!root) return filled
  const skip = '.el-select,.el-date-editor,.el-cascader,.el-input-number,.el-tree-select'
  let n = 0
  for (const item of root.querySelectorAll('.el-form-item.is-required')) {
    const text = Array.from(item.querySelectorAll('input.el-input__inner[type="text"],textarea.el-textarea__inner'))
      .find(node => !node.closest(skip) && !node.readOnly && !node.disabled && !node.value && node.getBoundingClientRect().width > 0)
    if (text) { text.setAttribute('data-w46-fill', String(++n)); filled.push('text') }
    const number = item.querySelector('.el-input-number input')
    if (number && !number.disabled && !number.value) { number.setAttribute('data-w46-fill', String(++n)); filled.push('number') }
  }
  return n
}
"""


@dataclass
class Outcome:
    route: str
    form: str
    operation: str
    passed: bool = False
    reason: str = ""
    detail: str = ""
    changed: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    card: dict = field(default_factory=dict)
    seconds: float = 0.0


class Driver:
    def __init__(self, stack, *, headless: bool = True):
        self.stack, self.headless = stack, headless

    def __enter__(self):
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context(viewport={"width": 1500, "height": 1000})
        self.context.add_init_script(self.stack.sign_in_script())
        self.context.add_init_script(IDENTITY)
        self.page = self.context.new_page()
        return self

    def __exit__(self, *_):
        self.context.close()
        self.browser.close()
        self._pw.stop()

    # -- steps ------------------------------------------------------------------------------
    def guard(self, step):
        reply = self.page.evaluate("step => window.__w46.guard(step)", step)
        return reply

    def open_page(self, route):
        """As the assistant does: the window is open first, then the application's own router
        shows the page, so what the page reads for itself is seen by the guard."""
        if "/woa/page-guard" not in "" and not getattr(self, "_loaded", False):
            self.page.goto(self.stack.origin + "/", wait_until="domcontentloaded")
            self.page.wait_for_selector(".el-menu, .layout-container, aside", timeout=30000)
            self._loaded = True
        begun = self.guard("begin")
        if begun["status"] != 200:
            raise ContractRefused("window_refused", str(begun)[:160])
        opened = self.page.evaluate("path => window.__w46.open(path)", route)
        if not opened:
            self.page.goto(self.stack.origin + route, wait_until="domcontentloaded")
            self.guard("begin")
        self.page.wait_for_selector(".el-table", timeout=20000)
        self.page.wait_for_function("() => !document.querySelector('.el-table .el-loading-mask')", timeout=15000)
        return begun

    def press(self, button):
        """A person's click; where the control is under something that never goes away (a fixed
        column, a sticky bar), the same click event reaches the control itself."""
        try:
            button.click(timeout=8000)
        except Exception:  # noqa: BLE001
            button.dispatch_event("click")

    def first_row_button(self, labels):
        """The first row, in order, that offers the control: rows in some states do not."""
        rows = self.page.locator(".el-table__body-wrapper tbody tr")
        for index in range(min(rows.count(), 10)):
            for label in labels:
                button = rows.nth(index).locator("button", has_text=label)
                if button.count():
                    return button.first
        return None

    def dialog_selector(self):
        return ".el-dialog:visible, .el-overlay-dialog .el-dialog"

    def wait_form(self, mode=None):
        self.page.wait_for_selector(".el-dialog form.el-form", timeout=10000)
        deadline = time.time() + 12
        selector = ".el-dialog form.el-form input, .el-dialog form.el-form textarea, .el-dialog form.el-form .el-form-item"
        while time.time() < deadline:
            data = self.page.evaluate("s => window.__w46.form(s)", selector)
            if data and not data.get("unreadable") and (mode is None or data.get("mode") == mode):
                return data
            self.last_unreadable = (data or {}).get("unreadable") or ("waiting_for_" + str(mode) if data else "not_found")
            time.sleep(0.4)
        return None

    def read_form(self):
        selector = ".el-dialog form.el-form .el-form-item"
        return self.page.evaluate("s => window.__w46.form(s)", selector)

    def fill_required(self):
        """A plain, generic filler for a new record: each required control still empty gets some
        value of its own kind. Whatever it cannot fill is left, and the form's own validation
        then says so (no request is sent)."""
        items = self.page.locator(".el-dialog .el-form-item.is-required")
        for index in range(items.count()):
            item = items.nth(index)
            try:
                if item.locator(".el-select").count():
                    shown = item.locator(".el-select .el-select__placeholder:not(.is-transparent), .el-select input.el-input__inner")
                    if item.locator(".el-select__selected-item").count():
                        continue
                    item.locator(".el-select").first.click(timeout=2000)
                    options = self.page.locator(".el-select-dropdown__item:visible:not(.is-disabled)")
                    if options.count():
                        options.first.click(timeout=2000)
                    else:   # a tree select offers nodes, not options
                        self.page.locator(".el-tree-node__content:visible").first.click(timeout=2000)
                    del shown
                elif item.locator(".el-date-editor").count():
                    editor = item.locator(".el-date-editor input").first
                    if not editor.input_value():
                        editor.fill("2026-11-14 10:00:00" if "datetime" in (item.locator(".el-date-editor").first.get_attribute("class") or "") else "2026-11-14")
                        editor.press("Enter")
                elif item.locator(".el-radio-group").count():
                    if not item.locator(".el-radio.is-checked,.el-radio-button.is-active").count():
                        item.locator(".el-radio,.el-radio-button").first.click(timeout=2000)
                elif item.locator(".el-input-number input").count():
                    box = item.locator(".el-input-number input").first
                    if not box.input_value():
                        box.fill("1")
                else:
                    box = item.locator("input.el-input__inner[type='text'], textarea.el-textarea__inner").first
                    if box.count() and not box.input_value() and box.is_editable():
                        box.fill("w46")
            except Exception:  # noqa: BLE001 -- an unfillable control is left for validation to report
                continue

    def submit_button(self):
        for button in self.page.locator(".el-dialog button").all():
            try:
                if SUBMIT.match(button.inner_text()) and button.is_visible() and button.is_enabled():
                    return button
            except Exception:  # noqa: BLE001
                continue
        return None

    def close_dialog(self):
        try:
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass

    # -- one operation ------------------------------------------------------------------------
    def run_operation(self, entry, operation: str) -> Outcome:
        out = Outcome(entry["route"], entry["component"], operation)
        started = time.time()
        stack = self.stack
        actor = stack.actor()
        try:
            self._operate(entry, operation, out, actor)
        except ContractRefused as exc:
            out.reason, out.detail = exc.code, exc.detail
        except Exception as exc:  # noqa: BLE001 -- recorded, never retried
            out.reason, out.detail = "driver_error", f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            try:
                self.guard("end")
            except Exception:  # noqa: BLE001
                pass
            stack.port.disarm()
            self.close_dialog()
            out.seconds = round(time.time() - started, 1)
        return out

    def _operate(self, entry, operation, out, actor):
        stack = self.stack
        self.open_page(entry["route"])
        if operation == "update":
            button = self.first_row_button([entry["edit_label"], *EDIT_LABELS] if entry["edit_label"] else EDIT_LABELS)
            if button is None:
                out.reason, out.detail = "not_reachable", "no edit button on the first row (no record or no such control)"
                return
        else:
            button = self.page.locator("button", has_text=entry["create_label"] or "新增").first
            if not button.count():
                out.reason, out.detail = "not_reachable", "no create button"
                return
        self.press(button)
        raw = self.wait_form(operation)
        if raw is None:
            raise ContractRefused("form_data_unreadable", getattr(self, "last_unreadable", "unknown"))
        loaded = FormData.from_json(raw)
        if operation == "update" and loaded.mode != "update":
            out.reason, out.detail = "not_reachable", "the dialog is not an edit form"
            return
        if operation == "create" and loaded.mode != "create":
            out.reason, out.detail = "not_reachable", "the dialog is not a create form"
            return
        marked = self.page.evaluate(MARK_FIELD, ".el-dialog:last-of-type form.el-form, .el-dialog form.el-form")
        label = ""
        if marked is None:
            out.reason, out.detail = "no_editable_field", "no plain text field to change"
            return
        label = marked["label"]
        control = self.page.locator('[data-w46="1"]').first
        control.fill((marked["value"] or "") + ("" if operation == "update" else "") + "w46")
        if operation == "create":
            self.fill_required()
        self.page.wait_for_timeout(300)
        raw = self.read_form()
        if raw is None or raw.get("unreadable"):
            raise ContractRefused("form_data_unreadable", (raw or {}).get("unreadable", "after_typing"))
        current = FormData.from_json(raw)
        changed = diff_models(loaded.model, current.model)
        out.changed = sorted({c.field for c in changed})
        requested = frozenset(item.prop.split(".")[0] for item in current.items if item.label == label)
        button = self.submit_button()
        if button is None:
            out.reason, out.detail = "no_submit_control", "no visible submit control"
            return
        card = build_card(loaded=loaded, form=current, page=entry["route"], button=re.sub(r"\s+", "", button.inner_text()),
                          mode=operation, record_id=current.record_id, requested=requested)
        out.card = {"target": card.target, "changes": [[d.label, d.text] for d in card.details() if d.kind == "change"],
                    "derived": [[d.label, d.text] for d in card.details() if d.kind == "derived"]}
        port = stack.port
        prefixes = port.prefixes(port.recent_reads(), mode=card.mode, record_id=card.record_id)
        if not prefixes:
            raise ContractRefused("entity_unknown", "the page made no read that names the entity")
        baseline = port.read_record(prefixes, card.record_id) if card.mode == "update" else None
        if card.mode == "update" and baseline is None:
            raise ContractRefused("record_unreadable", "the record could not be read as the same person")
        permit = SubmitPermit(actor, card, prefixes, port.catalog(prefixes), baseline,
                              (lambda: port.read_record(prefixes, card.record_id)) if card.mode == "update" else None,
                              dry_run=True)
        port.arm(permit)
        entered = self.guard("begin")
        if entered["status"] != 200 or (entered["body"] or {}).get("guard") != "approved_submit":
            raise ContractRefused("permit_not_active", json.dumps(entered)[:200])
        button.click()
        self.page.wait_for_timeout(2500)
        self.guard("end")
        port.disarm()
        out.requests = [{"seq": r.sequence, "method": r.method, "path": r.path, "decision": r.decision,
                         "fields": list(r.fields), "detail": list(r.detail)} for r in permit.log]
        out.passed, out.reason = self._judge(permit)

    @staticmethod
    def _judge(permit):
        captured = permit.captured()
        other = [r for r in permit.log if r.decision not in {"would_allow", "duplicate"}]
        if captured and not other and permit.complete():
            return True, ""
        if not permit.log:
            return False, "no_request"
        if any(r.decision == "body_differs_from_form" for r in permit.log):
            return False, "transform_before_submit"
        if len({(r.method, r.path) for r in permit.log if r.decision != "duplicate"}) > 1 or (captured and other):
            return False, "multiple_requests"
        if captured and not permit.complete():
            return False, "change_not_covered"
        return False, "other:" + (other[0].decision if other else "unknown")
