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
import hashlib
import json
import re
import socket
import threading
import time
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
PAGE_MENUS = (
    {"key": "contacts", "label": "Contacts", "path": "/contacts"},
    {"key": "workflows", "label": "Vendor workspaces", "path": "/contacts/workflows"},
    {"key": "desk-partner", "label": "Partner desk", "path": "/contacts/workflows/partner"},
    {"key": "desk-supplier", "label": "Supplier desk", "path": "/contacts/workflows/supplier"},
    {"key": "help", "label": "Help", "path": "/contacts/help"},
)
_MODEL_ENV_KEYS = (
    "AGENT_OPENAI_BASE_URL",
    "AGENT_OPENAI_API_KEY",
    "AGENT_OPENAI_MODEL",
    "AGENT_PROVIDER",
)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def _assistant_tag(script_url: str | None) -> str:
    """The one tag a host page needs: `assistant.js`, told where this application's bootstrap is.

    The script obtains, renews and reconnects sessions itself from `/woa/bootstrap`, which
    turns the signed-in page session into an agent session; nothing here carries a credential.
    """
    if script_url is None:
        return ""
    return (f'<script type="module" src="{html.escape(script_url, quote=True)}" '
            'data-woa-bootstrap="/woa/bootstrap"></script>')


def _html(source_contacts: Sequence[Mapping[str, str]], workspace: str, *,
          can_write: bool = False, assistant_script: str | None = None,
          adversarial_form_fixture: bool = False, visible_pages: Sequence[str] | None = None) -> bytes:
    contacts = json.dumps(source_contacts, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape(workspace.title())
    menu = " ".join(f'<a href="{item["path"]}" data-route>{item["label"]}</a>'
                    for item in PAGE_MENUS if item["key"] in {"contacts", "workflows", "help"}
                    and (visible_pages is None or item["key"] in visible_pages))
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
    header nav {{ margin-left: auto; display: flex; gap: 16px; }}
    header a {{ color: #bfdbfe; text-decoration: none; }}
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
    form {{ display: grid; gap: 18px; margin-top: 24px; }}
    label {{ display: grid; gap: 7px; color: #475569; font-size: 13px; font-weight: 700; }}
    input {{ width: 100%; padding: 12px 14px; color: #172033; background: white;
      border: 1px solid #cbd5e1; border-radius: 10px; font: inherit; }}
    [data-adversarial-panel][hidden] {{ display: none; }}
    .adversarial-spacer {{ height: 1200px; }}
    [contenteditable] {{ min-height: 90px; padding: 12px; background: white;
      border: 1px solid #cbd5e1; border-radius: 10px; }}
    button, .primary {{ width: fit-content; padding: 11px 16px; color: white;
      background: #2563eb; border: 0; border-radius: 10px; font: inherit;
      font-weight: 750; cursor: pointer; text-decoration: none; }}
    .status {{ min-height: 22px; color: #b91c1c; }}
  </style>
</head>
<body>
  <header><strong>{title} Contacts</strong><span>Synthetic local workspace</span><nav>{menu}</nav></header>
  <main id="app"></main>
  <script>
    // Server-rendered for the first paint, then refetched on every navigation. A snapshot
    // captured at load is how a single-page app comes to show a record it was told about
    // but has never fetched -- which is exactly what a freshly created contact is.
    let contacts = {contacts};
    const canWrite = {str(bool(can_write)).lower()};
    const app = document.querySelector('#app');
    let filterRevision = 0;
    async function refresh() {{
      try {{
        const response = await fetch('/api/contacts', {{cache: 'no-store'}});
        const payload = await response.json();
        if (Array.isArray(payload.contacts)) contacts = payload.contacts;
      }} catch {{ /* keep the last good list; render() still has something to show */ }}
      render();
    }}
    function render() {{
      app.removeAttribute('data-active-view');
      if (location.pathname === '/contacts/filter') {{
        app.innerHTML = `<section data-page-route="/contacts/filter"><h1>Filter contacts</h1>
          <form id="contact-filter"><label>Name contains<input id="contact-filter-name"></label>
          <button type="button">Search contacts</button></form><div id="contact-filter-results"></div></section>`;
        const form = app.querySelector('#contact-filter');
        const apply = async () => {{
          const value = form.querySelector('input').value;
          const response = await fetch('/api/contacts', {{cache: 'no-store'}});
          if (!response.ok || !form.isConnected) return;
          const payload = await response.json();
          const rows = payload.contacts.filter(item => item.name.toLowerCase().includes(value.toLowerCase()));
          const digest = async text => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
            new TextEncoder().encode(text))), byte => byte.toString(16).padStart(2, '0')).join('');
          const fieldDigest = await digest(JSON.stringify([value]));
          const queryDigest = await digest(JSON.stringify({{name_contains: value}}));
          if (!form.isConnected) return;
          app.querySelector('#contact-filter-results').textContent = rows.map(item => item.name).join(' · ');
          form.dataset.pageRead = JSON.stringify({{path: '/api/contacts', query_digest: queryDigest,
            query_fields: {{name_contains: fieldDigest}}, revision: ++filterRevision,
            page: 1, page_size: 100, total: rows.length, rows: rows.length}});
        }};
        form.addEventListener('submit', event => event.preventDefault());
        form.querySelector('button').addEventListener('click', apply);
        void apply();
        return;
      }}
      if (location.pathname === '/contacts/help') {{
        app.innerHTML = `<a class="back" href="/contacts" data-route>← All contacts</a>
          <section class="record" data-page-route="/contacts/help"><div class="eyebrow">Workspace help</div><h1>Help</h1>
          <p class="lede">Open a contact to read its email. Contact changes need a workspace editor.</p>
          <a class="back" href="/contacts/workflows" data-route>Vendor workspaces</a></section>`;
        document.title = 'Help · Minimal Contacts';
        return;
      }}
      if ({str(adversarial_form_fixture).lower()} &&
          location.pathname === '/contacts/adversarial') {{
        app.innerHTML = `<a class="back" href="/contacts" data-route>← All contacts</a>
          <section class="record"><div class="eyebrow">Safety fixture</div>
          <h1>Adversarial current form</h1>
          <p class="lede">Every ordinary-looking field below is outside the adapter contract.</p>
          <nav role="tablist"><button type="button" role="tab" data-tab="general">General</button>
          <button type="button" role="tab" data-tab="dynamic">Dynamic details</button></nav>
          <div data-adversarial-panel="general"><label>客户参考 / Customer reference
          <input name="customer_reference"></label>
          <label>Rich text widget<div contenteditable="true" data-widget="richtext"></div></label>
          <label>Attachment widget<input name="attachment" type="file"></label></div>
          <div data-adversarial-panel="dynamic" hidden><label>Dynamic notebook value
          <input name="dynamic_value"></label></div>
          <div class="adversarial-spacer" aria-hidden="true"></div>
          <label>Field after scroll<input name="scrolled_value"></label>
          <p data-rerender-target>Original field rendering</p></section>`;
        const reference = app.querySelector('[name="customer_reference"]');
        reference.addEventListener('change', () => {{
          window.__adversarialEffects = Promise.all([
            fetch('/api/effects/database', {{method: 'POST'}}),
            fetch('/api/effects/external-http', {{method: 'POST'}}),
          ]);
          history.pushState({{}}, '', '/contacts/adversarial?changed=1');
          app.querySelector('[data-rerender-target]').textContent = 'Rerendered by onchange';
        }});
        for (const tab of app.querySelectorAll('[data-tab]')) tab.addEventListener('click', () => {{
          for (const panel of app.querySelectorAll('[data-adversarial-panel]'))
            panel.hidden = panel.dataset.adversarialPanel !== tab.dataset.tab;
        }});
        document.title = 'Adversarial form · Minimal Contacts';
        return;
      }}
      if (location.pathname === '/contacts/new') {{
        if (!canWrite) {{ app.innerHTML = '<h1>Not permitted</h1>'; return; }}
        app.dataset.activeView = 'Create';
        app.innerHTML = `<a class="back" href="/contacts" data-route>← All contacts</a>
          <section class="record"><div class="eyebrow">Contact record</div>
          <h1>New contact</h1><p class="lede">Review the values before you save.</p>
          <form id="contact-create" data-page-route="/contacts/new"><label>Name<input name="name" autocomplete="name"
            required maxlength="120"></label><label>Email<input name="email" type="email"
            autocomplete="email" required maxlength="200"></label>
            <div role="radiogroup" aria-label="Contact method">
              <label>Email only<input type="radio" name="local-contact-method" value="email" checked></label>
              <label>Phone call<input type="radio" name="local-contact-method" value="phone"></label>
            </div>
            <div id="contact-email-preferences"><label>Email preferences<input id="contact-draft-email-preferences"></label></div>
            <label>Daily limit<input id="contact-draft-limit" type="number"></label>
            <label>Follow-up date<input id="contact-draft-date" type="date"></label>
            <label>Period start<input id="contact-period-start" type="date"></label>
            <label>Period end<input id="contact-period-end" type="date"></label>
            <fieldset id="contact-draft-lines"><legend>Local lines</legend></fieldset>
            <button id="contact-add-line" type="button">Add local line</button>
            <label>Priority<input id="contact-draft-priority" role="combobox" readonly
              aria-haspopup="listbox" aria-controls="contact-priorities" aria-expanded="false"></label>
            <ul id="contact-priorities" role="listbox" hidden>
              <li role="option" aria-selected="false" tabindex="0">Normal</li>
              <li role="option" aria-selected="false" tabindex="0">Urgent</li>
            </ul>
            <button type="submit">Save contact</button><p class="status" role="status"></p>
          </form></section>`;
        document.title = 'New contact · Minimal Contacts';
        for (const radio of app.querySelectorAll('[name="local-contact-method"]'))
          radio.addEventListener('change', () => {{
            if (radio.checked && radio.value === 'phone') app.querySelector('#contact-email-preferences')?.remove();
          }});
        const start = app.querySelector('#contact-period-start');
        const end = app.querySelector('#contact-period-end');
        for (const input of [start, end]) input.addEventListener('change', () => {{
          // Plain HTML two-field commit, deliberately unlike Element Plus.
          if (start.value && end.value) app.querySelector('#contact-create').dataset.period = start.value + '/' + end.value;
        }});
        app.querySelector('#contact-add-line').addEventListener('click', () => {{
          const lines = app.querySelector('#contact-draft-lines');
          const number = lines.querySelectorAll('input').length + 1;
          const label = document.createElement('label'); label.textContent = 'Local line ' + number;
          const input = document.createElement('input'); input.id = 'contact-line-' + number;
          label.append(input); lines.append(label);
        }});
        const priority = app.querySelector('#contact-draft-priority');
        const choices = app.querySelector('#contact-priorities');
        priority.addEventListener('click', () => {{
          choices.hidden = !choices.hidden;
          priority.setAttribute('aria-expanded', String(!choices.hidden));
        }});
        for (const option of choices.querySelectorAll('[role=option]')) option.addEventListener('click', () => {{
          for (const peer of choices.querySelectorAll('[role=option]'))
            peer.setAttribute('aria-selected', String(peer === option));
          priority.value = option.textContent;
          choices.hidden = true; priority.setAttribute('aria-expanded', 'false');
        }});
        return;
      }}
      if (location.pathname === '/contacts/workflows') {{
        app.innerHTML = `<a class="back" href="/contacts" data-route>← All contacts</a>
          <div class="eyebrow">Read-only workspace</div><h1>Vendor workspaces</h1>
          <p class="lede">Choose a desk to inspect its current terms.</p>
          <section class="grid" data-page-route="/contacts/workflows"><a class="card" href="/contacts/workflows/partner" data-route>
            <span class="name">Partner desk</span></a>
          <a class="card" href="/contacts/workflows/supplier" data-route>
            <span class="name">Supplier desk</span></a></section>`;
        document.title = 'Vendor workspaces · Minimal Contacts';
        return;
      }}
      const workflow = /^\\/contacts\\/workflows\\/(partner|supplier)$/.exec(location.pathname);
      if (workflow) {{
        const desk = workflow[1];
        app.innerHTML = `<a class="back" href="/contacts/workflows" data-route>← Vendor workspaces</a>
          <section class="record" data-page-route="/contacts/workflows/${{desk}}"
            data-workflow-desk="${{desk}}" data-active-tab="overview">
          <div class="eyebrow">Read-only workspace</div><h1>${{desk === 'partner' ? 'Partner' : 'Supplier'}} desk</h1>
          <nav role="tablist"><button id="workflow-overview" type="button" role="tab" aria-selected="true" data-workflow-tab="overview">Overview</button>
          <button id="workflow-terms" type="button" role="tab" aria-selected="false" data-workflow-tab="terms">Terms</button></nav>
          <p data-workflow-content>Overview</p></section>`;
        for (const tab of app.querySelectorAll('[data-workflow-tab]'))
          tab.addEventListener('click', () => {{
            const section = app.querySelector('[data-workflow-desk]');
            section.dataset.activeTab = tab.dataset.workflowTab;
            for (const peer of section.querySelectorAll('[data-workflow-tab]'))
              peer.setAttribute('aria-selected', String(peer === tab));
            section.querySelector('[data-workflow-content]').textContent =
              tab.dataset.workflowTab === 'terms' ? 'Current terms' : 'Overview';
          }});
        document.title = 'Vendor desk · Minimal Contacts';
        return;
      }}
      const edit = /^\\/contacts\\/(edit|edit-locked)\\/([^/]+)$/.exec(location.pathname);
      if (edit) {{
        // The edit form reads its collection through the application's read
        // endpoint on open -- the read is the source a form read may vouch for.
        // The page paints from its snapshot while the read lands; it also applies
        // its own display capitalisation to the name, a self-edit the assistant
        // must restore before any save could be honest. The locked variant
        // recomputes on every entry, so the restore can never stick.
        const locked = edit[1] === 'edit-locked';
        void fetch('/api/contacts', {{cache: 'no-store'}})
          .then(response => response.ok ? response.json() : null)
          .then(payload => {{
            if (payload && Array.isArray(payload.contacts)) contacts = payload.contacts;
          }});
        const contact = contacts.find(item => item.id === edit[2]);
        if (!contact) {{ app.innerHTML = '<h1>Contact not found</h1>'; return; }}
        app.innerHTML = `<a class="back" href="/contacts" data-route>← All contacts</a>
          <section class="record" data-page-route="/contacts/${{edit[1]}}/${{contact.id}}">
          <div class="eyebrow">Edit contact</div><h1>${{contact.name}}</h1>
          <form id="contact-edit" data-record-id="${{contact.id}}">
            <label>Name <input name="name" required maxlength="120"></label>
            <label>Email <input name="email" type="email" required maxlength="200"></label>
            <button type="submit">Save contact</button><p class="status" role="status"></p>
          </form></section>`;
        document.title = `Edit ${{contact.name}} · Minimal Contacts`;
        const editForm = document.querySelector('#contact-edit');
        const nameField = editForm.elements.name;
        nameField.value = contact.name.toUpperCase();
        editForm.elements.email.value = contact.email;
        if (locked) nameField.addEventListener('input', () => {{
          nameField.value = nameField.value.toUpperCase();
        }});
        return;
      }}
      const match = /^\\/contacts\\/([^/]+)$/.exec(location.pathname);
      if (match) {{
        const contact = contacts.find(item => item.id === match[1]);
        if (!contact) {{ app.innerHTML = '<h1>Contact not found</h1>'; return; }}
        app.dataset.activeView = 'Detail';
        // A record's own operations sit in a collapsed menu, as many applications keep
        // them; only an account that may write is shown the menu at all.
        const menu = canWrite ? `<div class="record-menu">
          <button type="button" aria-haspopup="menu" aria-expanded="false"
            data-record-menu>More</button>
          <div role="menu" hidden data-record-menu-items>
            <button type="button" role="menuitem"
              data-delete-contact="${{contact.id}}">Delete</button></div></div>` : '';
        app.innerHTML = `<a class="back" href="/contacts" data-route>← All contacts</a>
          <section class="record" data-page-route="/contacts/${{contact.id}}"><div class="eyebrow">Contact record</div>
          <h1>${{contact.name}}</h1>${{menu}}<dl><dt>Email</dt><dd>${{contact.email}}</dd>
          <dt>Record ID</dt><dd>${{contact.id}}</dd></dl></section>`;
        document.title = `${{contact.name}} · Minimal Contacts`;
        return;
      }}
      app.dataset.activeView = 'List';
      app.innerHTML = `<div class="eyebrow">Developer demo</div><h1>Contacts</h1>
        <p class="lede">A small first-party app whose current records come from a typed API.
        Ask the embedded assistant to list a contact or open a record.</p>${{
          canWrite ? '<a class="primary" href="/contacts/new" data-route>New contact</a>' : ''
        }}<section class="grid" data-page-route="/contacts" style="margin-top: 20px">${{
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
    addEventListener('click', async event => {{
      const toggle = event.target.closest('[data-record-menu]');
      if (toggle) {{
        const items = toggle.parentElement.querySelector('[data-record-menu-items]');
        const open = toggle.getAttribute('aria-expanded') !== 'true';
        toggle.setAttribute('aria-expanded', String(open));
        items.hidden = !open;
        return;
      }}
      const remove = event.target.closest('[data-delete-contact]');
      if (!remove) return;
      // The person deletes, after the page's own confirmation, with the browser session.
      if (!confirm('Delete this contact?')) return;
      const response = await fetch(
        `/api/browser/contacts/${{encodeURIComponent(remove.dataset.deleteContact)}}/delete`,
        {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: '{{}}'}});
      if (response.ok) {{ history.replaceState({{}}, '', '/contacts'); await refresh(); }}
    }});
    addEventListener('submit', async event => {{
      if (event.target.id !== 'contact-create' && event.target.id !== 'contact-edit') return;
      event.preventDefault();
      const form = event.target;
      const status = form.querySelector('[role="status"]');
      const editing = form.id === 'contact-edit';
      const payload = editing
        ? {{contactId: form.dataset.recordId, ...Object.fromEntries(new FormData(form))}}
        : Object.fromEntries(new FormData(form));
      form.setAttribute('aria-busy', 'true');
      try {{
        const response = await fetch('/api/browser/contacts', {{
          method: editing ? 'PUT' : 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(payload),
        }});
        const result = await response.json();
        if (!response.ok || !result.contact) throw new Error(result.error || 'save_failed');
        if (editing) {{ Object.assign(contacts.find(item => item.id === result.contact.id), result.contact); }}
        else {{ contacts.push(result.contact); }}
        history.replaceState({{}}, '', `/contacts/${{result.contact.id}}`);
        await refresh();
      }} catch (error) {{ status.textContent = `Could not save: ${{error.message}}`; }}
      finally {{ form.removeAttribute('aria-busy'); }}
    }});
    render();
  </script>
  {_assistant_tag(assistant_script)}
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
                 write_fault: str = "", port: int = 0,
                 adversarial_form_fixture: bool = False):
        # Mutable now: the example gained a write, and a store that cannot change cannot
        # demonstrate one. `_lock` is what keeps the create and the readback consistent.
        self.workspaces = {DEMO_WORKSPACE: _validated(contacts),
                           OTHER_WORKSPACE: _validated(other_contacts)}
        #: Optional two-state subscription flags per workspace, keyed by contact id.
        self.subscriptions: dict[str, dict[str, str]] = {
            ws: {item["id"]: "enabled" for item in items}
            for ws, items in self.workspaces.items()
        }
        #: Synthetic notes, master-detail dossiers, and group relations for neutral CompiledWrites tests.
        self.contact_notes: dict[str, list[dict]] = {DEMO_WORKSPACE: [], OTHER_WORKSPACE: []}
        self.contact_dossiers: dict[str, list[dict]] = {
            DEMO_WORKSPACE: [
                {
                    "id": "dossier-1",
                    "name": "Ada Dossier",
                    "owner": "operator",
                    "note": "Keep safe",
                    "items": [
                        {"id": "item-1", "label": "Primary Phone", "value": "+44-100"},
                        {"id": "item-2", "label": "Office", "value": "London"},
                    ],
                }
            ],
            OTHER_WORKSPACE: [],
        }
        self.contact_groups: dict[str, dict[str, list[str]]] = {DEMO_WORKSPACE: {}, OTHER_WORKSPACE: {}}
        if not self.workspaces[DEMO_WORKSPACE]:
            raise ValueError("contacts must contain id, name, and email")
        #: The demo workspace's records, which is what a single-workspace reader expects.
        self.contacts = self.workspaces[DEMO_WORKSPACE]
        if write_fault not in self.WRITE_FAULTS:
            raise ValueError("write_fault is invalid")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be an integer from 0 to 65535")
        if not isinstance(adversarial_form_fixture, bool):
            raise ValueError("adversarial_form_fixture must be a boolean")
        self.adversarial_form_fixture = adversarial_form_fixture
        #: Application-owned menu revocation; tests can remove a page for one actor.
        self.hidden_pages: dict[str, set[str]] = {}
        self.write_fault = write_fault
        #: Every POST this application received, refused or not.
        self.write_requests = 0
        #: POSTs the application's own authentication or permissions turned away.
        self.refused_writes = 0
        #: POSTs from a permitted actor that reached the store.
        self.post_requests = 0
        self.records_created = 0
        self.records_deleted = 0
        #: Requests emitted by the hostile current-form fixture. They model effects an
        #: adapter cannot infer from an ordinary-looking DOM input.
        self.side_effect_requests = {"database": 0, "external-http": 0}
        self.side_effect_records: list[str] = []
        self.post_received = threading.Event()
        self.release_post = threading.Event()
        if write_fault != "hold_before_commit":
            self.release_post.set()
        self._lock = threading.Lock()
        #: (login session, window name) -> expiry. Any open window takes the session
        #: read-only, but a page's begin/end pair is its own: one tab releasing its
        #: window never lifts another's. Callers naming no window share the "" one.
        self._page_windows: dict[tuple[str, str], float] = {}
        #: Bounded journals the write integration inspects: every page/API write
        #: attempt (marked or not) and every page-session collection read.
        self.write_journal: list[dict] = []
        self.read_journal: list[tuple] = []
        #: Optional (session, method, path, query, body) -> verdict hook the host
        #: installs to admit one armed request inside the read-only window.
        self._write_gate = None
        self._assistant_bootstrap: Callable[[DemoActor, str], dict] | None = None
        self._assistant_revoke: Callable[[DemoActor], None] | None = None
        self._assistant_script: str | None = None
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

            def _create_contact(self, actor: DemoActor, body: bytes = b"",
                                gate=None, path: str = "") -> None:
                """Apply the application's own validation and uniqueness rule once."""
                with owner._lock:
                    owner.post_requests += 1
                owner.post_received.set()
                try:
                    payload = json.loads(body or b"{}")
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
                    identifier = name.casefold().replace(" ", "-")
                    if not re.fullmatch(r"[a-z0-9-]{1,120}", identifier):
                        # Names are Unicode values; record identities follow the
                        # same opaque ASCII contract as the application's reader.
                        identifier = "contact-" + hashlib.sha256(
                            name.casefold().encode("utf-8")).hexdigest()
                    if any(item["id"] == identifier for item in store):
                        self._reply(409, _json_bytes({"error": "already_exists"}),
                                    "application/json")
                        return
                    record = {"id": identifier, "name": name, "email": email}
                    store.append(record)
                    owner.records_created += 1
                if owner.write_fault == "drop_response":
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                if path.startswith("/api/browser/"):
                    with owner._lock:
                        owner.write_journal.append({"method": "POST", "path": path,
                            "created_id": record["id"], "marked": bool(
                                isinstance(gate, dict) and gate.get("marked"))})
                        del owner.write_journal[:-200]
                reply = _json_bytes({"contact": record})
                if isinstance(gate, dict):
                    gate["settle"](201, reply)
                self._reply(201, reply, "application/json")

            def _gate_write(self, resolved, method: str, path: str, query: str, body: bytes):
                """Apply the read-only window to one write request.

                While the assistant holds the window only a request carrying the
                application's own assistant marker may pass, and only through the
                host-installed gate: the armed permit admits exactly what the card
                approved. Anything else under `/api/` still meets the window's 409.
                Returns the verdict dict for an admitted request (its ``settle``
                reports the application's answer back to the permit).
                """
                marked = self.headers.get("X-WOA-Assistant") == "1"
                if resolved is None or resolved[1] != "page_session" \
                        or not path.startswith("/api/"):
                    return None
                with owner._lock:
                    now = time.time()
                    guarded = any(until > now for (session, _window), until
                                  in owner._page_windows.items()
                                  if session == resolved[0].session)
                if not guarded:
                    return None
                if not marked or owner._write_gate is None:
                    self._reply(409, _json_bytes({"error": "read_only_window"}),
                                "application/json")
                    return "refused"
                verdict = owner._write_gate(resolved[0].session, method, path, query, body)
                if not verdict["allowed"]:
                    self._reply(409, _json_bytes({"error": verdict["code"]}),
                                "application/json")
                    return "refused"
                verdict["marked"] = marked
                return verdict

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                if path in {"/api/page-window/begin", "/api/page-window/end"}:
                    resolved = self._actor()
                    if (resolved is None or resolved[1] != "page_session"
                            or self.headers.get("Origin") != owner.origin):
                        self._reply(403, _json_bytes({"error": "window_actor_refused"}), "application/json")
                        return
                    length = int(self.headers.get("Content-Length") or 0)
                    if not 0 < length <= 100:
                        self._reply(400, _json_bytes({"error": "invalid_window"}), "application/json")
                        return
                    try:
                        window = json.loads(self.rfile.read(length)).get("window") or ""
                    except (ValueError, UnicodeDecodeError, AttributeError):
                        window = ""
                    if not isinstance(window, str) or len(window) > 128:
                        window = ""
                    active = path.endswith("/begin")
                    with owner._lock:
                        now = time.time()
                        owner._page_windows = {key: until for key, until
                                               in owner._page_windows.items() if until > now}
                        expires = now + 120 if active else 0
                        owner._page_windows[(resolved[0].session, window)] = expires
                    self._reply(200, _json_bytes({"active": active, "expires_at": expires}),
                                "application/json")
                    return
                resolved = self._actor()
                body = b""
                if path.startswith("/api/"):
                    length = int(self.headers.get("Content-Length") or 0)
                    if 0 < length <= 64 * 1024:
                        body = self.rfile.read(length)
                gate = self._gate_write(resolved, "POST", path,
                                        urllib.parse.urlsplit(self.path).query, body)
                if gate == "refused":
                    return
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
                                or any(not (char.isalnum() or char in "-_.")
                                       for char in instance_id)):
                            raise ValueError()
                        bootstrap = owner._assistant_bootstrap(resolved[0], instance_id)
                    except (ValueError, OSError):
                        self._reply(400, _json_bytes({"error": "invalid_bootstrap"}),
                                    "application/json")
                        return
                    except Exception as exc:
                        if getattr(exc, "code", "") == "session_capacity":
                            self._reply(429, _json_bytes({
                                "error": "assistant_capacity",
                                "retryable": True,
                            }), "application/json")
                            return
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
                if path in {"/api/effects/database", "/api/effects/external-http"}:
                    if not owner.adversarial_form_fixture:
                        self._refuse_write(404, "not_found")
                        return
                    resolved = self._actor()
                    if (self.headers.get("Origin") != owner.origin or resolved is None
                            or resolved[1] != "page_session"):
                        self._refuse_write(403, "origin_refused")
                        return
                    effect = path.rsplit("/", 1)[-1]
                    with owner._lock:
                        owner.write_requests += 1
                        owner.side_effect_requests[effect] += 1
                        if effect == "database":
                            owner.side_effect_records.append("onchange-created-record")
                    self.send_response(204)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with owner._lock:
                    owner.write_requests += 1
                resolved = self._actor()
                deleted = re.fullmatch(r"/api/browser/contacts/([^/]+)/delete", path)
                if deleted:
                    # Human-owned, like the create form below: the person's own page
                    # session, same origin, and the application's own write authority.
                    if (self.headers.get("Origin") != owner.origin or resolved is None
                            or resolved[1] != "page_session"):
                        self._refuse_write(403, "origin_refused")
                        return
                    actor = resolved[0]
                    if "contacts:write" not in actor.permissions:
                        self._refuse_write(403, "forbidden")
                        return
                    identifier = urllib.parse.unquote(deleted.group(1))
                    with owner._lock:
                        store = owner.workspaces[actor.workspace]
                        kept = [item for item in store if item["id"] != identifier]
                        if len(kept) == len(store):
                            self._reply(404, _json_bytes({"error": "not_found"}),
                                        "application/json")
                            return
                        store[:] = kept
                        owner.records_deleted += 1
                        owner.write_journal.append({"method": "POST", "path": path,
                            "record_id": identifier,
                            "marked": bool(isinstance(gate, dict) and gate.get("marked"))})
                        del owner.write_journal[:-200]
                    reply = _json_bytes({"deleted": identifier})
                    if isinstance(gate, dict):
                        gate["settle"](200, reply)
                    self._reply(200, reply, "application/json")
                    return
                if path == "/api/browser/contacts":
                    # This is the human-owned form endpoint, separate from the delegated
                    # Agent API below. Same-origin JSON plus SameSite cookie prevents a
                    # cross-site form from submitting it; prefill_form never calls submit.
                    if (self.headers.get("Origin") != owner.origin or resolved is None
                            or resolved[1] != "page_session"):
                        self._refuse_write(403, "origin_refused")
                        return
                    actor = resolved[0]
                    if "contacts:write" not in actor.permissions:
                        self._refuse_write(403, "forbidden")
                        return
                    self._create_contact(actor, body=body, gate=gate, path=path)
                    return
                if path == "/api/contact-notes":
                    if resolved is None or resolved[1] != "delegated_api":
                        self._refuse_write(401, "delegated_token_required")
                        return
                    actor = resolved[0]
                    if "contacts:write" not in actor.permissions:
                        self._refuse_write(403, "forbidden")
                        return
                    try:
                        payload = json.loads(body or b"{}")
                    except (ValueError, OSError):
                        self._reply(400, _json_bytes({"error": "invalid_json"}), "application/json")
                        return
                    title = str(payload.get("title") or "").strip()
                    note_owner = str(payload.get("owner") or "").strip()
                    category = str(payload.get("category") or "").strip()
                    if not title or not note_owner or category not in {"general", "billing", "vip"}:
                        self._reply(400, _json_bytes({"error": "invalid_note"}), "application/json")
                        return
                    with owner._lock:
                        notes = owner.contact_notes.setdefault(actor.workspace, [])
                        nid = f"note-{len(notes) + 1}"
                        rec = {
                            "id": nid,
                            "title": title,
                            "owner": note_owner,
                            "category": category,
                        }
                        if payload.get("body"):
                            rec["body"] = str(payload["body"]).strip()
                        notes.append(rec)
                    self._reply(201, _json_bytes({"note": dict(rec)}), "application/json")
                    return
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
                self._create_contact(actor, body=body, gate=gate, path=path)

            def do_PUT(self):
                """The edit form's own endpoint: browser-session only, same window rules."""
                path = urllib.parse.urlsplit(self.path).path
                resolved = self._actor()
                body = b""
                if path.startswith("/api/"):
                    length = int(self.headers.get("Content-Length") or 0)
                    if 0 < length <= 64 * 1024:
                        body = self.rfile.read(length)
                gate = self._gate_write(resolved, "PUT", path,
                                        urllib.parse.urlsplit(self.path).query, body)
                if gate == "refused":
                    return
                delegated_contact = re.fullmatch(r"/api/contacts/([a-z0-9-]+)", path)
                delegated_sub = re.fullmatch(r"/api/contact-subscriptions/([a-z0-9-]+)", path)
                delegated_dossier = re.fullmatch(r"/api/contact-dossiers/([a-z0-9-]+)", path)
                delegated_group = re.fullmatch(r"/api/contact-groups/([a-z0-9-]+)", path)
                if delegated_contact or delegated_sub or delegated_dossier or delegated_group:
                    with owner._lock:
                        owner.write_requests += 1
                    if resolved is None or resolved[1] != "delegated_api":
                        self._refuse_write(401, "delegated_token_required")
                        return
                    actor = resolved[0]
                    if "contacts:write" not in actor.permissions:
                        self._refuse_write(403, "forbidden")
                        return
                    try:
                        payload = json.loads(body or b"{}")
                    except (ValueError, OSError):
                        self._reply(400, _json_bytes({"error": "invalid_json"}), "application/json")
                        return
                    identifier = (delegated_contact or delegated_sub or delegated_dossier or delegated_group).group(1)
                    if delegated_dossier:
                        with owner._lock:
                            dossiers = owner.contact_dossiers.setdefault(actor.workspace, [])
                            dossier = next((d for d in dossiers if d["id"] == identifier), None)
                            if dossier is None:
                                self._reply(404, _json_bytes({"error": "not_found"}), "application/json")
                                return
                            dossier["name"] = str(payload.get("name") or dossier["name"]).strip()
                            dossier["owner"] = str(payload.get("owner") or dossier["owner"]).strip()
                            dossier["note"] = str(payload.get("note") if "note" in payload else dossier["note"]).strip()
                            if isinstance(payload.get("items"), list):
                                new_items = []
                                for idx, it in enumerate(payload["items"], 1):
                                    new_items.append({
                                        "id": str(it.get("id") or f"item-{idx}"),
                                        "label": str(it.get("label") or "").strip(),
                                        "value": str(it.get("value") or "").strip(),
                                    })
                                dossier["items"] = new_items
                            updated = json.loads(json.dumps(dossier))
                        self._reply(200, _json_bytes({"dossier": updated}), "application/json")
                        return
                    if delegated_group:
                        groups = payload.get("groups")
                        if not isinstance(groups, list) or any(not isinstance(g, str) for g in groups):
                            self._reply(400, _json_bytes({"error": "invalid_groups"}), "application/json")
                            return
                        with owner._lock:
                            store = owner.workspaces[actor.workspace]
                            if not any(item["id"] == identifier for item in store):
                                self._reply(404, _json_bytes({"error": "not_found"}), "application/json")
                                return
                            owner.contact_groups.setdefault(actor.workspace, {})[identifier] = [str(g) for g in groups]
                        self._reply(200, _json_bytes({"id": identifier, "groups": list(groups)}), "application/json")
                        return
                    if delegated_contact:
                        if not isinstance(payload, dict) or set(payload) != {"id", "name", "email"} or payload["id"] != identifier:
                            self._reply(400, _json_bytes({"error": "update_shape"}), "application/json")
                            return
                        name = str(payload["name"]).strip()
                        email = str(payload["email"]).strip().casefold()
                        if not name or not email:
                            self._reply(400, _json_bytes({"error": "name_and_email_required"}), "application/json")
                            return
                        with owner._lock:
                            store = owner.workspaces[actor.workspace]
                            record = next((item for item in store if item["id"] == identifier), None)
                            if record is None:
                                self._reply(404, _json_bytes({"error": "not_found"}), "application/json")
                                return
                            record["name"], record["email"] = name, email
                            updated = dict(record)
                        self._reply(200, _json_bytes({"contact": updated}), "application/json")
                        return
                    if not isinstance(payload, dict) or set(payload) != {"id", "status"} or payload["id"] != identifier or payload["status"] not in {"enabled", "disabled"}:
                        self._reply(400, _json_bytes({"error": "status_shape"}), "application/json")
                        return
                    with owner._lock:
                        store = owner.workspaces[actor.workspace]
                        record = next((item for item in store if item["id"] == identifier), None)
                        if record is None:
                            self._reply(404, _json_bytes({"error": "not_found"}), "application/json")
                            return
                        owner.subscriptions.setdefault(actor.workspace, {})[identifier] = payload["status"]
                        sub = {"id": identifier, "name": record["name"], "status": payload["status"]}
                    self._reply(200, _json_bytes({"subscription": sub}), "application/json")
                    return
                if path != "/api/browser/contacts":
                    self._refuse_write(404, "not_found")
                    return
                with owner._lock:
                    owner.write_requests += 1
                if (self.headers.get("Origin") != owner.origin or resolved is None
                        or resolved[1] != "page_session"):
                    self._refuse_write(403, "origin_refused")
                    return
                actor = resolved[0]
                if "contacts:write" not in actor.permissions:
                    self._refuse_write(403, "forbidden")
                    return
                try:
                    payload = json.loads(body or b"{}")
                except (ValueError, OSError):
                    self._reply(400, _json_bytes({"error": "invalid_json"}),
                                "application/json")
                    return
                if not isinstance(payload, dict) or set(payload) != {"contactId", "name", "email"}:
                    self._reply(400, _json_bytes({"error": "update_shape"}),
                                "application/json")
                    return
                name = str(payload["name"]).strip()
                email = str(payload["email"]).strip().casefold()
                identifier = str(payload["contactId"])
                if not name or not email or not identifier:
                    self._reply(400, _json_bytes({"error": "name_and_email_required"}),
                                "application/json")
                    return
                with owner._lock:
                    store = owner.workspaces[actor.workspace]
                    record = next((item for item in store if item["id"] == identifier), None)
                    if record is None:
                        self._reply(404, _json_bytes({"error": "not_found"}),
                                    "application/json")
                        return
                    record["name"], record["email"] = name, email
                    updated = dict(record)
                    owner.write_journal.append({"method": "PUT", "path": path,
                        "record_id": identifier,
                        "marked": bool(gate and gate.get("marked"))})
                    del owner.write_journal[:-200]
                reply = _json_bytes({"contact": updated})
                if isinstance(gate, dict):
                    gate["settle"](200, reply)
                self._reply(200, reply, "application/json")

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
                if path.startswith("/demo/login/"):
                    actor_id = urllib.parse.unquote(path.removeprefix("/demo/login/"))
                    actor = ACTORS_BY_ID.get(actor_id)
                    if actor is None:
                        self._reply(404, b"not found\n", "text/plain; charset=utf-8")
                        return
                    # A real application calls the revocation hook from its login/logout
                    # lifecycle. This synthetic route does the same when the browser changes
                    # actor: old delegated authority must stop before the new cookie exists.
                    previous = resolved[0] if resolved is not None else None
                    if (previous is not None and previous.actor != actor.actor
                            and owner._assistant_revoke is not None):
                        owner._assistant_revoke(previous)
                    self._redirect(
                        "/contacts",
                        cookie=(f"{DEMO_SESSION_COOKIE}={actor.session}; Path=/; "
                                "HttpOnly; SameSite=Strict"))
                    return
                if path == "/healthz":
                    self._reply(200, _json_bytes({"ready": True}), "application/json")
                elif path == "/api/pages":
                    if resolved is None:
                        self._reply(401, _json_bytes({"error": "unauthenticated"}), "application/json")
                        return
                    actor = resolved[0]
                    if "contacts:read" not in actor.permissions:
                        self._reply(403, _json_bytes({"error": "forbidden"}), "application/json")
                        return
                    hidden = owner.hidden_pages.get(actor.actor, set())
                    self._reply(200, _json_bytes({"actor": actor.actor, "workspace": actor.workspace,
                        "pages": [dict(item) for item in PAGE_MENUS if item["key"] not in hidden],
                        "complete": True}), "application/json")
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
                        if resolved[1] == "page_session":
                            owner.read_journal.append(
                                (path, urllib.parse.urlsplit(self.path).query))
                            del owner.read_journal[:-200]
                        records = [dict(item)
                                   for item in owner.workspaces[resolved[0].workspace]]
                    self._reply(200, _json_bytes({"contacts": records}),
                                "application/json; charset=utf-8")
                elif re.fullmatch(r"/api/contacts/([a-z0-9-]+)", path):
                    if resolved is None:
                        self._reply(401, _json_bytes({"error": "unauthenticated"}), "application/json")
                        return
                    if "contacts:read" not in resolved[0].permissions:
                        self._reply(403, _json_bytes({"error": "forbidden"}), "application/json")
                        return
                    identifier = path.rsplit("/", 1)[-1]
                    with owner._lock:
                        found = next((dict(item) for item in owner.workspaces[resolved[0].workspace]
                                      if item["id"] == identifier), None)
                    if found is None:
                        self._reply(404, _json_bytes({"error": "not_found"}), "application/json")
                        return
                    self._reply(200, _json_bytes({"contact": found}), "application/json; charset=utf-8")
                elif re.fullmatch(r"/api/contact-subscriptions/([a-z0-9-]+)", path):
                    if resolved is None:
                        self._reply(401, _json_bytes({"error": "unauthenticated"}), "application/json")
                        return
                    if "contacts:read" not in resolved[0].permissions:
                        self._reply(403, _json_bytes({"error": "forbidden"}), "application/json")
                        return
                    identifier = path.rsplit("/", 1)[-1]
                    ws = resolved[0].workspace
                    with owner._lock:
                        found = next((dict(item) for item in owner.workspaces[ws] if item["id"] == identifier), None)
                        status = owner.subscriptions.get(ws, {}).get(identifier, "enabled")
                    if found is None:
                        self._reply(404, _json_bytes({"error": "not_found"}), "application/json")
                        return
                    self._reply(200, _json_bytes({"subscription": {"id": identifier, "name": found["name"], "status": status}}),
                                "application/json; charset=utf-8")
                elif path == "/api/contact-dossiers":
                    if resolved is None or "contacts:read" not in resolved[0].permissions:
                        self._reply(403 if resolved else 401, _json_bytes({"error": "forbidden"}), "application/json")
                        return
                    with owner._lock:
                        items = json.loads(json.dumps(owner.contact_dossiers.get(resolved[0].workspace, [])))
                    self._reply(200, _json_bytes({"dossiers": items}), "application/json; charset=utf-8")
                elif re.fullmatch(r"/api/contact-dossiers/([a-z0-9-]+)", path):
                    if resolved is None or "contacts:read" not in resolved[0].permissions:
                        self._reply(403 if resolved else 401, _json_bytes({"error": "forbidden"}), "application/json")
                        return
                    identifier = path.rsplit("/", 1)[-1]
                    with owner._lock:
                        found = next((json.loads(json.dumps(d)) for d in owner.contact_dossiers.get(resolved[0].workspace, [])
                                      if d["id"] == identifier), None)
                    if found is None:
                        self._reply(404, _json_bytes({"error": "not_found"}), "application/json")
                        return
                    self._reply(200, _json_bytes({"dossier": found}), "application/json; charset=utf-8")
                elif re.fullmatch(r"/api/contact-notes/([a-z0-9-]+)", path):
                    if resolved is None or "contacts:read" not in resolved[0].permissions:
                        self._reply(403 if resolved else 401, _json_bytes({"error": "forbidden"}), "application/json")
                        return
                    identifier = path.rsplit("/", 1)[-1]
                    with owner._lock:
                        found = next((dict(n) for n in owner.contact_notes.get(resolved[0].workspace, [])
                                      if n["id"] == identifier), None)
                    if found is None:
                        self._reply(404, _json_bytes({"error": "not_found"}), "application/json")
                        return
                    self._reply(200, _json_bytes({"note": found}), "application/json; charset=utf-8")
                elif re.fullmatch(r"/api/contact-groups/([a-z0-9-]+)", path):
                    if resolved is None or "contacts:read" not in resolved[0].permissions:
                        self._reply(403 if resolved else 401, _json_bytes({"error": "forbidden"}), "application/json")
                        return
                    identifier = path.rsplit("/", 1)[-1]
                    with owner._lock:
                        found = next((dict(item) for item in owner.workspaces[resolved[0].workspace] if item["id"] == identifier), None)
                        groups = list(owner.contact_groups.get(resolved[0].workspace, {}).get(identifier, []))
                    if found is None:
                        self._reply(404, _json_bytes({"error": "not_found"}), "application/json")
                        return
                    self._reply(200, _json_bytes({"id": identifier, "groups": groups}), "application/json; charset=utf-8")
                elif path == "/woa/page-controller.js":
                    # The application serves its own PageController from its own origin,
                    # which is what the SPI requires of any host. Read from the example's
                    # directory rather than from an installed package: this example is run
                    # from a checkout and is deliberately not shipped with the framework.
                    source = (Path(__file__).parent / "assets"
                              / "page_controller.js").read_bytes()
                    self._reply(200, source, "text/javascript; charset=utf-8")
                elif path == "/contacts" or path.startswith("/contacts/"):
                    if (path == "/contacts/adversarial"
                            and not owner.adversarial_form_fixture):
                        self._reply(404, b"not found\n", "text/plain; charset=utf-8")
                        return
                    if resolved is None or resolved[1] != "page_session":
                        self._reply(401, b"<!doctype html><title>Sign in</title>"
                                         b"<h1>Sign in required</h1>\n",
                                    "text/html; charset=utf-8")
                        return
                    actor = resolved[0]
                    menu = next((item for item in PAGE_MENUS if item["path"] == path), None)
                    if menu is not None and menu["key"] in owner.hidden_pages.get(actor.actor, set()):
                        self._reply(403, b"<!doctype html><title>Forbidden</title><h1>Not permitted</h1>\n",
                                    "text/html; charset=utf-8")
                        return
                    if "contacts:read" not in actor.permissions:
                        self._reply(403, b"<!doctype html><title>Forbidden</title>"
                                         b"<h1>Not permitted</h1>\n",
                                    "text/html; charset=utf-8")
                        return
                    with owner._lock:
                        records = [dict(item) for item in owner.workspaces[actor.workspace]]
                    self._reply(200, _html(
                        records, actor.workspace,
                        can_write="contacts:write" in actor.permissions,
                        assistant_script=owner._assistant_script,
                        visible_pages=tuple(item["key"] for item in PAGE_MENUS
                            if item["key"] not in owner.hidden_pages.get(actor.actor, set())),
                        adversarial_form_fixture=owner.adversarial_form_fixture),
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
                target=self._server.serve_forever, kwargs={"poll_interval": 0.01},
                name="minimal-contacts", daemon=True)
            self._thread.start()
        return self

    def install_assistant(self, *, bootstrap: Callable[[DemoActor, str], dict],
                          revoke: Callable[[DemoActor], None], script_url: str) -> None:
        """Install server-side hooks; the browser can never supply an actor or token.

        `script_url` is where the agent serves `assistant.js`; every signed-in page includes it.
        """
        if not callable(bootstrap) or not callable(revoke):
            raise TypeError("assistant bootstrap and revoke hooks must be callable")
        parsed = urllib.parse.urlsplit(script_url if isinstance(script_url, str) else "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("script_url must be an absolute http(s) URL")
        with self._lock:
            if self._assistant_bootstrap is not None:
                raise RuntimeError("assistant hooks are already installed")
            self._assistant_bootstrap = bootstrap
            self._assistant_revoke = revoke
            self._assistant_script = script_url

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
