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
from web_operation_agent.planning.affordance import resolve_source_affordances
from web_operation_agent.planning.sourcetree import SourceTree

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
    "addons/greenhouse/models/tray_rules.py": '''from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

TRAY_STAGE = [
    ("seeded", "Seeded"),
    ("sprouting", "Sprouting"),
]
TRAY_STAGE_WITH_HARVEST = TRAY_STAGE + [("harvested", "Harvested")]


class TrayRules(models.Model):
    _inherit = "greenhouse.tray"

    stage = fields.Selection(selection=TRAY_STAGE_WITH_HARVEST, string="Stage")
    pinned = fields.Boolean(help="Pinned trays cannot be moved.")

    def action_move(self):
        if any(tray.pinned for tray in self):
            raise UserError(_("You cannot move a pinned tray. Unpin it first."))
        for tray in self:
            if tray.stage == "harvested":
                raise ValidationError(_(
                    "A harvested tray has nothing left to move."
                    " Archive it instead."))
            elif not tray.cultivar_id:
                raise UserError(_("Pick a cultivar before moving the tray."))
        if not self:
            raise ValueError("nothing to move")
        return True

    def _move_error_message(self):
        """Return why the trays cannot be moved, or False."""
        if self.stage == "seeded":
            return _("Seeded trays stay where they were sown.")
        return False

    def _display_label(self):
        if self.pinned:
            return _("Pinned tray")
        return self.name

    def _compute_sprout_window(self):
        """
        Compute how long a tray may sprout. Possible outcomes:
        - short: the cultivar sprouts within a week, which is the ordinary case for leafy
          greens and most herbs grown on a standard tray.
        - long: the cultivar needs more than a week; this is possible only for a cultivar
          sown on the deep tray, and only while the tray is not pinned.
        """
        for tray in self:
            tray.stage = tray.stage
''',
    "addons/greenhouse/wizard/move_wizard_views.xml": """<?xml version="1.0"?>
<odoo>
    <record id="action_move_trays_wizard" model="ir.actions.act_window">
        <field name="name">Move Trays</field>
        <field name="res_model">greenhouse.move.wizard</field>
        <field name="view_mode">form</field>
        <field name="target">new</field>
    </record>
    <record id="action_trays_to_water" model="ir.actions.act_window">
        <field name="name">Trays to Water</field>
        <field name="res_model">greenhouse.tray</field>
        <field name="view_mode">list,form</field>
        <field name="domain">[('stage', '=', 'sprouting')]</field>
        <field name="help" type="html">
            <p class="o_view_nocontent_smiling_face">
            No tray needs water
            </p><p>
            A tray needs water while it is sprouting &amp; not pinned.
            </p>
        </field>
    </record>
</odoo>""",
    "addons/orchard/models/tree.py": '''from odoo import fields, models


class Tree(models.Model):
    _name = "orchard.tree"

    name = fields.Char("Tree")
''',
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


class Rules(unittest.TestCase):
    """Facts a read-only assistant never triggers and a page therefore never shows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.map = compile_fixture(self.root)
        self.tree = build(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _guards(self, subject):
        return [relation for relation in self.map.by_kind("service_guard")
                if relation.subject == subject]

    def test_a_refusal_carries_its_condition_and_cites_test_through_raise(self):
        pinned = next(relation for relation in self._guards("greenhouse.tray.action_move")
                      if "pinned" in relation.object)
        self.assertEqual(pinned.object, "You cannot move a pinned tray. Unpin it first.")
        self.assertEqual(pinned.attrs["exception"], "UserError")
        self.assertEqual(pinned.attrs["condition"], "any((tray.pinned for tray in self))")
        cited = self.tree.read_text(pinned.evidence[0].path).splitlines()[
            pinned.evidence[0].start_line - 1:pinned.evidence[0].end_line]
        self.assertIn("if any(tray.pinned", cited[0])
        self.assertIn("raise UserError", cited[-1])
        self.assertTrue(self.tree.verify(pinned.evidence[0]))

    def test_joined_messages_loops_and_else_branches_are_kept(self):
        harvested, cultivar = [
            relation for relation in self._guards("greenhouse.tray.action_move")
            if relation.attrs["loop"]]
        self.assertEqual(harvested.object,
                         "A harvested tray has nothing left to move. Archive it instead.")
        self.assertEqual(harvested.attrs["loop"], "tray in self")
        self.assertEqual(harvested.attrs["exception"], "ValidationError")
        self.assertEqual(cultivar.attrs["condition"],
                         "(not (tray.stage == 'harvested')) and (not tray.cultivar_id)")

    def test_a_programming_error_is_not_published_as_a_business_rule(self):
        messages = {relation.object for relation in self._guards(
            "greenhouse.tray.action_move")}
        self.assertNotIn("'nothing to move'", messages)
        self.assertEqual(len(messages), 3)

    def test_an_error_method_return_is_a_rule_but_a_display_label_is_not(self):
        returned = self._guards("greenhouse.tray._move_error_message")
        self.assertEqual([relation.object for relation in returned],
                         ["Seeded trays stay where they were sown."])
        self.assertEqual(returned[0].attrs["form"], "return")
        self.assertEqual(returned[0].attrs["condition"], "self.stage == 'seeded'")
        self.assertEqual(self._guards("greenhouse.tray._display_label"), [])

    def test_a_selection_named_by_a_module_constant_carries_its_values(self):
        stage = next(relation for relation in self.map.by_kind("service_entity")
                     if relation.object == "stage")
        self.assertIn("('harvested', 'Harvested')", stage.attrs["selection"])
        self.assertIn("('sprouting', 'Sprouting')", stage.attrs["selection"])

    def test_a_long_docstring_is_kept_and_covered_by_its_citation(self):
        window = next(relation for relation in self.map.by_kind("service_parameter")
                      if relation.subject == "greenhouse.tray._compute_sprout_window")
        self.assertIn("only while the tray is not pinned", window.attrs["doc"])
        body = "\n".join(self.tree.read_text(window.evidence[0].path).splitlines()[
            window.evidence[0].start_line - 1:window.evidence[0].end_line])
        self.assertIn("only while the tray is not pinned", body)
        self.assertIn("only while the tray is not pinned", window.evidence[0].excerpt,
                      "the answer quotes the stored excerpt, not the ranker's summary")

    def test_a_dialog_action_is_not_a_read_only_affordance(self):
        actions = {relation.attrs["action_id"]: relation
                   for relation in self.map.by_kind("request_handler")}
        self.assertEqual(actions["greenhouse.action_move_trays_wizard"].attrs["event_type"],
                         "dialog")
        self.assertEqual(actions["greenhouse.action_trays_to_water"].attrs["event_type"],
                         "view-only")
        candidates = resolve_source_affordances(
            self.map, task="move trays", served_webapps={ODOO_SOURCE_SCOPE},
            route_port=ODOO_ROUTE_PORT, limit=12)
        self.assertNotIn("action-greenhouse.action_move_trays_wizard",
                         {candidate.request for candidate in candidates})

    def test_action_html_help_is_normalized_to_text(self):
        water = next(relation for relation in self.map.by_kind("request_handler")
                     if relation.attrs["action_id"] == "greenhouse.action_trays_to_water")
        self.assertEqual(water.attrs["help"], "No tray needs water A tray needs water while "
                                              "it is sprouting & not pinned.")
        self.assertIn("A tray needs water while it is sprouting &amp; not pinned",
                      water.evidence[0].excerpt)

    def test_a_deployment_compiles_only_installed_modules(self):
        everything = {relation.attrs.get("module") for relation in self.map.relations}
        self.assertIn("orchard", everything)
        only = OdooCompiler(self.tree, "fixture", modules={"greenhouse"}).compile()
        self.assertEqual({relation.attrs.get("module") for relation in only.relations},
                         {"greenhouse"})
        with self.assertRaises(ValueError):
            OdooCompiler(self.tree, "fixture", modules=set())
        with self.assertRaises(ValueError):
            OdooCompiler(self.tree, "fixture", modules={"../etc"})


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
