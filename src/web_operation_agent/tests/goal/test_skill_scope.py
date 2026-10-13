"""Skill knowledge selection uses identities, independently of write enablement."""
import unittest
from dataclasses import replace
from types import SimpleNamespace

from web_operation_agent.goal.skill_execution import skills_for_scope
from web_operation_agent.skills import SkillArtifact, SkillRegistry
from web_operation_agent.business.tools.registry import ToolRegistry
from web_operation_agent.tests.goal import test_skill_declaration as declaration_fixture
from testapps.contacts.adapter import _ContactsToolExecutor
from testapps.contacts.packages import contact_operations


class SkillScopeTests(unittest.TestCase):
    def knowledge(self):
        return SkillArtifact.from_files("fixture", {
            "SKILL.md": b"---\nname: query-guide\ndescription: Query records and navigate their pages.\n---\nRead live facts.\n",
            "agents/woa.toml": b'schema_version = 1\nscope_entities = ["records"]\nscope_pages = ["help"]\n'})

    def test_queries_and_navigation_select_knowledge_without_any_tools(self):
        skill = self.knowledge()
        operations = SimpleNamespace(skills=SkillRegistry((skill,)), tools=ToolRegistry(()))
        for part in ({"kind": "fact", "entity": "records"},
                     {"kind": "page", "page": "help"}):
            with self.subTest(part=part):
                self.assertEqual(skills_for_scope(operations, [part]), (skill.skill_id,))
        self.assertEqual(skill.write_operations, ())
        self.assertEqual(skills_for_scope(operations, [{"entity": "other"}]), ())

    def test_operation_target_scopes_do_not_replace_its_changed_entity(self):
        context, definition = declaration_fixture.SkillDeclarationTests().creation_context()
        definition = replace(definition, scope_entities=("teams",),
                             input_fields=(("name", "contacts", "name"),))
        operations = SimpleNamespace(skills=context.registry, tools=ToolRegistry((definition,)))
        for entity in ("teams", "contacts"):
            with self.subTest(entity=entity):
                self.assertEqual(skills_for_scope(operations, [{"entity": entity}]),
                                 (context.loaded[0].skill_id,))

    def test_contacts_rest_package_selects_with_writes_enabled_or_disabled(self):
        for enabled in (False, True):
            executor = _ContactsToolExecutor("http://127.0.0.1:1", api_token="synthetic-test-token",
                                             allow_mutations=enabled)
            operations = contact_operations(executor, enabled)
            with self.subTest(enabled=enabled):
                self.assertEqual(skills_for_scope(operations, [{"entity": "contacts"}]),
                                 ("minimal.contacts.contact-maintenance",))
                self.assertEqual(bool(operations.tools.definitions), enabled)

    def test_invalid_host_scope_is_rejected_at_installation(self):
        for body in ('scope_entities = ["records", "records"]',
                     'scope_pages = [42]', 'scope_entities = "records"',
                     'scope_entities = ["private\\nvalue"]', 'unknown = []'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                SkillArtifact.from_files("fixture", {
                    "SKILL.md": b"---\nname: query-guide\ndescription: Query.\n---\nRead.\n",
                    "agents/woa.toml": ('schema_version = 1\n'+body).encode()})
