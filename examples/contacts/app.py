"""The synthetic Contacts application this example integrates.

A real integration deletes this file: the application already exists. It is here so the
example runs end to end without provisioning anything, and so the adapter next door has
something concrete to bind to. It serves one list page and one record page per contact,
plus the same-origin PageController the SPI requires the application itself to host.
"""
from __future__ import annotations

from pathlib import Path

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files



DEMO_CONTACTS = (
    {"id": "ada-lovelace", "name": "Ada Lovelace", "email": "ada@example.test"},
    {"id": "grace-hopper", "name": "Grace Hopper", "email": "grace@example.test"},
    {"id": "alan-turing", "name": "Alan Turing", "email": "alan@example.test"},
)

#: The two credentials a writeable integration needs, in their smallest honest form. A real
#: application already has both: a browser session it set at sign-in, and a delegated
#: credential the user issued for an integration. They are separate on purpose -- the point
#: of binding them is that neither one alone may authorise a write.
DEMO_SESSION_COOKIE = "contacts_session"
DEMO_SESSION_VALUE = "session-ada-northstar"
DEMO_API_TOKEN = "token-ada-northstar"
#: Who both of them resolve to. A framework that could not compare these would be trusting
#: whichever side happened to answer first.
DEMO_ACTOR = {"workspace": "northstar", "actor": "ada-lovelace"}
_MODEL_ENV_KEYS = (
    "AGENT_OPENAI_BASE_URL",
    "AGENT_OPENAI_API_KEY",
    "AGENT_OPENAI_MODEL_PRO",
    "AGENT_PROVIDER",
)
def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def _html(source_contacts: Sequence[Mapping[str, str]]) -> bytes:
    contacts = json.dumps(source_contacts, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Minimal Contacts</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; color: #172033; background: #f4f7fb; }}
    header {{ height: 64px; display: flex; align-items: center; gap: 12px; padding: 0 32px;
      color: white; background: #172554; box-shadow: 0 1px 12px #0f172a24; }}
    header strong {{ font-size: 17px; letter-spacing: .01em; }}
    header span {{ color: #bfdbfe; font-size: 13px; }}
    main {{ width: min(900px, calc(100% - 48px)); margin: 44px auto; }}
    .eyebrow {{ color: #2563eb; font-size: 12px; font-weight: 750; letter-spacing: .12em;
      text-transform: uppercase; }}
    h1 {{ margin: 8px 0 10px; font-size: clamp(30px, 4vw, 46px); letter-spacing: -.04em; }}
    .lede {{ margin: 0 0 28px; color: #64748b; max-width: 620px; line-height: 1.6; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 16px; }}
    .card {{ display: block; padding: 22px; color: inherit; text-decoration: none; background: white;
      border: 1px solid #dbe4f0; border-radius: 18px; box-shadow: 0 12px 35px #3341550d;
      transition: transform .15s ease, border-color .15s ease; }}
    .card:hover {{ transform: translateY(-2px); border-color: #93c5fd; }}
    .name {{ display: block; font-weight: 760; font-size: 18px; margin-bottom: 8px; }}
    .email {{ color: #64748b; }}
    .back {{ display: inline-block; margin-bottom: 28px; color: #2563eb; text-decoration: none; }}
    .record {{ max-width: 620px; padding: 32px; background: white; border: 1px solid #dbe4f0;
      border-radius: 22px; box-shadow: 0 16px 44px #33415512; }}
    dt {{ margin-top: 22px; color: #64748b; font-size: 12px; font-weight: 750;
      letter-spacing: .09em; text-transform: uppercase; }}
    dd {{ margin: 7px 0 0; font-size: 18px; }}
  </style>
</head>
<body>
  <header><strong>Northstar Contacts</strong><span>Synthetic local workspace</span></header>
  <main id="app"></main>
  <script>
    // Server-rendered for the first paint, then refetched on every navigation. A snapshot
    // captured at load is how a single-page app comes to show a record it was told about
    // but has never fetched -- which is exactly what a freshly created contact is.
    let contacts = {contacts};
    const app = document.querySelector('#app');
    async function refresh() {{
      try {{
        const response = await fetch('/api/contacts', {{cache: 'no-store'}});
        const payload = await response.json();
        if (Array.isArray(payload.contacts)) contacts = payload.contacts;
      }} catch {{ /* keep the last good list; render() still has something to show */ }}
      render();
    }}
    function render() {{
      const match = /^\\/contacts\\/([^/]+)$/.exec(location.pathname);
      if (match) {{
        const contact = contacts.find(item => item.id === match[1]);
        if (!contact) {{ app.innerHTML = '<h1>Contact not found</h1>'; return; }}
        app.innerHTML = `<a class="back" href="/contacts" data-route>← All contacts</a>
          <section class="record"><div class="eyebrow">Contact record</div>
          <h1>${{contact.name}}</h1><dl><dt>Email</dt><dd>${{contact.email}}</dd>
          <dt>Record ID</dt><dd>${{contact.id}}</dd></dl></section>`;
        document.title = `${{contact.name}} · Minimal Contacts`;
        return;
      }}
      app.innerHTML = `<div class="eyebrow">Developer demo</div><h1>Contacts</h1>
        <p class="lede">A small first-party app whose current records come from a typed API.
        Ask the embedded assistant to list a contact or open a record.</p><section class="grid">${{
          contacts.map(item => `<a class="card" href="/contacts/${{item.id}}" data-route>
            <span class="name">${{item.name}}</span><span class="email">${{item.email}}</span></a>`).join('')
        }}</section>`;
      document.title = 'Minimal Contacts';
    }}
    addEventListener('popstate', refresh);
    addEventListener('click', event => {{
      const link = event.target.closest('a[data-route]');
      if (!link) return;
      event.preventDefault(); history.pushState({{}}, '', link.href); refresh();
    }});
    render();
  </script>
</body>
</html>
""".encode("utf-8")


class MinimalContactsApplication:
    """Loopback first-party application and typed API backed by synthetic immutable data."""

    def __init__(self, *, contacts: Sequence[Mapping[str, str]] = DEMO_CONTACTS):
        # Mutable now: the example gained a write, and a store that cannot change cannot
        # demonstrate one. `_lock` is what keeps the create and the readback consistent.
        self.contacts = [{key: str(value) for key, value in item.items()}
                         for item in contacts]
        if not self.contacts or any(set(item) != {"id", "name", "email"}
                                    for item in self.contacts):
            raise ValueError("contacts must contain id, name, and email")
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def _reply(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _actor(self) -> dict | None:
                """Who this request is, by whichever credential it carried."""
                cookies = self.headers.get("Cookie") or ""
                authorization = self.headers.get("Authorization") or ""
                session = any(
                    piece.strip() == f"{DEMO_SESSION_COOKIE}={DEMO_SESSION_VALUE}"
                    for piece in cookies.split(";"))
                token = authorization == f"Bearer {DEMO_API_TOKEN}"
                if not (session or token):
                    return None
                # The assertion carries identifiers and nothing else. A page that could read
                # its own credential back out of this would be a credential in the DOM.
                return {**DEMO_ACTOR, "source": "page_session" if session else "delegated_api"}

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                actor = self._actor()
                if path != "/api/contacts":
                    self._reply(404, b"not found\n", "text/plain; charset=utf-8")
                    return
                if actor is None or actor["source"] != "delegated_api":
                    # A write takes the delegated credential, never the browser session:
                    # a page that could write with its own cookie is a CSRF away from a
                    # write nobody asked for.
                    self._reply(401, _json_bytes({"error": "delegated_token_required"}),
                                "application/json")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(min(length, 64 * 1024)) or b"{}")
                except (ValueError, OSError):
                    self._reply(400, _json_bytes({"error": "invalid_json"}),
                                "application/json")
                    return
                name = str(payload.get("name") or "").strip()
                email = str(payload.get("email") or "").strip().casefold()
                if not name or not email or set(payload) != {"name", "email"}:
                    self._reply(400, _json_bytes({"error": "name_and_email_required"}),
                                "application/json")
                    return
                with owner._lock:
                    identifier = urllib.parse.quote(
                        name.casefold().replace(" ", "-"), safe="")
                    if any(item["id"] == identifier for item in owner.contacts):
                        self._reply(409, _json_bytes({"error": "already_exists"}),
                                    "application/json")
                        return
                    record = {"id": identifier, "name": name, "email": email}
                    owner.contacts.append(record)
                self._reply(201, _json_bytes({"contact": record}), "application/json")

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                if path == "/api/whoami":
                    actor = self._actor()
                    if actor is None:
                        self._reply(401, _json_bytes({"error": "unauthenticated"}),
                                    "application/json")
                    else:
                        self._reply(200, _json_bytes(actor), "application/json")
                    return
                if path == "/healthz":
                    self._reply(200, _json_bytes({"ready": True}), "application/json")
                elif path == "/api/contacts":
                    with owner._lock:
                        records = [dict(item) for item in owner.contacts]
                    self._reply(200, _json_bytes({"contacts": records}),
                                "application/json; charset=utf-8")
                elif path == "/woa/page-controller.js":
                    # The application serves its own PageController from its own origin,
                    # which is what the SPI requires of any host. Read from the example's
                    # directory rather than from an installed package: this example is run
                    # from a checkout and is deliberately not shipped with the framework.
                    source = (Path(__file__).parent / "assets"
                              / "page_controller.js").read_bytes()
                    self._reply(200, source, "text/javascript; charset=utf-8")
                elif path == "/contacts" or path.startswith("/contacts/"):
                    self._reply(200, _html(owner.contacts), "text/html; charset=utf-8")
                elif path == "/":
                    self.send_response(302)
                    self.send_header("Location", "/contacts")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._reply(404, b"not found\n", "text/plain; charset=utf-8")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "MinimalContactsApplication":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever, name="minimal-contacts", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        if self._thread is None:
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._thread = None

    def __enter__(self) -> "MinimalContactsApplication":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()
