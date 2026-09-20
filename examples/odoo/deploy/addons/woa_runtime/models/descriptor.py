"""Actor-scoped metadata used to derive assistant capabilities.

This is intentionally metadata, not a generic data endpoint.  Odoo's ordinary delegated
users cannot enumerate ``ir.model`` or ``ir.actions.act_window`` even though the web client
can load their permitted menus and call ``fields_get`` once it already knows a model name.
Keeping the enumeration here lets Odoo apply the current user's ACLs and group-filtered menu
tree before anything reaches the Agent server.

The method grants no read or write on business records.  A later generic read still has to
execute through the delegated actor, and a write still needs the product's proposal,
confirmation, one-dispatch and readback protocol.
"""
from __future__ import annotations

import hashlib
import json
import re

from odoo import api, models, release
from odoo.exceptions import AccessError, ValidationError


_ACTION = re.compile(r"ir\.actions\.act_window,([1-9][0-9]*)\Z")
_MAX_MODELS = 5_000
_MAX_ACTIONS = 5_000
_MAX_MODULES = 5_000


def _digest(models_payload, actions_payload, omitted_models, omitted_actions):
    body = json.dumps(
        {"models": models_payload, "actions": actions_payload,
         "omitted_models": omitted_models, "omitted_actions": omitted_actions},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _parent_id(menu):
    value = menu.get("parent_id")
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _breadcrumb(menu_id, menus):
    labels = []
    seen = set()
    current = menu_id
    while current and current not in seen and len(labels) < 20:
        seen.add(current)
        menu = menus.get(current) or menus.get(str(current))
        if not isinstance(menu, dict):
            break
        label = str(menu.get("name") or "").strip()
        if label:
            labels.append(label[:200])
        current = _parent_id(menu)
    return " / ".join(reversed(labels))[:1_000]


def _module_digest(rows):
    return "sha256:" + hashlib.sha256(json.dumps(
        [(row["name"], row["version"]) for row in rows],
        ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


class WoaRuntimeDescriptor(models.AbstractModel):
    _name = "woa.runtime.descriptor"
    _description = "Actor-scoped Web Operation Agent metadata"

    def _require_internal_user(self):
        # Portal users have an authenticated session too, but exposing the installed module
        # list or registry shape to them would widen Odoo's ordinary information boundary.
        # The embedded backend assistant is an internal-user product surface.
        if not self.env.user.has_group("base.group_user"):
            raise AccessError("runtime capability discovery requires an internal user")

    def _source_manifest(self):
        installed = self.env["ir.module.module"].sudo().search_read(
            [("state", "=", "installed")], ["name", "installed_version"], order="name")
        if len(installed) > _MAX_MODULES:
            raise ValidationError("installed module manifest exceeds its safety limit")
        modules = [{"name": str(row.get("name") or "")[:64],
                    "version": str(row.get("installed_version") or "")[:100]}
                   for row in installed]
        return modules, _module_digest(modules)

    @api.model
    @api.readonly
    def source_manifest(self):
        """Return only what an offline compiler needs to exclude uninstalled code."""
        self._require_internal_user()
        modules, module_digest = self._source_manifest()
        return {"schema_version": 1, "odoo_release": str(release.version),
                "module_digest": module_digest, "modules": modules}

    @api.model
    @api.readonly
    def catalog(self):
        """Return a bounded, deterministic catalog for the current actor and language."""
        self._require_internal_user()
        model_rows = []
        omitted_models = []
        model_names = sorted(self.env.registry.models)
        if len(model_names) > _MAX_MODELS:
            raise ValidationError("runtime model catalog exceeds its safety limit")
        for name in model_names:
            model = self.env[name]
            if getattr(model, "_abstract", False):
                continue
            empty = model.browse()
            try:
                readable = bool(empty.has_access("read"))
                if not readable:
                    continue
                operations = [
                    operation for operation in ("create", "read", "write", "unlink")
                    if empty.has_access(operation)
                ]
            except Exception:  # one unusual model must not erase the whole safe catalog
                omitted_models.append(name)
                continue
            model_rows.append({
                "model": name,
                "label": str(getattr(model, "_description", "") or name)[:200],
                "operations": operations,
                "transient": bool(getattr(model, "_transient", False)),
            })

        menus = self.env["ir.ui.menu"].load_menus(False)
        action_menus = {}
        for key, menu in menus.items():
            if not isinstance(menu, dict):
                continue
            match = _ACTION.fullmatch(str(menu.get("action") or ""))
            if match is None:
                continue
            action_id = int(match.group(1))
            action_menus.setdefault(action_id, []).append((key, menu))

        action_rows = []
        omitted_actions = []
        if len(action_menus) > _MAX_ACTIONS:
            raise ValidationError("runtime action catalog exceeds its safety limit")
        for action_id in sorted(action_menus):
            # This is the same sudo read used by Odoo's auth=user /web/action/load.  The
            # action id came from load_menus(), whose result has already been filtered for
            # this actor.  We do not enumerate the action table under sudo.
            action = self.env["ir.actions.act_window"].sudo().browse(action_id).exists()
            if not action:
                omitted_actions.append(action_id)
                continue
            try:
                descriptor = action._get_action_dict()
            except Exception:
                omitted_actions.append(action_id)
                continue
            target = str(descriptor.get("res_model") or "")
            readable = False
            if target and target in self.env:
                try:
                    readable = bool(self.env[target].browse().has_access("read"))
                except Exception:
                    readable = False
            xml_id = str(descriptor.get("xml_id") or "")[:200]
            path = str(descriptor.get("path") or "")[:200]
            route = path or f"action-{xml_id or action_id}"
            views = descriptor.get("views")
            modes = []
            if isinstance(views, (list, tuple)):
                for item in views:
                    if (isinstance(item, (list, tuple)) and len(item) == 2
                            and isinstance(item[1], str) and item[1] not in modes):
                        modes.append(item[1][:40])
            breadcrumbs = []
            for key, menu in action_menus[action_id]:
                breadcrumb = _breadcrumb(
                    menu.get("id") if isinstance(menu.get("id"), int) else key, menus)
                if breadcrumb and breadcrumb not in breadcrumbs:
                    breadcrumbs.append(breadcrumb)
            breadcrumbs = sorted(breadcrumbs)[:20]
            action_rows.append({
                "action_id": action_id,
                "xml_id": xml_id,
                "name": str(descriptor.get("name") or "")[:200],
                "route": route,
                "model": target[:128],
                "readable": readable,
                "view_modes": modes,
                "breadcrumbs": breadcrumbs,
                # These are untrusted application expressions, useful for explaining an
                # action's scope but never executable by the Agent server.
                "domain": str(descriptor.get("domain") or "")[:2_000],
                "context": str(descriptor.get("context") or "")[:2_000],
            })

        _modules, module_digest = self._source_manifest()
        return {
            "schema_version": 1,
            "odoo_release": str(release.version),
            "language": str(self.env.lang or ""),
            "company_id": self.env.company.id,
            "module_digest": module_digest,
            "descriptor_digest": _digest(
                model_rows, action_rows, omitted_models, omitted_actions),
            "models": model_rows,
            "actions": action_rows,
            # A resolver may still use the catalog for positive matches, but it must not
            # claim a whole-catalog capability gap while either list is non-empty.
            "omitted_models": omitted_models,
            "omitted_actions": omitted_actions,
        }
