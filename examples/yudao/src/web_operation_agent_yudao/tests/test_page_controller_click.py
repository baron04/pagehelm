"""The application's page controller clicks and types as the generic controller does, sooner.

The generic controller pauses before a click to move a pointer that only its mask draws.
The application's module sends the same events without that pause. Both run here in a real
browser over the same page, and what the page received is compared event by event.
"""
import os
import unittest
from pathlib import Path

_GENERIC_PATH = os.environ.get("WOA_PAGE_CONTROLLER_MODULE", "")
GENERIC = Path(_GENERIC_PATH).expanduser() if _GENERIC_PATH else None
OWNED = Path(__file__).resolve().parents[5] / "frontend" / "src" / "integrations" / "yudao"
ORIGIN = "http://synthetic.test"

PAGE = """<!doctype html><meta charset="utf-8"><body style="margin:40px">
<button id="first">甲</button> <button id="second"><span id="inner">乙</span></button>
<input id="name" value="旧名称"> <textarea id="note"></textarea>
<div style="height:3000px"></div><button id="below">丙</button></body>"""

DRIVE = """
async mode => {
  const generic = await import('/woa/page-controller.js')
  const owned = await import('/woa/yudao-page-controller.js')
  const kinds = ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'pointerdown', 'mousedown',
    'focus', 'pointerup', 'mouseup', 'click', 'pointerout', 'pointerleave', 'mouseout', 'mouseleave',
    'blur', 'input', 'change']
  const log = []
  for (const kind of kinds) {
    document.addEventListener(kind, event => log.push(`${kind}@${event.target.id || event.target.nodeName}`
      + (event instanceof MouseEvent ? `(${Math.round(event.clientX)},${Math.round(event.clientY)},${event.button})` : '')), true)
  }
  const by = id => document.getElementById(id)
  const click = mode === 'generic' ? generic.clickElement : owned.clickElement
  const type = mode === 'generic' ? generic.inputTextElement : owned.inputTextElement
  const began = performance.now()
  await click(by('first'))
  await click(by('second'))
  await type(by('name'), '新名称')
  await type(by('note'), '两行\\n备注')
  await click(by('below'))
  await click(by('first'))
  return {log, ms: performance.now() - began, name: by('name').value, note: by('note').value,
    active: document.activeElement.id, scrolled: window.scrollY > 0}
}
"""


@unittest.skipUnless(GENERIC is not None and GENERIC.is_file(),
                     "WOA_PAGE_CONTROLLER_MODULE does not name an installed module")
@unittest.skipUnless((OWNED / "yudao-page-controller.js").is_file(),
                     "the application's page controller module is not in this checkout")
class PageControllerClickTests(unittest.TestCase):
    def test_plain_html_uses_ordinary_diagnostics_even_when_component_bindings_are_enabled(self):
        from playwright.sync_api import sync_playwright
        from urllib.parse import urlsplit
        playwright = self.enterContext(sync_playwright())
        browser = playwright.chromium.launch()
        self.addCleanup(browser.close)
        context = browser.new_context()
        self.addCleanup(context.close)
        files = {"/woa/page-controller.js":GENERIC,
                 "/woa/yudao-page-controller.js":OWNED / "yudao-page-controller.js",
                 "/woa/page-facts.js":OWNED / "page-facts.js"}
        def serve(route):
            path = urlsplit(route.request.url).path
            route.fulfill(body=files[path].read_bytes() if path in files else PAGE,
                          content_type="text/javascript; charset=utf-8" if path in files else "text/html; charset=utf-8")
        context.route("**/*", serve)
        page = context.new_page()
        page.goto(ORIGIN)
        result = page.evaluate("""async () => {
          const api=await import('/woa/yudao-page-controller.js?bindings=1')
          const controller=new api.PageController({enableMask:false})
          const find=id=>[...controller.elementTextMap.keys()].find(index=>
            api.getElementByIndex(controller.selectorMap,index)?.id===id)
          try {
            const before=await controller.getBrowserState()
            const filled=await controller.inputText(find('name'),'New name')
            await controller.getBrowserState()
            const clicked=await controller.clickElement(find('first'))
            const after=await controller.getBrowserState()
            const random=crypto.getRandomValues
            let degraded
            try {
              Object.defineProperty(crypto,'getRandomValues',{configurable:true,value:()=>{throw new Error('collector failed')}})
              degraded=await controller.inputText(find('name'),'Second name')
            } finally {Object.defineProperty(crypto,'getRandomValues',{configurable:true,value:random})}
            return {before,after,fill:filled.execution_trace,click:clicked.execution_trace,
              name:document.getElementById('name').value,degraded,diagnostic:degraded.execution_trace??null}
          } finally {controller.dispose()}
        }""")
        self.assertEqual(result["name"], "Second name")
        self.assertTrue(result["degraded"]["success"])
        self.assertIsNone(result["diagnostic"])
        self.assertNotEqual(result["fill"]["trace_id"], result["click"]["trace_id"])
        for key, operation in (("fill","fill"),("click","click")):
            events = result[key]["events"]
            self.assertTrue(events)
            self.assertEqual([e["sequence"] for e in events], list(range(1,len(events)+1)))
            self.assertTrue(all(e["path"] == "ordinary" and e["operation"] == operation for e in events))
            self.assertNotIn(result[key]["trace_id"], str(result["before"]) + str(result["after"]))
        self.assertNotIn("组件动作回执:", result["after"]["content"])

    def test_disabled_binding_mode_keeps_the_generic_page_reading(self):
        from playwright.sync_api import sync_playwright
        playwright = self.enterContext(sync_playwright())
        browser = playwright.chromium.launch()
        self.addCleanup(browser.close)
        files = {"/woa/page-controller.js": GENERIC,
                 "/woa/yudao-page-controller.js": OWNED / "yudao-page-controller.js",
                 "/woa/page-facts.js": OWNED / "page-facts.js"}
        results = []
        for mode in ("generic", "owned"):
            context = browser.new_context()
            try:
                def serve(route):
                    path = route.request.url[len(ORIGIN):]
                    if path in files:
                        route.fulfill(body=files[path].read_bytes(), content_type="text/javascript")
                    else:
                        route.fulfill(body=PAGE, content_type="text/html")
                context.route("**/*", serve)
                page = context.new_page()
                page.goto(ORIGIN)
                results.append(page.evaluate("""async mode => {
                  const generic = await import('/woa/page-controller.js')
                  const owned = await import('/woa/yudao-page-controller.js')
                  const api = mode === 'generic' ? generic : owned
                  const controller = new api.PageController({enableMask:false})
                  try {return await controller.getBrowserState()}
                  finally {controller.dispose()}
                }""", mode))
            finally:
                context.close()
        self.assertEqual(results[0], results[1])

    def drive(self, browser, mode):
        files = {"/woa/page-controller.js": GENERIC, "/woa/yudao-page-controller.js": OWNED / "yudao-page-controller.js",
                 "/woa/page-facts.js": OWNED / "page-facts.js"}
        context = browser.new_context(viewport={"width": 1000, "height": 700})
        self.addCleanup(context.close)

        def serve(route):
            path = route.request.url[len(ORIGIN):]
            if path in files:
                route.fulfill(body=files[path].read_bytes(), content_type="text/javascript; charset=utf-8")
            elif path == "/":
                route.fulfill(body=PAGE, content_type="text/html; charset=utf-8")
            else:
                route.fulfill(status=404, body="")
        context.route("**/*", serve)
        page = context.new_page()
        page.goto(ORIGIN + "/")
        return page.evaluate(DRIVE, mode)

    def test_the_same_events_reach_the_same_targets_in_the_same_order_without_the_pointer_pause(self):
        from playwright.sync_api import sync_playwright
        playwright = self.enterContext(sync_playwright())
        browser = playwright.chromium.launch()
        self.addCleanup(browser.close)
        generic, owned = self.drive(browser, "generic"), self.drive(browser, "owned")
        self.assertEqual(owned["log"], generic["log"])
        self.assertEqual({k: owned[k] for k in ("name", "note", "active", "scrolled")},
                         {k: generic[k] for k in ("name", "note", "active", "scrolled")})
        self.assertEqual((owned["name"], owned["note"], owned["active"]), ("新名称", "两行\n备注", "first"))
        # What a click sends: the element left before, then this one entered, pressed and clicked.
        self.assertIn("pointerdown@inner", "".join(owned["log"]))     # the element under the pointer takes it
        self.assertIn("blur@name", owned["log"])
        # Six clicks, each without the 0.4 s spent on a pointer nobody is shown.
        self.assertGreater(generic["ms"] - owned["ms"], 6 * 400 * 0.8)
        self.assertGreater(owned["ms"], 6 * 200)                      # the pause after each click is kept


if __name__ == "__main__":
    unittest.main()
