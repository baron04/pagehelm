"""The person at the panel: opens it, asks, answers the cards, reads the reply."""
from __future__ import annotations

import time


class Panel:
    def __init__(self, page):
        self.page = page

    def open(self):
        self.page.wait_for_selector(".woa-launcher, #woa-composer-input", timeout=60000, state="attached")
        if not self.page.locator("#woa-composer-input").count():
            self.page.locator(".woa-launcher").click()
        self.page.wait_for_selector("#woa-composer-input", timeout=30000)

    def ask(self, text: str):
        self.before = self.turns()
        box = self.page.locator("#woa-composer-input")
        box.fill(text)
        box.press("Enter")

    def card(self):
        """The approval card on screen, read in one step so a card that goes away cannot half-read."""
        try:
            found = self.page.evaluate("""() => {
              const root = document.querySelector('#woa-assistant')?.shadowRoot
              const node = [...(root?.querySelectorAll('.woa-approval') ?? [])].find(n => n.getBoundingClientRect().width > 0)
              return node ? {id: node.getAttribute('data-approval-id'), kind: node.getAttribute('data-kind') || 'write',
                             text: node.innerText} : null
            }""")
        except Exception:  # noqa: BLE001
            return None
        if not found:
            return None
        return {**found, "node": self.page.locator(f'.woa-approval[data-approval-id="{found["id"]}"]').first}

    def running(self) -> bool:
        return self.page.locator('.woa-work[data-running="true"]').count() > 0

    def reply(self) -> str:
        results = self.page.locator(".woa-turn").last.locator(".woa-agent")
        return results.last.inner_text() if results.count() else ""

    def turns(self) -> int:
        return self.page.locator(".woa-turn").count()

    def run(self, decide, *, timeout_s: float = 180, quiet_s: float = 3.0, done=None) -> dict:
        """Answer each card with `decide(kind, text)` -> approve | deny | wait, until the turn ends."""
        answered, deadline, idle_since = [], time.time() + timeout_s, None
        while time.time() < deadline:
            card = self.card()
            if card is not None:
                action = decide(card["kind"], card["text"])
                if action == "wait":
                    time.sleep(0.3)
                    continue
                answered.append({"id": card["id"], "kind": card["kind"], "text": card["text"], "answer": action})
                try:
                    # A person scrolls the card into view before answering it.
                    card["node"].evaluate("n => n.scrollIntoView({block: 'start'})")
                    card["node"].locator(f'[data-testid="woa-{"approve" if action == "approve" else "deny"}"]').click(timeout=5000)
                except Exception:  # noqa: BLE001 -- the card went away: the next look decides
                    answered.pop()
                    continue
                idle_since = None
                time.sleep(0.5)
                continue
            if (not self.running() and self.turns() > self.before
                    and (self.page.locator(".woa-turn").last.locator(".woa-took").count()
                         or done is not None and done())):
                idle_since = idle_since or time.time()
                if time.time() - idle_since >= quiet_s:
                    break
            else:
                idle_since = None
            time.sleep(0.4)
        return {"cards": answered, "reply": self.reply(), "timed_out": time.time() >= deadline}
