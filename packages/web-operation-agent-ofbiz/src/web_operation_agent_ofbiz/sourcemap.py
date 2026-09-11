"""Narrow-domain compiler: OFBiz's declarative descriptors -> a cited SourceMap.

This reads five kinds of file that the framework itself uses to decide what a page shows
and what a submit does:

    controller.xml   a request name -> the service or java event that runs it, and the view
                     it responds with. OFBiz also declares request-maps inside compound-widget
                     files (`*Cpd.xml`), and those are read too: 154 of them live there, one of
                     which is the invoice-side payment-application entry that a source-armed run
                     kept submitting to while the map could tell it only the form's target and
                     nothing about what that target invokes.
    *Forms.xml       a form -> the request it targets and the fields it submits
    *Screens.xml     a screen -> the forms it renders
    *Menus.xml       a labelled navigation item -> the request it links to
    servicedef/*.xml a service -> its IN parameters and the entity it is declared against
    *UiLabels.xml    a label key -> the English string a user actually sees

Chaining those gives the thing a planner needs and a page cannot show until you are already
on it: "the visible words *Payments* lead to `findPayments`; the form on the payment screen
posts to `createPaymentApplication`; that request runs the service of the same name; the
service requires paymentId, invoiceId and amountApplied."

It is a reader for one framework's own configuration, not a general static analyser: there
is no call graph, no dataflow, no cross-file symbol resolution beyond the locations OFBiz
itself writes down. Anything it cannot interpret is recorded as `unsupported`, never
guessed -- an invented relation is worse than a missing one, because the planner would cite
it.
"""
from __future__ import annotations

import re
from collections import defaultdict

from web_operation_agent.schemas import SCHEMA_VERSIONS, EvidenceRef, SourceMap, SourceRelation
from web_operation_agent.sourcetree import AccessDenied, SourceTree
from web_operation_agent.xmlscan import XMLScanError, parse

LABEL_RE = re.compile(r"\$\{uiLabelMap\.([A-Za-z0-9_]+)\}")
CONTROLLER_RE = re.compile(r"^(applications|framework|plugins)/([^/]+)/webapp/([^/]+)/WEB-INF/[^/]*controller[^/]*\.xml$")
#: Compound-widget files carry a `<site-conf>` section with request-maps of their own. They sit
#: under widget/, not WEB-INF/, so the controller pattern never sees them.
COMPOUND_RE = re.compile(r"^(applications|framework|plugins)/([^/]+)/widget/.*Cpd\.xml$")
WIDGET_RE = re.compile(r"^(applications|framework|plugins)/([^/]+)/widget/.*\.xml$")
SERVICEDEF_RE = re.compile(r"^(applications|framework|plugins)/([^/]+)/servicedef/[^/]*\.xml$")
LABELS_RE = re.compile(r"^(applications|framework|plugins)/([^/]+)/config/[^/]*UiLabels\.xml$")


class OFBizCompiler:
    def __init__(self, tree: SourceTree, attestation_id: str):
        self.tree = tree
        self.attestation_id = attestation_id
        self.labels: dict[str, str] = {}
        self.relations: list[SourceRelation] = []
        self.unsupported: list[dict] = []
        #: component -> the webapps its controllers serve, so a widget file under
        #: applications/accounting/widget can be attributed to the accounting webapp.
        self.component_webapps: dict[str, list[str]] = defaultdict(list)
        self._file_set: set[str] = set()

    # -- helpers -----------------------------------------------------------------------
    def _cite(self, rel: str, node, *, excerpt: int = 200) -> EvidenceRef:
        end = node.end_line or node.start_line
        return self.tree.cite(rel, node.start_line, max(node.start_line, end), excerpt_chars=excerpt)

    def _note_unsupported(self, rel: str, node, why: str) -> None:
        self.unsupported.append({"path": rel, "line": node.start_line, "element": node.tag,
                                 "why": why})

    def _add(self, kind: str, subject: str, obj: str, attrs: dict, ref: EvidenceRef) -> None:
        self.relations.append(SourceRelation(kind=kind, subject=subject, object=obj,
                                             attrs=attrs, evidence=(ref,)))

    def resolve_label(self, raw: str) -> tuple[str, bool]:
        """'${uiLabelMap.X} Y' -> ('Payments Y', True). Unresolvable keys stay verbatim."""
        if not raw:
            return "", True
        resolved = True

        def sub(m):
            nonlocal resolved
            v = self.labels.get(m.group(1))
            if v is None:
                resolved = False
                return m.group(0)
            return v
        return LABEL_RE.sub(sub, raw).strip(), resolved

    def _parse(self, rel: str):
        try:
            return parse(self.tree.read_text(rel))
        except (XMLScanError, AccessDenied, UnicodeError) as e:
            self.unsupported.append({"path": rel, "line": 0, "element": "#file",
                                     "why": f"unreadable: {type(e).__name__}: {e}"})
            return None

    # -- passes ------------------------------------------------------------------------
    def _pass_labels(self, files: list[str]) -> None:
        for rel in files:
            if not LABELS_RE.match(rel):
                continue
            root = self._parse(rel)
            if root is None:
                continue
            for prop in root.find_all("property"):
                key = prop.attrs.get("key")
                if not key:
                    continue
                for val in prop.kids("value"):
                    if val.attrs.get("lang") == "en":
                        # The text itself is not captured by the scanner; re-read the span.
                        try:
                            body = self.tree.read_text(rel).splitlines()[val.start_line - 1]
                        except Exception:
                            break
                        m = re.search(r">([^<]*)<", body)
                        if m:
                            self.labels.setdefault(key, m.group(1).strip())
                        break

    def _pass_controllers(self, files: list[str]) -> None:
        for rel in files:
            m = CONTROLLER_RE.match(rel)
            if not m:
                continue
            _, component, webapp = m.groups()
            if webapp not in self.component_webapps[component]:
                self.component_webapps[component].append(webapp)
            self._read_request_maps(rel, component, webapp)

    def _pass_compound_widgets(self, files: list[str]) -> None:
        """Request-maps declared inside compound-widget files.

        A component's compound widgets are served by that component's own webapps, which the
        controller pass has already discovered. Where a component serves several webapps the
        same request is declared once and reachable from each, so it is recorded for each --
        the alternative is to drop it, and a request the planner cannot see is a request it
        cannot reason about.
        """
        for rel in files:
            m = COMPOUND_RE.match(rel)
            if not m:
                continue
            component = m.group(2)
            for webapp in self._webapps_for(component):
                self._read_request_maps(rel, component, webapp)

    def _read_request_maps(self, rel: str, component: str, webapp: str) -> None:
        root = self._parse(rel)
        if root is None:
            return
        for rm in root.find_all("request-map"):
            uri = rm.attrs.get("uri")
            if not uri:
                self._note_unsupported(rel, rm, "request-map without a uri")
                continue
            subject = f"{webapp}/{uri}"
            sec = rm.first("security")
            sec_attrs = sec.attrs if sec is not None else {}
            ref = self._cite(rel, rm)
            ev = rm.first("event")
            if ev is None:
                self._add("request_handler", subject, "", {
                    "webapp": webapp, "component": component, "event_type": "view-only",
                    "auth": sec_attrs.get("auth", ""), "https": sec_attrs.get("https", "")}, ref)
            else:
                etype = ev.attrs.get("type", "")
                invoke = ev.attrs.get("invoke", "")
                if etype == "service":
                    obj = invoke
                elif etype in ("java", "groovy", "simple", "service-multi"):
                    obj = f"{ev.attrs.get('path', '')}#{invoke}".strip("#")
                else:
                    self._note_unsupported(rel, ev, f"unhandled event type {etype!r}")
                    obj = ""
                self._add("request_handler", subject, obj, {
                    "webapp": webapp, "component": component, "event_type": etype,
                    "auth": sec_attrs.get("auth", ""), "https": sec_attrs.get("https", "")}, ref)
            for resp in rm.kids("response"):
                self._add("request_response", subject, resp.attrs.get("value", ""), {
                    "webapp": webapp, "response_name": resp.attrs.get("name", ""),
                    "response_type": resp.attrs.get("type", "")}, ref)
        for view in root.find_all("view-map"):
            name, page = view.attrs.get("name"), view.attrs.get("page")
            if not name or not page:
                continue
            location, mark, screen = page.partition("#")
            resolved = self._component_path(location) or location
            target = resolved + (mark + screen if mark else "")
            self._add("view_screen", f"{webapp}/{name}", target, {
                "webapp": webapp, "component": component,
                "view_type": view.attrs.get("type", "")},
                self._cite(rel, view, excerpt=160))

    def _webapps_for(self, component: str) -> list[str]:
        return self.component_webapps.get(component) or [component]

    def _pass_widgets(self, files: list[str]) -> None:
        for rel in files:
            m = WIDGET_RE.match(rel)
            if not m:
                continue
            _, component = m.group(1), m.group(2)
            root = self._parse(rel)
            if root is None:
                continue
            webapps = self._webapps_for(component)
            for form in root.find_all("form"):
                name = form.attrs.get("name")
                target = form.attrs.get("target")
                if not name:
                    continue
                subject = f"{rel}#{name}"
                ref = self._cite(rel, form, excerpt=160)
                targets = []
                if target:
                    targets.append((target, "primary", "", ref))
                for alt in form.kids("alt-target"):
                    if alt.attrs.get("target"):
                        targets.append((alt.attrs["target"], "alternate",
                                        alt.attrs.get("use-when", ""),
                                        self._cite(rel, alt, excerpt=160)))
                for destination, target_kind, use_when, target_ref in targets:
                    self._add("form_target", subject, destination, {
                        "component": component, "webapps": webapps,
                        "form_type": form.attrs.get("type", ""),
                        "form_name": name, "target_kind": target_kind,
                        "use_when": use_when}, target_ref)
                if targets:
                    for fld in form.find_all("field"):
                        fname = fld.attrs.get("name")
                        if not fname:
                            continue
                        kind = fld.children[0].tag if fld.children else ""
                        self._add("form_field", subject, fname, {
                            "widget": kind, "required": fld.attrs.get("required-field", ""),
                            "form_name": name},
                            self._cite(rel, fld, excerpt=0))
            for screen in root.find_all("screen"):
                sname = screen.attrs.get("name")
                if not sname:
                    continue
                for inc in screen.find_all("include-form"):
                    fname = inc.attrs.get("name")
                    loc = inc.attrs.get("location", "")
                    if not fname:
                        continue
                    self._add("screen_includes_form", f"{rel}#{sname}",
                              f"{self._component_path(loc) or rel}#{fname}",
                              {"component": component, "screen_name": sname, "form_name": fname},
                              self._cite(rel, inc, excerpt=0))
            for menu in root.find_all("menu"):
                mname = menu.attrs.get("name", "")
                for item in menu.find_all("menu-item"):
                    link = item.first("link")
                    if link is None:
                        continue
                    target = link.attrs.get("target", "")
                    if not target:
                        continue
                    title, ok = self.resolve_label(item.attrs.get("title", "")
                                                   or item.attrs.get("name", ""))
                    gated = bool(item.find_all("if-has-permission") or item.find_all("condition"))
                    # A menu route can depend on values carried by the page context. Keep
                    # only the declarative binding as written on the link's direct child:
                    # resolving a field here would mix runtime data into the SourceMap.
                    parameters = [{"name": parameter.attrs.get("param-name", ""),
                                   "from_field": parameter.attrs.get("from-field", ""),
                                   "value": parameter.attrs.get("value", "")}
                                  for parameter in link.kids("parameter")]
                    self._add("menu_link", f"{component}:{mname}:{item.attrs.get('name', '')}",
                              target, {"title": title, "title_resolved": ok,
                                       "component": component, "webapps": webapps,
                                       "url_mode": link.attrs.get("url-mode", ""),
                                       "parameters": parameters,
                                       "conditional": gated},
                              self._cite(rel, item, excerpt=160))

    def _pass_services(self, files: list[str]) -> None:
        for rel in files:
            if not SERVICEDEF_RE.match(rel):
                continue
            root = self._parse(rel)
            if root is None:
                continue
            for svc in root.find_all("service"):
                name = svc.attrs.get("name")
                if not name:
                    continue
                ref = self._cite(rel, svc, excerpt=200)
                self._add("service_entity", name, svc.attrs.get("default-entity-name", ""), {
                    "engine": svc.attrs.get("engine", ""), "invoke": svc.attrs.get("invoke", ""),
                    "auth": svc.attrs.get("auth", "")}, ref)
                for auto in svc.kids("auto-attributes"):
                    self._add("service_parameter", name, "*auto*", {
                        "mode": auto.attrs.get("mode", ""),
                        "include": auto.attrs.get("include", ""),
                        "optional": auto.attrs.get("optional", ""),
                        "entity": auto.attrs.get("entity-name", svc.attrs.get("default-entity-name", ""))},
                        self._cite(rel, auto, excerpt=0))
                for attr in svc.kids("attribute"):
                    aname = attr.attrs.get("name")
                    if not aname:
                        continue
                    self._add("service_parameter", name, aname, {
                        "mode": attr.attrs.get("mode", ""), "type": attr.attrs.get("type", ""),
                        "optional": attr.attrs.get("optional", "false")},
                        self._cite(rel, attr, excerpt=0))

    def _component_path(self, location: str) -> str:
        """'component://accounting/widget/PaymentForms.xml' -> its real path in the tree.

        OFBiz resolves `component://` through a runtime component registry, which is not a
        descriptor this compiler can read. Rather than guess a layout, the three roots a
        release tree actually uses are tried and the answer is only accepted if the file is
        really there; otherwise '' is returned and the caller keeps the local file as the
        subject instead of inventing a path.
        """
        if not location.startswith("component://"):
            return ""
        rest = location[len("component://"):]
        if "/" not in rest:
            return ""
        component, tail = rest.split("/", 1)
        for root in ("applications", "framework", "plugins", "themes"):
            candidate = f"{root}/{component}/{tail}"
            if candidate in self._file_set:
                return candidate
        return ""

    # -- entry point -------------------------------------------------------------------
    def compile(self) -> SourceMap:
        files = self.tree.files()
        self._file_set = set(files)
        self._pass_labels(files)
        self._pass_controllers(files)
        self._pass_compound_widgets(files)
        self._pass_widgets(files)
        self._pass_services(files)
        return SourceMap(schema_version=SCHEMA_VERSIONS["SourceMap"],
                         attestation_id=self.attestation_id,
                         repository_id=self.tree.repository_id, release=self.tree.release,
                         relations=self.relations, unsupported=self.unsupported)
