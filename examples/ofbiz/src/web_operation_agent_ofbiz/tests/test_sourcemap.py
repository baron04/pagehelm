"""The compiler is checked against invented applications, never against the real one.

If the only fixture were OFBiz's own accounting module, a compiler that had quietly learned
the words "payment" and "invoice" would pass. So every case here is a made-up application
with made-up vocabulary, and the assertions are about the *shape* of what is extracted.
"""
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.composition.sources import SourceCompiler
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent_ofbiz.sourcemap import OFBizCompiler

POLICY = {
    "repository_id": "fixture/invented", "release": "v0",
    "max_bytes_per_read": 1 << 20,
    "allow_globs": ["applications/**/webapp/**/WEB-INF/*controller*.xml",
                    "applications/**/widget/**/*.xml",
                    "applications/**/servicedef/*.xml",
                    "applications/**/config/*UiLabels.xml"],
    "deny_globs": ["**/secrets/**"],
}

# Three unrelated invented domains, different naming conventions in each.
FIXTURES = {
    "greenhouse": {
        "applications/greenhouse/webapp/glasshouse/WEB-INF/controller.xml": """<?xml version="1.0"?>
<site-conf xmlns="http://ofbiz.apache.org/Site-Conf">
  <request-map uri="findTrays"><security auth="true"/><response name="success" type="view" value="TrayList"/></request-map>
  <request-map uri="sproutSeedling">
    <security https="true" auth="true"/>
    <event type="service" invoke="sproutSeedling"/>
    <response name="success" type="view" value="TrayDetail"/>
  </request-map>
  <request-map uri="wateringRun"><event type="java" path="org.invented.Watering" invoke="run"/></request-map>
</site-conf>""",
        "applications/greenhouse/widget/GlasshouseMenus.xml": """<?xml version="1.0"?>
<menus xmlns="http://ofbiz.apache.org/Widget-Menu">
  <menu name="GlasshouseAppBar">
    <menu-item name="trays" title="${uiLabelMap.GreenhouseTraysMenu}">
      <link target="findTrays">
        <parameter param-name="trayId" from-field="selectedTrayId"/>
        <parameter param-name="viewMode" value="summary"/>
        <parameter param-name="gardenId"/>
      </link>
    </menu-item>
    <menu-item name="watering" title="Watering"><link target="wateringRun"/></menu-item>
  </menu>
</menus>""",
        "applications/greenhouse/widget/GlasshouseForms.xml": """<?xml version="1.0"?>
<forms xmlns="http://ofbiz.apache.org/Widget-Form">
  <form name="SproutSeedlingForm" type="single" target="sproutSeedling">
    <field name="trayId"><text/></field>
    <field name="cultivarId"><text/></field>
    <field name="sproutButton"><submit/></field>
  </form>
  <form name="TrayReport" type="list">
    <field name="trayId"><display/></field>
  </form>
</forms>""",
        "applications/greenhouse/widget/GlasshouseScreens.xml": """<?xml version="1.0"?>
<screens xmlns="http://ofbiz.apache.org/Widget-Screen">
  <screen name="TrayDetail">
    <section><widgets>
      <include-form name="SproutSeedlingForm" location="component://greenhouse/widget/GlasshouseForms.xml"/>
    </widgets></section>
  </screen>
</screens>""",
        "applications/greenhouse/servicedef/services_tray.xml": """<?xml version="1.0"?>
<services xmlns="http://ofbiz.apache.org/Service-Definition">
  <service name="sproutSeedling" engine="entity-auto" invoke="create" default-entity-name="Seedling">
    <auto-attributes mode="IN" include="pk" optional="false"/>
    <attribute name="trayId" type="String" mode="IN" optional="false"/>
    <attribute name="cultivarId" type="String" mode="IN" optional="false"/>
    <attribute name="nurseryNote" type="String" mode="IN" optional="true"/>
    <attribute name="seedlingId" type="String" mode="OUT" optional="false"/>
  </service>
</services>""",
        "applications/greenhouse/config/GreenhouseUiLabels.xml": """<?xml version="1.0"?>
<resource>
  <property key="GreenhouseTraysMenu">
    <value xml:lang="de">Schalen</value>
    <value xml:lang="en">Seed Trays</value>
  </property>
</resource>""",
    },
    "lighthouse": {
        "applications/lighthouse/webapp/beacon/WEB-INF/beacon-controller.xml": """<?xml version="1.0"?>
<site-conf xmlns="http://ofbiz.apache.org/Site-Conf">
  <request-map uri="LampSchedule/Edit">
    <response name="success" type="view" value="LampSchedule/Edit"/>
  </request-map>
  <request-map uri="LampSchedule/create">
    <event type="service" invoke="createLampSchedule"/>
    <response name="success" type="view" value="ScheduleList"/>
    <response name="error" type="view" value="ScheduleList"/>
  </request-map>
  <view-map name="LampSchedule/Edit" type="screen"
            page="component://lighthouse/widget/beacon/BeaconScreens.xml#LampEditorScreen"/>
</site-conf>""",
        "applications/lighthouse/widget/beacon/BeaconForms.xml": """<?xml version="1.0"?>
<forms xmlns="http://ofbiz.apache.org/Widget-Form">
  <form name="NewLampSchedule" type="single" target="LampSchedule/create">
    <field name="beaconId"><text/></field>
    <field name="rotationSeconds"><text/></field>
  </form>
  <form name="LampEditor" type="single" target="LampSchedule/update">
    <alt-target target="LampSchedule/create" use-when="schedule==null"/>
    <field name="beaconId"><text/></field>
    <field name="rotationSeconds"><text/></field>
  </form>
</forms>""",
        "applications/lighthouse/widget/beacon/BeaconScreens.xml": """<?xml version="1.0"?>
<screens xmlns="http://ofbiz.apache.org/Widget-Screen">
  <screen name="LampEditorScreen"><section><widgets>
    <include-form name="LampEditor"
                  location="component://lighthouse/widget/beacon/BeaconForms.xml"/>
  </widgets></section></screen>
</screens>""",
        "applications/lighthouse/servicedef/services.xml": """<?xml version="1.0"?>
<services>
  <service name="createLampSchedule" engine="simple">
    <attribute name="beaconId" type="String" mode="IN" optional="false"/>
    <attribute name="rotationSeconds" type="Long" mode="IN" optional="false"/>
  </service>
</services>""",
    },
    "broken": {
        "applications/broken/webapp/broke/WEB-INF/controller.xml": """<?xml version="1.0"?>
<site-conf xmlns="http://ofbiz.apache.org/Site-Conf">
  <request-map><security auth="true"/></request-map>
  <request-map uri="oddity"><event type="telepathy" invoke="guess"/></request-map>
</site-conf>""",
        "applications/broken/widget/BrokeForms.xml": "<forms><form name=\"Unclosed\" target=\"x\">",
    },
}


def build(tmp: Path, which) -> SourceTree:
    for group in which:
        for rel, body in FIXTURES[group].items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
    return SourceTree(POLICY, tmp)


def compile_fixture(tmp: Path, *which):
    return OFBizCompiler(build(tmp, which), "fixture-attestation").compile()


class PortConformance(unittest.TestCase):
    def test_ofbiz_compiler_satisfies_the_core_source_compiler_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            compiler = OFBizCompiler(build(Path(tmp), ()), "fixture-attestation")
        self.assertIsInstance(compiler, SourceCompiler)


class Extraction(unittest.TestCase):
    def test_the_full_chain_is_extracted_from_an_invented_application(self):
        with tempfile.TemporaryDirectory() as d:
            smap = compile_fixture(Path(d), "greenhouse")
            menus = {r.attrs["title"]: r.object for r in smap.by_kind("menu_link")}
            self.assertEqual(menus["Seed Trays"], "findTrays",
                             "the English label must be resolved from the label file")
            self.assertEqual(menus["Watering"], "wateringRun")

            trays = next(r for r in smap.by_kind("menu_link")
                         if r.attrs["title"] == "Seed Trays")
            self.assertEqual(trays.attrs["parameters"], [
                {"name": "trayId", "from_field": "selectedTrayId", "value": ""},
                {"name": "viewMode", "from_field": "", "value": "summary"},
                {"name": "gardenId", "from_field": "", "value": ""},
            ])
            self.assertTrue(trays.evidence[0].excerpt.lstrip().startswith("<menu-item"))
            self.assertGreater(trays.evidence[0].end_line, trays.evidence[0].start_line)

            handlers = {r.subject: r for r in smap.by_kind("request_handler")}
            self.assertEqual(handlers["glasshouse/sproutSeedling"].object, "sproutSeedling")
            self.assertEqual(handlers["glasshouse/sproutSeedling"].attrs["event_type"], "service")
            self.assertEqual(handlers["glasshouse/wateringRun"].object,
                             "org.invented.Watering#run")
            self.assertEqual(handlers["glasshouse/findTrays"].attrs["event_type"], "view-only")

            forms = {r.attrs["form_name"]: r.object for r in smap.by_kind("form_target")}
            self.assertEqual(forms["SproutSeedlingForm"], "sproutSeedling")
            self.assertNotIn("TrayReport", forms, "a form with no target submits nothing")

            fields = {r.object for r in smap.by_kind("form_field")
                      if r.attrs["form_name"] == "SproutSeedlingForm"}
            self.assertEqual(fields, {"trayId", "cultivarId", "sproutButton"})

            params = [r for r in smap.by_kind("service_parameter")
                      if r.subject == "sproutSeedling"]
            required = {r.object for r in params
                        if r.attrs.get("optional") == "false" and r.attrs.get("mode") == "IN"
                        and r.object != "*auto*"}
            self.assertEqual(required, {"trayId", "cultivarId"})
            optional = {r.object for r in params if r.attrs.get("optional") == "true"}
            self.assertEqual(optional, {"nurseryNote"})
            outs = {r.object for r in params if r.attrs.get("mode") == "OUT"}
            self.assertEqual(outs, {"seedlingId"})
            # An `auto-attributes` block expands at runtime against an entity this compiler
            # does not read, so it is recorded as an explicit unknown rather than expanded.
            auto = [r for r in params if r.object == "*auto*"]
            self.assertEqual(len(auto), 1)
            self.assertEqual(auto[0].attrs["entity"], "Seedling")

            screens = smap.by_kind("screen_includes_form")
            self.assertTrue(any(s.object.endswith("#SproutSeedlingForm") for s in screens))

    def test_a_multi_segment_request_name_is_kept_intact(self):
        with tempfile.TemporaryDirectory() as d:
            smap = compile_fixture(Path(d), "lighthouse")
            self.assertIn("beacon/LampSchedule/create",
                          {r.subject for r in smap.by_kind("request_handler")})
            self.assertIn("LampSchedule/create",
                          {r.object for r in smap.by_kind("form_target")})

    def test_a_controller_view_is_bound_to_its_exact_widget_screen(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            tree = build(tmp, ("lighthouse",))
            smap = OFBizCompiler(tree, "fixture").compile()
            view = next(r for r in smap.by_kind("view_screen")
                        if r.subject == "beacon/LampSchedule/Edit")
            self.assertEqual(
                view.object,
                "applications/lighthouse/widget/beacon/BeaconScreens.xml#LampEditorScreen")
            self.assertTrue(tree.verify(view.evidence[0]))

    def test_a_conditional_alternate_target_is_a_separate_cited_relation(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            tree = build(tmp, ("greenhouse",))
            path = tmp / "applications/greenhouse/widget/AlternateForms.xml"
            path.write_text('''<forms>
              <form name="SeedlingEditor" target="Seedling/update">
                <alt-target target="Seedling/create" use-when="seedling==null"/>
                <field name="trayId"><text/></field>
              </form>
            </forms>''')
            smap = OFBizCompiler(SourceTree(POLICY, tmp), "fixture").compile()
            targets = [r for r in smap.by_kind("form_target")
                       if r.attrs.get("form_name") == "SeedlingEditor"]
            self.assertEqual([(r.object, r.attrs["target_kind"], r.attrs["use_when"])
                              for r in targets],
                             [("Seedling/update", "primary", ""),
                              ("Seedling/create", "alternate", "seedling==null")])
            for relation in targets:
                self.assertTrue(tree.verify(relation.evidence[0]))
            self.assertGreater(targets[1].evidence[0].start_line,
                               targets[0].evidence[0].start_line)

    def test_an_included_controller_file_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            smap = compile_fixture(Path(d), "lighthouse")
            self.assertTrue(smap.by_kind("request_handler"),
                            "beacon-controller.xml must be compiled, not only controller.xml")

    def test_what_cannot_be_interpreted_is_recorded_not_guessed(self):
        with tempfile.TemporaryDirectory() as d:
            smap = compile_fixture(Path(d), "broken")
            whys = " ".join(u["why"] for u in smap.unsupported)
            self.assertIn("telepathy", whys, "an unknown event type must be reported")
            self.assertIn("unreadable", whys, "malformed XML must be reported, not skipped")
            self.assertFalse([r for r in smap.by_kind("request_handler")
                              if r.subject.endswith("/oddity") and r.object],
                             "an uninterpretable handler must not be given an invented object")

    def test_every_relation_carries_a_citation_that_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            tree = build(tmp, ("greenhouse", "lighthouse"))
            smap = OFBizCompiler(tree, "a").compile()
            self.assertTrue(smap.relations)
            for r in smap.relations:
                self.assertTrue(r.evidence, f"{r.kind}/{r.subject} has no evidence")
                for ref in r.evidence:
                    self.assertTrue(tree.verify(ref),
                                    f"citation {ref.key} does not verify against the tree")

    def test_unresolved_label_keys_are_flagged_not_faked(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "applications/x/widget").mkdir(parents=True)
            (tmp / "applications/x/widget/M.xml").write_text(
                '<menus><menu name="M"><menu-item name="i" title="${uiLabelMap.NoSuchKey}">'
                '<link target="t"/></menu-item></menu></menus>')
            smap = OFBizCompiler(SourceTree(POLICY, tmp), "a").compile()
            item = smap.by_kind("menu_link")[0]
            self.assertFalse(item.attrs["title_resolved"])
            self.assertIn("uiLabelMap.NoSuchKey", item.attrs["title"])


class Determinism(unittest.TestCase):
    def test_compiling_twice_gives_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            tree = build(tmp, ("greenhouse", "lighthouse", "broken"))
            a = OFBizCompiler(tree, "a").compile()
            b = OFBizCompiler(SourceTree(POLICY, tmp), "a").compile()
            self.assertEqual(a.content_digest, b.content_digest)


if __name__ == "__main__":
    unittest.main()


COMPOUND_FIXTURE = {
    "applications/orchard/webapp/grove/WEB-INF/controller.xml": """<?xml version="1.0"?>
<site-conf xmlns="http://ofbiz.apache.org/Site-Conf">
  <request-map uri="findPlots"><security auth="true"/></request-map>
</site-conf>""",
    # OFBiz declares request-maps inside compound widgets too, under widget/ rather than
    # WEB-INF/, with their own namespace prefixes.
    "applications/orchard/widget/grove/GroveHarvestCpd.xml": """<?xml version="1.0"?>
<compound-widgets xmlns:sc="http://ofbiz.apache.org/Site-Conf"
                  xmlns:wf="http://ofbiz.apache.org/Widget-Form">
  <sc:site-conf>
    <sc:request-map uri="Grove/Harvest/record">
      <sc:security https="true" auth="true"/>
      <sc:event type="service" invoke="recordGroveHarvest"/>
      <sc:response name="success" type="request" value="json"/>
    </sc:request-map>
  </sc:site-conf>
  <wf:forms>
    <wf:form name="RecordHarvest" type="single" target="Grove/Harvest/record">
      <wf:field name="plotId"><wf:hidden/></wf:field>
      <wf:field name="bushels"><wf:text/></wf:field>
    </wf:form>
  </wf:forms>
</compound-widgets>""",
}


class CompoundWidgetRequestMaps(unittest.TestCase):
    """A request the map cannot see is a request the planner cannot reason about.

    In the deployed OFBiz release 154 request-maps live in compound-widget files, one of them
    the invoice-side payment-application entry a source-armed run kept submitting to. The map
    knew the form's target and nothing about what it invoked.
    """

    def _compile(self, tmp: Path):
        for rel, body in COMPOUND_FIXTURE.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        return OFBizCompiler(SourceTree(POLICY, tmp), "fixture").compile()

    def test_a_request_declared_in_a_compound_widget_is_compiled(self):
        with tempfile.TemporaryDirectory() as d:
            smap = self._compile(Path(d))
            handlers = {r.subject: r for r in smap.by_kind("request_handler")}
            self.assertIn("grove/Grove/Harvest/record", handlers,
                          f"compound-widget request-map missing; have {sorted(handlers)}")
            h = handlers["grove/Grove/Harvest/record"]
            self.assertEqual(h.object, "recordGroveHarvest")
            self.assertEqual(h.attrs["event_type"], "service")
            self.assertEqual(h.attrs["auth"], "true")

    def test_its_response_shape_is_recorded_too(self):
        with tempfile.TemporaryDirectory() as d:
            smap = self._compile(Path(d))
            resp = [r for r in smap.by_kind("request_response")
                    if r.subject == "grove/Grove/Harvest/record"]
            self.assertTrue(resp)
            self.assertEqual(resp[0].object, "json")

    def test_the_citation_points_at_the_compound_file(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            smap = self._compile(tmp)
            h = next(r for r in smap.by_kind("request_handler")
                     if r.subject == "grove/Grove/Harvest/record")
            self.assertTrue(h.evidence[0].path.endswith("GroveHarvestCpd.xml"))
            self.assertTrue(SourceTree(POLICY, tmp).verify(h.evidence[0]))
