"""The synthetic Contacts application this example integrates.

A real integration deletes this file: the application already exists. It is here so the
example runs end to end without provisioning anything, and so the adapter next door has
something concrete to bind to. It serves one list page and one record page per contact,
plus the same-origin PageController the SPI requires the application itself to host.

It also has the two things an application's own authority is made of, in their smallest
form: actors with different permissions, and workspaces whose records are invisible to each
other. Both workspaces deliberately hold contacts with the same names -- an integration that
leaks across workspaces, or lets a record it cannot see decide that something "already
exists", is found out by exactly that.
"""
from __future__ import annotations

from pathlib import Path

import html
import json
import socket
import threading
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer



DEMO_CONTACTS = (
    {"id": "ada-lovelace", "name": "Ada Lovelace", "email": "ada@example.test"},
    {"id": "grace-hopper", "name": "Grace Hopper", "email": "grace@example.test"},
    {"id": "alan-turing", "name": "Alan Turing", "email": "alan@example.test"},
)
#: Another organisation's records. Two names repeat names above on purpose.
SOUTHWIND_CONTACTS = (
    {"id": "grace-hopper", "name": "Grace Hopper", "email": "grace.hopper@southwind.test"},
    {"id": "radia-perlman", "name": "Radia Perlman", "email": "radia@southwind.test"},
    {"id": "katherine-johnson", "name": "Katherine Johnson",
     "email": "katherine@southwind.test"},
)
DEMO_WORKSPACE = "northstar"
OTHER_WORKSPACE = "southwind"

#: The two credentials a writeable integration needs, in their smallest honest form. A real
#: application already has both: a browser session it set at sign-in, and a delegated
#: credential the user issued for an integration. They are separate on purpose -- the point
#: of binding them is that neither one alone may authorise a write.
DEMO_SESSION_COOKIE = "contacts_session"
DEMO_SESSION_VALUE = "session-ada-northstar"
DEMO_API_TOKEN = "token-ada-northstar"
#: Who both of them resolve to. A framework that could not compare these would be trusting
#: whichever side happened to answer first.
DEMO_ACTOR = {"workspace": DEMO_WORKSPACE, "actor": "ada-lovelace"}


@dataclass(frozen=True, slots=True)
class DemoActor:
    """One synthetic user: where they work, how they sign in, and what they may do."""

    actor: str
    workspace: str
    session: str
    token: str
    permissions: tuple[str, ...]


DEMO_ACTORS = (
    DemoActor("ada-lovelace", DEMO_WORKSPACE, DEMO_SESSION_VALUE, DEMO_API_TOKEN,
              ("contacts:read", "contacts:write")),
    #: Same workspace, and the application lets her read and nothing else.
    DemoActor("rosalind-franklin", DEMO_WORKSPACE, "session-rosalind-northstar",
              "token-rosalind-northstar", ("contacts:read",)),
    DemoActor("mary-jackson", OTHER_WORKSPACE, "session-mary-southwind",
              "token-mary-southwind", ("contacts:read", "contacts:write")),
    #: Signed in, and allowed nothing: the application refuses her reads on its own.
    DemoActor("hedy-lamarr", DEMO_WORKSPACE, "session-hedy-northstar",
              "token-hedy-northstar", ()),
)
ACTORS_BY_ID = {item.actor: item for item in DEMO_ACTORS}
_MODEL_ENV_KEYS = (
    "AGENT_OPENAI_BASE_URL",
    "AGENT_OPENAI_API_KEY",
    "AGENT_OPENAI_MODEL",
    "AGENT_PROVIDER",
)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


_ASSISTANT_LOADER = b"""
(() => {
  if (window.top !== window || window.__WOA_NATIVE_BOOTSTRAP_STARTED__) return;
  window.__WOA_NATIVE_BOOTSTRAP_STARTED__ = true;
  let instanceId;
  try {
    instanceId = sessionStorage.getItem('woa.runtime.instance.v1');
    if (!instanceId) {
      instanceId = crypto.randomUUID().replaceAll('-', '');
      sessionStorage.setItem('woa.runtime.instance.v1', instanceId);
    }
  } catch {
    instanceId = crypto.randomUUID().replaceAll('-', '');
  }
  window.__WOA_RUNTIME_READY__ = fetch('/woa/bootstrap', {
    method: 'POST', credentials: 'same-origin', cache: 'no-store',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({runtime_instance_id: instanceId}),
  }).then(response => {
    if (!response.ok) throw new Error(`assistant bootstrap failed (${response.status})`);
    return response.json();
  }).then(payload => import(payload.runtime_module).then(
    module => module.startEmbeddedRuntime(payload.config)));
})();
"""


def _html(source_contacts: Sequence[Mapping[str, str]], workspace: str, *,
          assistant_enabled: bool = False) -> bytes:
    contacts = json.dumps(source_contacts, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape(workspace.title())
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
  <header><strong>{title} Contacts</strong><span>Synthetic local workspace</span></header>
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
  {('<script src="/woa/loader.js"></script>' if assistant_enabled else '')}
</body>
</html>
""".encode("utf-8")


def _validated(contacts: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    records = [{key: str(value) for key, value in item.items()} for item in contacts]
    if any(set(item) != {"id", "name", "email"} for item in records):
        raise ValueError("contacts must contain id, name, and email")
    return records


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Do not turn an ordinary browser navigation into an operator traceback."""

    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        import sys

        if isinstance(sys.exception(), (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class MinimalContactsApplication:
    """Loopback first-party application and typed API backed by synthetic resettable data."""

    WRITE_FAULTS = frozenset({"", "hold_before_commit", "drop_response", "fail_readback"})

    def __init__(self, *, contacts: Sequence[Mapping[str, str]] = DEMO_CONTACTS,
                 other_contacts: Sequence[Mapping[str, str]] = SOUTHWIND_CONTACTS,
                 write_fault: str = "", port: int = 0):
        # Mutable now: the example gained a write, and a store that cannot change cannot
        # demonstrate one. `_lock` is what keeps the create and the readback consistent.
        self.workspaces = {DEMO_WORKSPACE: _validated(contacts),
                           OTHER_WORKSPACE: _validated(other_contacts)}
        if not self.workspaces[DEMO_WORKSPACE]:
            raise ValueError("contacts must contain id, name, and email")
        #: The demo workspace's records, which is what a single-workspace reader expects.
        self.contacts = self.workspaces[DEMO_WORKSPACE]
        if write_fault not in self.WRITE_FAULTS:
            raise ValueError("write_fault is invalid")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer from 0 to 65535")
        self.write_fault = write_fault
        #: Every POST this application received, refused or not.
        self.write_requests = 0
        #: POSTs the application's own authentication or permissions turned away.
        self.refused_writes = 0
        #: POSTs from a permitted actor that reached the store.
        self.post_requests = 0
        self.records_created = 0
        self.post_received = threading.Event()
        self.release_post = threading.Event()
        if write_fault != "hold_before_commit":
            self.release_post.set()
        self._lock = threading.Lock()
        self._assistant_bootstrap: Callable[[DemoActor, str], dict] | None = None
        self._assistant_revoke: Callable[[DemoActor], None] | None = None
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
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self, location: str, *, cookie: str = "") -> None:
                self.send_response(302)
                self.send_header("Location", location)
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _actor(self) -> tuple[DemoActor, str] | None:
                """Who this request is, by whichever credential it carried."""
                cookies = self.headers.get("Cookie") or ""
                authorization = self.headers.get("Authorization") or ""
                pieces = {piece.strip() for piece in cookies.split(";")}
                for actor in DEMO_ACTORS:
                    if f"{DEMO_SESSION_COOKIE}={actor.session}" in pieces:
                        return actor, "page_session"
                    if authorization == f"Bearer {actor.token}":
                        return actor, "delegated_api"
                return None

            def _refuse_write(self, status: int, error: str) -> None:
                with owner._lock:
                    owner.refused_writes += 1
                self._reply(status, _json_bytes({"error": error}), "application/json")

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                if path == "/woa/bootstrap":
                    resolved = self._actor()
                    if (owner._assistant_bootstrap is None or resolved is None
                            or resolved[1] != "page_session"):
                        self._reply(401, _json_bytes({"error": "unauthenticated"}),
                                    "application/json")
                        return
                    if self.headers.get("Origin") != owner.origin:
                        self._reply(403, _json_bytes({"error": "origin_refused"}),
                                    "application/json")
                        return
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                        if not 1 <= length <= 1024:
                            raise ValueError()
                        payload = json.loads(self.rfile.read(length))
                        if not isinstance(payload, dict):
                            raise ValueError()
                        instance_id = payload.get("runtime_instance_id")
                        if (set(payload) != {"runtime_instance_id"}
                                or not isinstance(instance_id, str)
                                or not 8 <= len(instance_id) <= 128
                                or any(not (char.isalnum() or char in "-_")
                                       for char in instance_id)):
                            raise ValueError()
                        bootstrap = owner._assistant_bootstrap(resolved[0], instance_id)
                    except (ValueError, OSError):
                        self._reply(400, _json_bytes({"error": "invalid_bootstrap"}),
                                    "application/json")
                        return
                    except Exception:
                        self._reply(503, _json_bytes({"error": "assistant_unavailable"}),
                                    "application/json")
                        return
                    self._reply(200, _json_bytes(bootstrap), "application/json")
                    return
                if path == "/woa/logout":
                    resolved = self._actor()
                    if (self.headers.get("Origin") != owner.origin or resolved is None
                            or resolved[1] != "page_session"):
                        self._reply(403, _json_bytes({"error": "origin_refused"}),
                                    "application/json")
                        return
                    if owner._assistant_revoke is not None:
                        owner._assistant_revoke(resolved[0])
                    self.send_response(204)
                    self.send_header(
                        "Set-Cookie",
                        f"{DEMO_SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with owner._lock:
                    owner.write_requests += 1
                resolved = self._actor()
                if path != "/api/contacts":
                    self._refuse_write(404, "not_found")
                    return
                if resolved is None or resolved[1] != "delegated_api":
                    # A write takes the delegated credential, never the browser session:
                    # a page that could write with its own cookie is a CSRF away from a
                    # write nobody asked for.
                    self._refuse_write(401, "delegated_token_required")
                    return
                actor = resolved[0]
                if "contacts:write" not in actor.permissions:
                    # The application's own authority, independent of anything an
                    # integration decided before sending the request.
                    self._refuse_write(403, "forbidden")
                    return
                with owner._lock:
                    owner.post_requests += 1
                owner.post_received.set()
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
                if not owner.release_post.wait(timeout=10):
                    self._reply(503, _json_bytes({"error": "write_hold_timeout"}),
                                "application/json")
                    return
                with owner._lock:
                    store = owner.workspaces[actor.workspace]
                    identifier = urllib.parse.quote(
                        name.casefold().replace(" ", "-"), safe="")
                    # Uniqueness is the workspace's, like every other rule about its records.
                    if any(item["id"] == identifier for item in store):
                        self._reply(409, _json_bytes({"error": "already_exists"}),
                                    "application/json")
                        return
                    record = {"id": identifier, "name": name, "email": email}
                    store.append(record)
                    owner.records_created += 1
                if owner.write_fault == "drop_response":
                    # The application committed, then the transport disappeared before an
                    # acknowledgement.  This is deliberately a broken HTTP exchange: the
                    # adapter must report an unknown outcome and must never retry it.
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                self._reply(201, _json_bytes({"contact": record}), "application/json")

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                resolved = self._actor()
                if path == "/api/whoami":
                    if resolved is None:
                        self._reply(401, _json_bytes({"error": "unauthenticated"}),
                                    "application/json")
                    else:
                        actor, source = resolved
                        # The assertion carries identifiers and permissions, and nothing
                        # else. A page that could read its own credential back out of this
                        # would be a credential in the DOM.
                        self._reply(200, _json_bytes({
                            "workspace": actor.workspace, "actor": actor.actor,
                            "source": source, "permissions": list(actor.permissions),
                        }), "application/json")
                    return
                if path == "/woa/loader.js" and owner._assistant_bootstrap is not None:
                    self._reply(200, _ASSISTANT_LOADER,
                                "text/javascript; charset=utf-8")
                    return
                if path.startswith("/demo/login/"):
                    actor_id = urllib.parse.unquote(path.removeprefix("/demo/login/"))
                    actor = ACTORS_BY_ID.get(actor_id)
                    if actor is None:
                        self._reply(404, b"not found\n", "text/plain; charset=utf-8")
                        return
                    self._redirect(
                        "/contacts",
                        cookie=(f"{DEMO_SESSION_COOKIE}={actor.session}; Path=/; "
                                "HttpOnly; SameSite=Strict"))
                    return
                if path == "/healthz":
                    self._reply(200, _json_bytes({"ready": True}), "application/json")
                elif path == "/api/contacts":
                    if resolved is None:
                        self._reply(401, _json_bytes({"error": "unauthenticated"}),
                                    "application/json")
                        return
                    if "contacts:read" not in resolved[0].permissions:
                        self._reply(403, _json_bytes({"error": "forbidden"}),
                                    "application/json")
                        return
                    if owner.write_fault == "fail_readback" and owner.records_created:
                        self._reply(503, _json_bytes({"error": "readback_unavailable"}),
                                    "application/json; charset=utf-8")
                        return
                    with owner._lock:
                        records = [dict(item)
                                   for item in owner.workspaces[resolved[0].workspace]]
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
                    if resolved is None or resolved[1] != "page_session":
                        self._reply(401, b"<!doctype html><title>Sign in</title>"
                                         b"<h1>Sign in required</h1>\n",
                                    "text/html; charset=utf-8")
                        return
                    actor = resolved[0]
                    if "contacts:read" not in actor.permissions:
                        self._reply(403, b"<!doctype html><title>Forbidden</title>"
                                         b"<h1>Not permitted</h1>\n",
                                    "text/html; charset=utf-8")
                        return
                    with owner._lock:
                        records = [dict(item) for item in owner.workspaces[actor.workspace]]
                    self._reply(200, _html(
                        records, actor.workspace,
                        assistant_enabled=owner._assistant_bootstrap is not None),
                                "text/html; charset=utf-8")
                elif path == "/":
                    self._redirect("/contacts")
                else:
                    self._reply(404, b"not found\n", "text/plain; charset=utf-8")

        self._server = _QuietThreadingHTTPServer(("127.0.0.1", port), Handler)
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

    def install_assistant(self, *, bootstrap: Callable[[DemoActor, str], dict],
                          revoke: Callable[[DemoActor], None]) -> None:
        """Install server-side hooks; the browser can never supply an actor or token."""
        if not callable(bootstrap) or not callable(revoke):
            raise TypeError("assistant bootstrap and revoke hooks must be callable")
        with self._lock:
            if self._assistant_bootstrap is not None:
                raise RuntimeError("assistant hooks are already installed")
            self._assistant_bootstrap = bootstrap
            self._assistant_revoke = revoke

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
