"""A compiler-free SourceMap fixture for core tests.

`SourceRetriever`, `ContractPlanner` and `validate_contract` consume `SourceRelation`
triples and citations. They do not consume XML. Building the fixture from triples, rather
than by running an application distribution's compiler, buys three things:

  * the core suite installs and runs without any application distribution;
  * the relations use an invented route grammar (`/{scope}/do/{route}`), so a core module
    that silently assumes the public reference's route spelling fails here;
  * a compiler change cannot quietly alter what the core retriever is tested against.

The descriptor files this writes are deliberately plain: their only job is to give every
citation a real byte range that `SourceTree.verify` can check, exactly as a real tree does.
Tests that need a *compiler's* output tested against the retriever belong in that
application's own distribution, not here.
"""
from __future__ import annotations

from pathlib import Path

from web_operation_agent.schemas import SCHEMA_VERSIONS, SourceMap, SourceRelation
from web_operation_agent.planning.sourcetree import SourceTree


FIXTURE_POLICY = {
    "repository_id": "fixture/invented",
    "release": "v0",
    "max_bytes_per_read": 1 << 20,
    "allow_globs": ["apps/**/*.decl"],
    "deny_globs": ["**/secrets/**"],
}

class _Writer:
    """Append one declaration line per relation and cite exactly those bytes."""

    def __init__(self, root: Path):
        self.root = root
        self._lines: dict[str, list[str]] = {}

    def line(self, path: str, text: str) -> tuple[int, int]:
        lines = self._lines.setdefault(path, [])
        lines.append(text)
        return len(lines), len(lines)

    def flush(self) -> None:
        for path, lines in self._lines.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_fixture_tree(root: Path) -> SourceTree:
    """Write the fixture descriptors under ``root`` and return the tree that reads them."""
    _, tree = _compile(root)
    return tree


def fixture_source_map(root: Path) -> SourceMap:
    """The invented two-application SourceMap the core retrieval tests are written against."""
    source_map, _tree = _compile(root)
    return source_map


def _compile(root: Path) -> tuple[SourceMap, SourceTree]:
    writer = _Writer(Path(root))
    plan: list[tuple[str, str, str, str, dict]] = []

    def declare(path: str, text: str, kind: str, subject: str, obj: str, attrs: dict):
        plan.append((path, text, kind, subject, obj, attrs))

    glasshouse = "apps/greenhouse/glasshouse.decl"
    beacon = "apps/lighthouse/beacon.decl"
    forms = "apps/greenhouse/glasshouse.forms"
    beacon_forms = "apps/lighthouse/beacon.forms"
    screens = "apps/greenhouse/glasshouse.screens"
    beacon_screens = "apps/lighthouse/beacon.screens"
    menus = "apps/greenhouse/glasshouse.menus"
    services = "apps/greenhouse/glasshouse.services"
    beacon_services = "apps/lighthouse/beacon.services"

    declare(glasshouse, "route findTrays handled by view-only, auth required",
            "request_handler", "glasshouse/findTrays", "",
            {"webapp": "glasshouse", "component": "greenhouse", "event_type": "view-only",
             "auth": "true", "https": ""})
    declare(glasshouse, "route findTrays responds success with view TrayList",
            "request_response", "glasshouse/findTrays", "TrayList",
            {"webapp": "glasshouse", "response_name": "success", "response_type": "view"})
    declare(glasshouse, "route sproutSeedling handled by service sproutSeedling",
            "request_handler", "glasshouse/sproutSeedling", "sproutSeedling",
            {"webapp": "glasshouse", "component": "greenhouse", "event_type": "service",
             "auth": "true", "https": "true"})
    declare(glasshouse, "route sproutSeedling responds success with view TrayDetail",
            "request_response", "glasshouse/sproutSeedling", "TrayDetail",
            {"webapp": "glasshouse", "response_name": "success", "response_type": "view"})
    declare(glasshouse, "route wateringRun handled by java org.invented.Watering#run",
            "request_handler", "glasshouse/wateringRun", "org.invented.Watering#run",
            {"webapp": "glasshouse", "component": "greenhouse", "event_type": "java",
             "auth": "", "https": ""})

    declare(menus, "menu item trays shown as Seed Trays targets findTrays",
            "menu_link", "greenhouse:GlasshouseAppBar:trays", "findTrays",
            {"title": "Seed Trays", "title_resolved": True, "component": "greenhouse",
             "webapps": ["glasshouse"], "url_mode": "",
             "parameters": [{"name": "trayId", "from_field": "selectedTrayId", "value": ""},
                            {"name": "viewMode", "from_field": "", "value": "summary"},
                            {"name": "gardenId", "from_field": "", "value": ""}],
             "conditional": False})
    declare(menus, "menu item watering shown as Watering targets wateringRun",
            "menu_link", "greenhouse:GlasshouseAppBar:watering", "wateringRun",
            {"title": "Watering", "title_resolved": True, "component": "greenhouse",
             "webapps": ["glasshouse"], "url_mode": "", "parameters": [],
             "conditional": False})

    sprout_form = f"{forms}#SproutSeedlingForm"
    declare(forms, "form SproutSeedlingForm targets sproutSeedling",
            "form_target", sprout_form, "sproutSeedling",
            {"component": "greenhouse", "webapps": ["glasshouse"], "form_type": "single",
             "form_name": "SproutSeedlingForm", "target_kind": "primary", "use_when": ""})
    for field, widget in (("trayId", "text"), ("cultivarId", "text"),
                          ("sproutButton", "submit")):
        declare(forms, f"form SproutSeedlingForm submits {field} as {widget}",
                "form_field", sprout_form, field,
                {"widget": widget, "required": "", "form_name": "SproutSeedlingForm"})
    declare(screens, "screen TrayDetail includes form SproutSeedlingForm",
            "screen_includes_form", f"{screens}#TrayDetail", sprout_form,
            {"component": "greenhouse", "screen_name": "TrayDetail",
             "form_name": "SproutSeedlingForm"})

    declare(services, "service sproutSeedling is declared against entity Seedling",
            "service_entity", "sproutSeedling", "Seedling",
            {"engine": "entity-auto", "invoke": "create", "auth": ""})
    for name, mode, kind, optional in (("*auto*", "IN", "", "false"),
                                       ("trayId", "IN", "String", "false"),
                                       ("cultivarId", "IN", "String", "false"),
                                       ("nurseryNote", "IN", "String", "true"),
                                       ("seedlingId", "OUT", "String", "false")):
        attrs = {"mode": mode, "optional": optional}
        attrs.update({"include": "pk", "entity": "Seedling"} if not kind else {"type": kind})
        declare(services, f"service sproutSeedling declares {mode} parameter {name}",
                "service_parameter", "sproutSeedling", name, attrs)

    declare(beacon, "route LampSchedule/Edit handled by view-only",
            "request_handler", "beacon/LampSchedule/Edit", "",
            {"webapp": "beacon", "component": "lighthouse", "event_type": "view-only",
             "auth": "", "https": ""})
    declare(beacon, "route LampSchedule/Edit responds success with view LampSchedule/Edit",
            "request_response", "beacon/LampSchedule/Edit", "LampSchedule/Edit",
            {"webapp": "beacon", "response_name": "success", "response_type": "view"})
    declare(beacon, "route LampSchedule/create handled by service createLampSchedule",
            "request_handler", "beacon/LampSchedule/create", "createLampSchedule",
            {"webapp": "beacon", "component": "lighthouse", "event_type": "service",
             "auth": "", "https": ""})
    for response in ("success", "error"):
        declare(beacon, f"route LampSchedule/create responds {response} with view ScheduleList",
                "request_response", "beacon/LampSchedule/create", "ScheduleList",
                {"webapp": "beacon", "response_name": response, "response_type": "view"})
    declare(beacon, "view LampSchedule/Edit renders screen LampEditorScreen",
            "view_screen", "beacon/LampSchedule/Edit",
            f"{beacon_screens}#LampEditorScreen",
            {"webapp": "beacon", "component": "lighthouse", "view_type": "screen"})

    new_schedule = f"{beacon_forms}#NewLampSchedule"
    lamp_editor = f"{beacon_forms}#LampEditor"
    declare(beacon_forms, "form NewLampSchedule targets LampSchedule/create",
            "form_target", new_schedule, "LampSchedule/create",
            {"component": "lighthouse", "webapps": ["beacon"], "form_type": "single",
             "form_name": "NewLampSchedule", "target_kind": "primary", "use_when": ""})
    for field in ("beaconId", "rotationSeconds"):
        declare(beacon_forms, f"form NewLampSchedule submits {field} as text",
                "form_field", new_schedule, field,
                {"widget": "text", "required": "", "form_name": "NewLampSchedule"})
    declare(beacon_forms, "form LampEditor targets LampSchedule/update",
            "form_target", lamp_editor, "LampSchedule/update",
            {"component": "lighthouse", "webapps": ["beacon"], "form_type": "single",
             "form_name": "LampEditor", "target_kind": "primary", "use_when": ""})
    declare(beacon_forms,
            "form LampEditor targets LampSchedule/create when schedule==null",
            "form_target", lamp_editor, "LampSchedule/create",
            {"component": "lighthouse", "webapps": ["beacon"], "form_type": "single",
             "form_name": "LampEditor", "target_kind": "alternate",
             "use_when": "schedule==null"})
    for field in ("beaconId", "rotationSeconds"):
        declare(beacon_forms, f"form LampEditor submits {field} as text",
                "form_field", lamp_editor, field,
                {"widget": "text", "required": "", "form_name": "LampEditor"})
    declare(beacon_screens, "screen LampEditorScreen includes form LampEditor",
            "screen_includes_form", f"{beacon_screens}#LampEditorScreen", lamp_editor,
            {"component": "lighthouse", "screen_name": "LampEditorScreen",
             "form_name": "LampEditor"})

    declare(beacon_services, "service createLampSchedule is a simple service",
            "service_entity", "createLampSchedule", "",
            {"engine": "simple", "invoke": "", "auth": ""})
    for name, kind in (("beaconId", "String"), ("rotationSeconds", "Long")):
        declare(beacon_services, f"service createLampSchedule declares IN parameter {name}",
                "service_parameter", "createLampSchedule", name,
                {"mode": "IN", "type": kind, "optional": "false"})

    spans = [(path, kind, subject, obj, attrs, writer.line(path, text))
             for path, text, kind, subject, obj, attrs in plan]
    writer.flush()
    tree = SourceTree({**FIXTURE_POLICY,
                       "allow_globs": ["apps/**/*.decl", "apps/**/*.forms",
                                       "apps/**/*.screens", "apps/**/*.menus",
                                       "apps/**/*.services"]}, Path(root))
    relations = [
        SourceRelation(kind=kind, subject=subject, object=obj, attrs=attrs,
                       evidence=(tree.cite(path, start, end, excerpt_chars=200),))
        for path, kind, subject, obj, attrs, (start, end) in spans
    ]
    source_map = SourceMap(
        schema_version=SCHEMA_VERSIONS["SourceMap"],
        attestation_id="fixture",
        repository_id=tree.repository_id,
        release=tree.release,
        relations=relations,
        unsupported=[],
    )
    return source_map, tree
