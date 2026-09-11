"""Narrow-domain compiler: Odoo's own module descriptors -> a cited SourceMap.

Odoo keeps in XML what OFBiz keeps in controller, screen, form and menu descriptors, and
keeps in Python what OFBiz keeps in service definitions:

    <menuitem>                        a labelled navigation item -> the action it opens
    <record model="ir.actions.act_window">
                                      an action -> the model it opens, the views it offers,
                                      and (since 17.0) the URL segment it lives at
    <record model="ir.ui.view">       a view -> the model it renders, its fields with their
                                      `invisible` / `readonly` / `required` / `domain`
                                      conditions, and its buttons with the condition under
                                      which each one is reachable at all
    models/*.py                       a model -> its `fields.*` declarations (type, selection,
                                      compute, store) and its method signatures with their
                                      `@api.depends`

Chaining those answers what a page cannot show until the answer no longer matters: on a
confirmed sales order the two buttons both labelled "Create Invoice" are distinguished only
by `invisible="invoice_status != 'to invoice'"` versus
`invisible="invoice_status != 'no' or state != 'sale'"`, and the second one silently
defaults to a percentage down payment. The DOM renders one button with one label.

This is a reader for one framework's own configuration, not a general static analyser.
Two limits are deliberate and are recorded rather than guessed:

  * **Inheritance is linked, not merged.** An Odoo view is a primary view plus a chain of
    `inherit_id` patches applied by XPath. Every relation carries `root_view`, so the union
    of a model's declared fields is computable, but a patch that *removes* or *replaces* a
    node is not applied. Treat `root_view` as "declared somewhere in this view family",
    not as "present in the rendered arch".
  * **Cross-module record patches are not merged into definitions.** A module may write
    `<record id="other_module.some_action">` to amend a record it does not own. Such a
    record declares no `res_model` and is reported as `unsupported`, never as a definition.
"""
from __future__ import annotations

import ast
import json
import re
from importlib import resources
from pathlib import Path

from web_operation_agent.schemas import (SCHEMA_VERSIONS, EvidenceRef, SourceMap,
                                         SourceRelation)
from web_operation_agent.sourcetree import AccessDenied, SourceTree
from web_operation_agent.xmlscan import XMLScanError, parse

from .route import ODOO_SOURCE_SCOPE


#: `addons/<module>/...` for ordinary modules, `odoo/addons/base/...` for base.
MODULE_RE = re.compile(r"^(?:addons/([^/]+)|odoo/addons/(base))/")
#: `%(sale.action_view_sale_advance_payment_inv)d` is how a view names an action id.
ACTION_REF_RE = re.compile(r"%\(([A-Za-z0-9_.]+)\)[ds]")
_XML_ID = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
#: Attributes that decide whether a user can see or use a node. These are the reason the
#: compiler exists: without them a relation says a control exists but not when it appears.
CONDITION_ATTRS = ("invisible", "readonly", "required", "column_invisible", "domain",
                   "context", "groups", "states", "confirm")
#: The elements Odoo renders as the structure of one view.
ARCH_ROOTS = frozenset({"form", "list", "tree", "kanban", "search", "calendar", "graph",
                        "pivot", "activity"})
#: `fields.Char(...)` keywords worth carrying; the rest are rendering detail.
FIELD_KEYWORDS = frozenset({"string", "required", "readonly", "compute", "related",
                            "selection", "comodel_name", "domain", "default", "store",
                            "tracking", "help", "index", "copy"})
_MAX_ATTR_CHARS = 400


def _load_policy() -> dict:
    raw = resources.files("web_operation_agent_odoo").joinpath("policies", "odoo.json")
    return json.loads(raw.read_text(encoding="utf-8"))


#: The packaged allow/deny policy. `release` is empty on purpose: only the deployment knows
#: which Odoo build its source root is, and a citation bound to the wrong release is worse
#: than no citation. Use `odoo_source_tree` to bind one.
ODOO_SOURCE_POLICY = _load_policy()


def odoo_source_tree(source_root: str | Path, *, release: str) -> SourceTree:
    """Bind the packaged policy to one deployment's source root and exact release."""
    if not isinstance(release, str) or not release.strip() or len(release) > 200:
        raise ValueError("odoo source release must be a bounded non-empty string")
    return SourceTree({**ODOO_SOURCE_POLICY, "release": release.strip()}, source_root)


def module_of(rel: str) -> str:
    match = MODULE_RE.match(rel)
    return (match.group(1) or match.group(2)) if match else ""


def qualify(module: str, xml_id: str) -> str:
    """`action_orders` declared in module `sale` is globally `sale.action_orders`."""
    if not xml_id or not _XML_ID.fullmatch(xml_id):
        return ""
    return xml_id if "." in xml_id else f"{module}.{xml_id}"


def _conditions(attrs: dict) -> dict:
    return {name: str(attrs[name])[:_MAX_ATTR_CHARS]
            for name in CONDITION_ATTRS if attrs.get(name)}


class OdooCompiler:
    """Compile one Odoo source tree into a cited ``SourceMap``.

    Satisfies the core ``SourceCompiler`` port: offline, version-bound, no live session.
    """

    def __init__(self, tree: SourceTree, attestation_id: str, *,
                 read_models: bool = True):
        if not isinstance(tree, SourceTree):
            raise TypeError("OdooCompiler requires a SourceTree")
        if not isinstance(attestation_id, str) or not attestation_id.strip():
            raise ValueError("attestation_id must be a non-empty string")
        self.tree = tree
        self.attestation_id = attestation_id.strip()
        self.read_models = bool(read_models)
        self.relations: list[SourceRelation] = []
        self.unsupported: list[dict] = []
        #: action xml_id -> the URL route it is reachable at, and its display name. Menus
        #: and buttons reference actions by xml_id; the route is what a user's URL shows.
        self.action_routes: dict[str, str] = {}
        self.action_names: dict[str, str] = {}
        #: view xml_id -> the view it patches, used to resolve every relation's root view.
        self.view_parent: dict[str, str] = {}
        self._pending_menus: list[tuple] = []
        self._pending_view_relations: list[tuple[SourceRelation, str]] = []

    # -- citation helpers ---------------------------------------------------------------
    def _cite(self, rel: str, node, *, excerpt: int = 200) -> EvidenceRef:
        end = node.end_line or node.start_line
        return self.tree.cite(rel, node.start_line, max(node.start_line, end),
                              excerpt_chars=excerpt)

    def _add(self, kind: str, subject: str, obj: str, attrs: dict,
             ref: EvidenceRef) -> SourceRelation:
        relation = SourceRelation(kind=kind, subject=subject, object=obj, attrs=attrs,
                                  evidence=(ref,))
        self.relations.append(relation)
        return relation

    def _skip(self, rel: str, line: int, tag: str, why: str) -> None:
        self.unsupported.append({"path": rel, "line": line, "element": tag, "why": why})

    # -- entry point --------------------------------------------------------------------
    def compile(self) -> SourceMap:
        files = self.tree.files()
        for rel in (name for name in files if name.endswith(".xml")):
            try:
                root = parse(self.tree.read_text(rel))
            except (XMLScanError, AccessDenied) as exc:
                self._skip(rel, 1, "#document", f"unparsed: {exc}")
                continue
            module = module_of(rel)
            self._records(rel, module, root)
            self._pending_menus.append((rel, module, root))
        # A menu's label may be the action's, and an action may be declared in any file, so
        # menus can only be resolved once every action in the tree has been read.
        for rel, module, root in self._pending_menus:
            self._menus(rel, module, root)
        self._resolve_root_views()
        if self.read_models:
            for rel in (name for name in files if name.endswith(".py")):
                self._model_file(rel, module_of(rel))
        return SourceMap(
            schema_version=SCHEMA_VERSIONS["SourceMap"],
            attestation_id=self.attestation_id,
            repository_id=self.tree.repository_id,
            release=self.tree.release,
            relations=self.relations,
            unsupported=self.unsupported,
        )

    # -- routes -------------------------------------------------------------------------
    @staticmethod
    def route_for(action_xml_id: str, url_path: str) -> str:
        """The subpath Odoo's web client uses for one action.

        `web/static/src/core/browser/router.js` writes an action with a `path` as that path
        and any other action as `action-<xml id or numeric id>`.
        """
        if url_path:
            return url_path
        return f"action-{action_xml_id}" if action_xml_id else ""

    @staticmethod
    def identity(route: str) -> str:
        """The `(scope, route)` identity the core's source consumers key on, as one string."""
        return f"{ODOO_SOURCE_SCOPE}/{route}" if route else ""

    # -- XML: records -------------------------------------------------------------------
    def _records(self, rel: str, module: str, root) -> None:
        for node in root.find_all("record"):
            model = node.attrs.get("model", "")
            xml_id = qualify(module, node.attrs.get("id", ""))
            fields = {child.attrs.get("name", ""): child for child in node.kids("field")}
            if model == "ir.actions.act_window":
                self._action(rel, module, node, xml_id, fields)
            elif model == "ir.actions.act_window.view":
                self._act_window_view(rel, module, node, fields)
            elif model == "ir.ui.view":
                self._view(rel, module, node, xml_id, fields)

    def _ref(self, module: str, fields: dict, name: str) -> str:
        node = fields.get(name)
        return qualify(module, node.attrs.get("ref", "")) if node is not None else ""

    def _text(self, rel: str, node) -> str:
        """Odoo puts scalar values in element text, which xmlscan does not retain.

        Reading them back out of the cited line range keeps the compiler honest: the value
        it reports is inside the bytes its citation proves.
        """
        if node is None:
            return ""
        try:
            lines = self.tree.read_text(rel).splitlines()
        except AccessDenied:
            return ""
        span = "\n".join(lines[node.start_line - 1:(node.end_line or node.start_line)])
        match = re.search(r">([^<]*)</field>", span, re.S)
        return (match.group(1).strip() if match else "")[:_MAX_ATTR_CHARS]

    def _action(self, rel: str, module: str, node, xml_id: str, fields: dict) -> None:
        if not xml_id:
            self._skip(rel, node.start_line, "record", "act_window declares no xml id")
            return
        res_model = self._text(rel, fields.get("res_model"))
        if not res_model:
            # A record with no res_model is amending a definition owned by another module.
            # Emitting it as a definition would report an action with no model at all.
            self._skip(rel, node.start_line, "record",
                       "record patches an existing act_window; no res_model declared")
            return
        name = self._text(rel, fields.get("name"))
        url_path = self._text(rel, fields.get("path"))
        route = self.route_for(xml_id, url_path)
        identity = self.identity(route)
        self.action_routes[xml_id] = route
        if name:
            self.action_names[xml_id] = name
        self._add("request_handler", identity, res_model,
                  {"module": module, "action_id": xml_id, "event_type": "view-only",
                   "name": name, "url_path": url_path,
                   "view_mode": self._text(rel, fields.get("view_mode")),
                   "domain": self._text(rel, fields.get("domain")),
                   "context": self._text(rel, fields.get("context")),
                   "target": self._text(rel, fields.get("target"))},
                  self._cite(rel, node, excerpt=240))
        primary = self._ref(module, fields, "view_id")
        if primary:
            self._add("request_response", identity, primary,
                      {"module": module, "response_type": "view",
                       "response_name": "success", "action_id": xml_id},
                      self._cite(rel, node, excerpt=160))

    def _act_window_view(self, rel: str, module: str, node, fields: dict) -> None:
        action = self._ref(module, fields, "act_window_id")
        view = self._ref(module, fields, "view_id")
        route = self.action_routes.get(action, "")
        if not action or not view:
            return
        self._add("request_response", self.identity(route or f"action-{action}"), view,
                  {"module": module, "response_type": "view",
                   "response_name": self._text(rel, fields.get("view_mode")) or "alternate",
                   "action_id": action},
                  self._cite(rel, node, excerpt=160))

    def _view(self, rel: str, module: str, node, xml_id: str, fields: dict) -> None:
        arch = fields.get("arch")
        if not xml_id or arch is None:
            return
        model = self._text(rel, fields.get("model"))
        inherit = self._ref(module, fields, "inherit_id")
        if inherit:
            self.view_parent[xml_id] = inherit
        view_type = next((child.tag for child in arch.children if child.tag in ARCH_ROOTS), "")
        name = self._text(rel, fields.get("name"))
        base = {"module": module, "model": model, "view_type": view_type,
                "inherit_id": inherit}
        self._pending_view_relations.append((
            self._add("view_screen", self.identity(xml_id), name or xml_id,
                      {**base, "view_id": xml_id}, self._cite(rel, node, excerpt=160)),
            xml_id))
        for field in arch.find_all("field"):
            name_attr = field.attrs.get("name", "")
            if not name_attr:
                continue
            self._pending_view_relations.append((
                self._add("form_field", xml_id, name_attr,
                          {**base, "widget": field.attrs.get("widget", ""),
                           **_conditions(field.attrs)},
                          self._cite(rel, field, excerpt=200)),
                xml_id))
        for button in arch.find_all("button"):
            target = button.attrs.get("name", "")
            if not target:
                continue
            action_ref = ACTION_REF_RE.search(target)
            resolved = qualify(module, action_ref.group(1)) if action_ref else target
            self._pending_view_relations.append((
                self._add("form_target", xml_id, resolved,
                          {**base, "target_type": button.attrs.get("type", "object"),
                           "label": button.attrs.get("string", ""),
                           "button_id": button.attrs.get("id", ""),
                           **_conditions(button.attrs)},
                          self._cite(rel, button, excerpt=240)),
                xml_id))

    def _resolve_root_views(self) -> None:
        """Stamp every view-derived relation with the primary view of its inheritance chain.

        Odoo assembles a rendered view from one primary view plus a chain of `inherit_id`
        patches. Without this, "which fields does the sales order form declare" can only be
        answered for whichever file happened to declare a given field.
        """
        cache: dict[str, str] = {}

        def root(view: str) -> str:
            seen: list[str] = []
            current = view
            while current in self.view_parent and current not in seen:
                seen.append(current)
                if current in cache:
                    current = cache[current]
                    break
                current = self.view_parent[current]
            for item in seen:
                cache[item] = current
            return current

        for relation, view in self._pending_view_relations:
            relation.attrs["root_view"] = root(view)
        self._pending_view_relations.clear()

    # -- XML: menus ---------------------------------------------------------------------
    def _menu_label(self, node, module: str) -> tuple[str, bool]:
        """The breadcrumb a user reads, and whether every segment of it is a real label.

        A `menuitem` without `name` displays the label of the action it opens; when neither
        exists the xml id is used and the caller is told the title is unresolved, so the
        core never presents an internal identifier as if it were user-visible text.
        """
        names: list[str] = []
        resolved = True
        current = node
        while current is not None and current.tag == "menuitem":
            label = current.attrs.get("name") or ""
            if not label:
                label = self.action_names.get(
                    qualify(module, current.attrs.get("action", "")), "")
            if not label:
                label = current.attrs.get("id", "")
                resolved = resolved and not label
            if label:
                names.append(label)
            current = current.parent
        return " / ".join(reversed(names)), resolved

    def _menus(self, rel: str, module: str, root) -> None:
        for node in root.find_all("menuitem"):
            action = qualify(module, node.attrs.get("action", ""))
            if not action:
                if not node.children:
                    self._skip(rel, node.start_line, "menuitem",
                               "leaf menu declares no action")
                continue
            route = self.action_routes.get(action)
            if route is None:
                self._skip(rel, node.start_line, "menuitem",
                           "menu opens an action this source tree does not define")
                continue
            title, resolved = self._menu_label(node, module)
            self._add("menu_link", title or action, self.identity(route),
                      {"module": module, "title": title, "title_resolved": resolved,
                       "menu_id": qualify(module, node.attrs.get("id", "")),
                       "action_id": action, "parameters": [],
                       "conditional": bool(node.attrs.get("groups")),
                       "sequence": node.attrs.get("sequence", ""),
                       **_conditions(node.attrs)},
                      self._cite(rel, node, excerpt=160))

    # -- Python models ------------------------------------------------------------------
    def _model_file(self, rel: str, module: str) -> None:
        try:
            tree = ast.parse(self.tree.read_text(rel))
        except (AccessDenied, SyntaxError, ValueError, RecursionError) as exc:
            self._skip(rel, 1, "#module", f"unparsed: {type(exc).__name__}")
            return
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            model = self._declared_model(node)
            if not model:
                continue
            for item in node.body:
                if isinstance(item, ast.Assign):
                    self._field(rel, module, model, item)
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._method(rel, module, model, item)

    @staticmethod
    def _declared_model(node: ast.ClassDef) -> str:
        for item in node.body:
            if (isinstance(item, ast.Assign) and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)
                    and item.targets[0].id in {"_name", "_inherit"}
                    and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, str)):
                return item.value.value
        return ""

    def _field(self, rel: str, module: str, model: str, item: ast.Assign) -> None:
        call = item.value
        if (not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute)
                or not isinstance(call.func.value, ast.Name)
                or call.func.value.id != "fields"
                or len(item.targets) != 1 or not isinstance(item.targets[0], ast.Name)):
            return
        attrs = {"module": module, "field_type": call.func.attr}
        for keyword in call.keywords:
            if keyword.arg in FIELD_KEYWORDS:
                attrs[keyword.arg] = _literal(keyword.value)
        if call.args and isinstance(call.args[0], ast.Constant):
            attrs.setdefault("string", str(call.args[0].value)[:_MAX_ATTR_CHARS])
        self._add("service_entity", model, item.targets[0].id, attrs,
                  self.tree.cite(rel, item.lineno, item.end_lineno or item.lineno,
                                 excerpt_chars=240))

    def _method(self, rel: str, module: str, model: str, item) -> None:
        if item.name.startswith("__"):
            return
        arguments = [argument.arg for argument in item.args.args if argument.arg != "self"]
        if item.args.vararg:
            arguments.append("*" + item.args.vararg.arg)
        if item.args.kwarg:
            arguments.append("**" + item.args.kwarg.arg)
        arguments += [argument.arg for argument in item.args.kwonlyargs]
        self._add("service_parameter", f"{model}.{item.name}", ",".join(arguments),
                  {"module": module,
                   "decorators": [_literal(node) for node in item.decorator_list],
                   "doc": (ast.get_docstring(item) or "")[:300]},
                  self.tree.cite(rel, item.lineno,
                                 min(item.lineno + 3, item.end_lineno or item.lineno),
                                 excerpt_chars=240))


def _literal(node) -> str:
    try:
        return str(ast.literal_eval(node))[:_MAX_ATTR_CHARS]
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return ast.unparse(node)[:_MAX_ATTR_CHARS]
