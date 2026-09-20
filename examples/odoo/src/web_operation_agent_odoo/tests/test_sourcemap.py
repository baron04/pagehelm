"""The Odoo compiler, against an invented module tree.

The fixture is two invented Odoo modules, not a slice of the real `sale` module, so a test
can never be satisfied by the compiler happening to know Odoo's own names. What it does
mirror exactly is Odoo's declarative shape: nested `menuitem`s that inherit their label from
the action they open, an `ir.actions.act_window` with a stored URL `path`, an `ir.ui.view`
whose buttons are distinguished only by their `invisible` conditions, and a model whose
`fields.*` declarations carry the selection a page never shows.
"""
import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.composition.sources import SourceCompiler
from web_operation_agent.affordance import resolve_source_affordances
from web_operation_agent.sourcetree import SourceTree

from web_operation_agent_odoo.route import ODOO_ROUTE_PORT, ODOO_SOURCE_SCOPE
from web_operation_agent_odoo.sourcemap import ODOO_SOURCE_POLICY, OdooCompiler


FIXTURES = {
    "addons/greenhouse/views/tray_menus.xml": """<?xml version="1.0"?>
<odoo>
    <menuitem id="greenhouse_root" name="Greenhouse" sequence="30">
        <menuitem id="tray_menu" name="Trays" sequence="10">
            <menuitem id="menu_trays" action="action_trays" sequence="10"/>
            <menuitem id="menu_cultivars" action="action_cultivars"
                      groups="greenhouse.group_grower" sequence="20"/>
        </menuitem>
    </menuitem>
</odoo>""",
    "addons/greenhouse/views/tray_views.xml": """<?xml version="1.0"?>
<odoo>
    <record id="view_tray_form" model="ir.ui.view">
        <field name="name">greenhouse.tray.form</field>
        <field name="model">greenhouse.tray</field>
        <field name="arch" type="xml">
            <form string="Tray">
                <header>
                    <button name="action_sprout" type="object" string="Sprout"
                            invisible="state != 'draft'"/>
                    <button name="action_sprout" type="object" string="Sprout"
                            invisible="state != 'ready' or not cultivar_id"
                            context="{'default_mode': 'bulk'}"/>
                    <button name="%(greenhouse.action_cultivars)d" type="action"
                            string="Pick Cultivar" confirm="Replace the current cultivar?"/>
                </header>
                <field name="name" readonly="state == 'done'"/>
                <field name="cultivar_id" domain="[('sale_ok', '=', True)]" required="1"/>
                <field name="state" invisible="1"/>
            </form>
        </field>
    </record>

    <record id="view_tray_list" model="ir.ui.view">
        <field name="name">greenhouse.tray.list</field>
        <field name="model">greenhouse.tray</field>
        <field name="arch" type="xml">
            <list string="Trays">
                <field name="name"/>
                <field name="state" column_invisible="True"/>
            </list>
        </field>
    </record>

    <record id="view_tray_form_extra" model="ir.ui.view">
        <field name="name">greenhouse.tray.form.extra</field>
        <field name="model">greenhouse.tray</field>
        <field name="inherit_id" ref="view_tray_form"/>
        <field name="arch" type="xml">
            <form>
                <field name="nursery_note"/>
            </form>
        </field>
    </record>

    <record id="action_trays" model="ir.actions.act_window">
        <field name="name">Seed Trays</field>
        <field name="path">trays</field>
        <field name="res_model">greenhouse.tray</field>
        <field name="view_id" ref="view_tray_list"/>
        <field name="view_mode">list,form</field>
        <field name="domain">[('active', '=', True)]</field>
    </record>

    <record id="action_cultivars" model="ir.actions.act_window">
        <field name="name">Cultivars</field>
        <field name="res_model">greenhouse.cultivar</field>
        <field name="view_mode">list,form</field>
    </record>

    <record id="other_module.someone_elses_action" model="ir.actions.act_window">
        <field name="context">{'search_default_mine': 1}</field>
    </record>
</odoo>""",
    "addons/greenhouse/models/tray.py": '''from odoo import api, fields, models


class Tray(models.Model):
    _name = "greenhouse.tray"
    _description = "Seed tray"

    name = fields.Char("Reference", required=True)
    state = fields.Selection(
        selection=[("draft", "Draft"), ("ready", "Ready"), ("done", "Done")],
        string="Status", default="draft", store=True)
    sprout_policy = fields.Selection(
        selection=[("manual", "Manual"), ("on_delivery", "On delivery")],
        string="Sprouting Policy", compute="_compute_sprout_policy", store=True)
    cultivar_id = fields.Many2one(comodel_name="greenhouse.cultivar")

    @api.depends("state", "cultivar_id.sale_ok")
    def _compute_sprout_policy(self):
        """Mirrors Odoo's own computed-field shape."""
        pass

    def action_sprout(self, cultivar_id, note=None, *extra, **options):
        return True
''',
    "addons/lighthouse/views/lamp_views.xml": """<?xml version="1.0"?>
<odoo>
    <record id="view_lamp_form" model="ir.ui.view">
        <field name="name">lighthouse.lamp.form</field>
        <field name="model">lighthouse.lamp</field>
        <field name="arch" type="xml">
            <form string="Lamp"><field name="rotation_seconds"/></form>
        </field>
    </record>
    <record id="action_lamps" model="ir.actions.act_window">
        <field name="name">Lamps</field>
        <field name="path">lamps</field>
        <field name="res_model">lighthouse.lamp</field>
        <field name="view_mode">list,form</field>
    </record>
    <record id="action_lamps_list_view" model="ir.actions.act_window.view">
        <field name="act_window_id" ref="action_lamps"/>
        <field name="view_id" ref="view_lamp_form"/>
        <field name="view_mode">form</field>
    </record>
</odoo>""",
    "addons/lighthouse/views/lamp_menus.xml": """<?xml version="1.0"?>
<odoo>
    <menuitem id="lighthouse_root" name="Lighthouse">
        <menuitem id="menu_lamps" action="action_lamps"/>
        <menuitem id="menu_missing" action="nowhere.action_absent"/>
        <menuitem id="menu_group_only"/>
    </menuitem>
</odoo>""",
}


def build(root: Path) -> SourceTree:
    for rel, body in FIXTURES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return SourceTree({**ODOO_SOURCE_POLICY, "release": "fixture-v0"}, root)


def compile_fixture(root: Path):
    return OdooCompiler(build(root), "fixture-attestation").compile()


class PortConformance(unittest.TestCase):
    def test_the_odoo_compiler_satisfies_the_core_source_compiler_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(
                OdooCompiler(build(Path(tmp)), "fixture"), SourceCompiler)


class Extraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.map = compile_fixture(self.root)
        self.tree = build(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _by(self, kind):
        return self.map.by_kind(kind)

    def test_every_relation_carries_a_citation_that_still_resolves(self):
        self.assertTrue(self.map.relations)
        for relation in self.map.relations:
            for reference in relation.evidence:
                self.assertTrue(self.tree.verify(reference),
                                f"{relation.kind}/{relation.subject} cites stale bytes")

    def test_a_menu_without_a_name_takes_the_label_of_the_action_it_opens(self):
        menus = {relation.subject: relation for relation in self._by("menu_link")}
        self.assertIn("Greenhouse / Trays / Seed Trays", menus)
        self.assertEqual(menus["Greenhouse / Trays / Seed Trays"].object,
                         f"{ODOO_SOURCE_SCOPE}/trays")
        self.assertTrue(menus["Greenhouse / Trays / Seed Trays"].attrs["title_resolved"])
        self.assertTrue(menus["Greenhouse / Trays / Cultivars"].attrs["conditional"])

    def test_an_action_without_a_url_path_is_still_addressable(self):
        routes = {relation.attrs["action_id"]: relation.subject
                  for relation in self._by("request_handler")}
        self.assertEqual(routes["greenhouse.action_trays"], f"{ODOO_SOURCE_SCOPE}/trays")
        self.assertEqual(routes["greenhouse.action_cultivars"],
                         f"{ODOO_SOURCE_SCOPE}/action-greenhouse.action_cultivars")

    def test_an_action_carries_the_model_domain_and_url_segment(self):
        trays = next(relation for relation in self._by("request_handler")
                     if relation.attrs["action_id"] == "greenhouse.action_trays")
        self.assertEqual(trays.object, "greenhouse.tray")
        self.assertEqual(trays.attrs["url_path"], "trays")
        self.assertEqual(trays.attrs["domain"], "[('active', '=', True)]")
        self.assertEqual(trays.attrs["event_type"], "view-only")

    def test_a_cross_module_patch_is_reported_not_published_as_a_definition(self):
        self.assertNotIn("other_module.someone_elses_action",
                         {relation.attrs.get("action_id")
                          for relation in self._by("request_handler")})
        reasons = [row["why"] for row in self.map.unsupported]
        self.assertIn("record patches an existing act_window; no res_model declared", reasons)
        self.assertIn("menu opens an action this source tree does not define", reasons)
        self.assertIn("leaf menu declares no action", reasons)

    def test_two_buttons_with_one_label_are_separated_by_their_conditions(self):
        sprout = [relation for relation in self._by("form_target")
                  if relation.attrs.get("label") == "Sprout"]
        self.assertEqual(len(sprout), 2)
        self.assertEqual({relation.object for relation in sprout}, {"action_sprout"})
        self.assertEqual(
            sorted(relation.attrs["invisible"] for relation in sprout),
            ["state != 'draft'", "state != 'ready' or not cultivar_id"])
        bulk = next(relation for relation in sprout
                    if "ready" in relation.attrs["invisible"])
        self.assertEqual(bulk.attrs["context"], "{'default_mode': 'bulk'}")

    def test_a_button_naming_an_action_resolves_it_to_the_action_id(self):
        pick = next(relation for relation in self._by("form_target")
                    if relation.attrs.get("label") == "Pick Cultivar")
        self.assertEqual(pick.object, "greenhouse.action_cultivars")
        self.assertEqual(pick.attrs["target_type"], "action")
        self.assertEqual(pick.attrs["confirm"], "Replace the current cultivar?")

    def test_field_conditions_survive_compilation(self):
        fields = {relation.object: relation.attrs for relation in self._by("form_field")
                  if relation.subject == "greenhouse.view_tray_form"}
        self.assertEqual(fields["name"]["readonly"], "state == 'done'")
        self.assertEqual(fields["cultivar_id"]["domain"], "[('sale_ok', '=', True)]")
        self.assertEqual(fields["cultivar_id"]["required"], "1")
        self.assertEqual(fields["state"]["invisible"], "1")
        listed = {relation.object: relation.attrs for relation in self._by("form_field")
                  if relation.subject == "greenhouse.view_tray_list"}
        self.assertEqual(listed["state"]["column_invisible"], "True")

    def test_an_inheriting_view_is_attributed_to_the_view_family_root(self):
        extra = next(relation for relation in self._by("form_field")
                     if relation.object == "nursery_note")
        self.assertEqual(extra.subject, "greenhouse.view_tray_form_extra")
        self.assertEqual(extra.attrs["root_view"], "greenhouse.view_tray_form")
        primary = next(relation for relation in self._by("form_field")
                       if relation.subject == "greenhouse.view_tray_form"
                       and relation.object == "name")
        self.assertEqual(primary.attrs["root_view"], "greenhouse.view_tray_form")
        family = {relation.object for relation in self._by("form_field")
                  if relation.attrs.get("root_view") == "greenhouse.view_tray_form"}
        self.assertEqual(family, {"name", "cultivar_id", "state", "nursery_note"})

    def test_model_fields_carry_the_selection_a_page_never_shows(self):
        fields = {relation.object: relation.attrs for relation in self._by("service_entity")
                  if relation.subject == "greenhouse.tray"}
        self.assertEqual(fields["sprout_policy"]["field_type"], "Selection")
        self.assertIn("on_delivery", fields["sprout_policy"]["selection"])
        self.assertEqual(fields["sprout_policy"]["compute"], "_compute_sprout_policy")
        self.assertEqual(fields["sprout_policy"]["store"], "True")
        self.assertEqual(fields["cultivar_id"]["comodel_name"], "greenhouse.cultivar")
        self.assertEqual(fields["name"]["string"], "Reference")

    def test_method_signatures_and_their_dependencies_are_recorded(self):
        methods = {relation.subject: relation for relation in self._by("service_parameter")}
        sprout = methods["greenhouse.tray.action_sprout"]
        self.assertEqual(sprout.object, "cultivar_id,note,*extra,**options")
        compute = methods["greenhouse.tray._compute_sprout_policy"]
        self.assertEqual(compute.attrs["decorators"],
                         ["api.depends('state', 'cultivar_id.sale_ok')"])
        self.assertIn("computed-field shape", compute.attrs["doc"])

    def test_an_alternate_view_is_labelled_so_the_primary_stays_unambiguous(self):
        responses = [relation for relation in self._by("request_response")
                     if relation.subject == f"{ODOO_SOURCE_SCOPE}/lamps"]
        self.assertEqual([relation.attrs["response_name"] for relation in responses], ["form"])
        trays = [relation for relation in self._by("request_response")
                 if relation.subject == f"{ODOO_SOURCE_SCOPE}/trays"]
        self.assertEqual([relation.attrs["response_name"] for relation in trays], ["success"])
        self.assertEqual(trays[0].object, "greenhouse.view_tray_list")


class AffordanceInterop(unittest.TestCase):
    """The core's generic affordance resolver must work on this map with no core change."""

    def test_the_core_resolves_candidates_from_an_odoo_source_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_map = compile_fixture(Path(tmp))
            candidates = resolve_source_affordances(
                source_map, task="show me the seed trays",
                served_webapps={ODOO_SOURCE_SCOPE}, route_port=ODOO_ROUTE_PORT, limit=4)
        self.assertTrue(candidates)
        best = candidates[0]
        self.assertEqual(best.title, "Greenhouse / Trays / Seed Trays")
        self.assertEqual(best.route_template, "/odoo/trays")
        self.assertEqual(best.view, "greenhouse.view_tray_list")
        self.assertTrue(best.citations)
        self.assertEqual(
            ODOO_ROUTE_PORT.validate_tool_route(best.route_template), "/odoo/trays")


if __name__ == "__main__":
    unittest.main()
