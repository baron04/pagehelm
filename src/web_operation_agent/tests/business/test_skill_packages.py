"""Installed knowledge cannot expand authority or change a pending write."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from web_operation_agent.skills import SkillArtifact, SkillRegistry, SkillContext
from web_operation_agent.business.runtime import BusinessToolRuntime, BusinessToolResult, BusinessMutationSpec
from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.business.tools.catalog import BusinessSkillCatalog
from web_operation_agent.business.tools.registry import ToolDefinition, ToolRegistry, validate_input
from web_operation_agent.business.tools.contracts import ToolOperation
from web_operation_agent.schemas.approval_details import ApprovalDetail
from web_operation_agent.schemas import ApprovalRequest


def package(body="Read current names; ask before publishing.", operations=("publish_note",)):
    return {"SKILL.md": ("---\nname: team-notes\ndescription: Explain, find and publish team notes.\n---\n" + body).encode(),
            "agents/woa.toml": ("schema_version = 1\nwrite_operations = " + json.dumps(operations)).encode(),
            "references/meaning.md": b"Team means the actor's current team."}


class Handler:
    def __init__(self): self.calls = []
    def prepare(self, args, *, context=None):
        return BusinessMutationSpec(args, "Publish a note", ("Current team",),
                                    details=(ApprovalDetail("body", "正文", args["body"]),),
                                    reconciliation={"kind": "team_note"})
    def dispatch(self, args, *, context=None):
        self.calls.append(args)
        return BusinessToolResult({"published": True}, {"published": True})


class SkillPackages(unittest.TestCase):
    def test_standard_description_selection_covers_catalog_and_never_truncates(self):
        # One decision offers every installed standard package description, so the
        # catalog size itself is the bound. The bound admits a 24-package catalog;
        # a larger one is refused outright -- never silently truncated -- and must
        # supply a retrieval surface instead.
        def catalog(count):
            skills = {f"example.subject-{i}": SimpleNamespace(
                skill_id=f"example.subject-{i}", read_only=False, tool_names=(),
                triggers=(), write_operations=()) for i in range(count)}
            return BusinessSkillCatalog(skills=skills, schemas={}, effects={},
                                        required_scopes={}, route_skill_hints=(),
                                        withheld_skills=())

        for count in (1, 24):
            with self.subTest(count=count):
                selection = catalog(count).select("any task", allow_mutations=True)
                self.assertEqual(len(selection.skills), count)
        with self.assertRaises(ValueError):
            catalog(25).select("any task", allow_mutations=True)

    def test_loaded_card_lists_only_readable_installed_text_attachments(self):
        files = {**package(), "assets/guide.txt": b"A reviewed guide.",
                 "references/image.png": b"synthetic image", "references/invalid.md": b"\xff"}
        skill = SkillArtifact.from_files("contacts", files)
        self.assertEqual(skill.loaded_card()["resources"],
                         ["assets/guide.txt", "references/meaning.md"])
        self.assertEqual(skill.read_resource("assets/guide.txt"), "A reviewed guide.")

    def test_input_shape_errors_name_missing_schema_fields_and_hide_unexpected_keys(self):
        schema = {"type": "object", "properties": {"target": {"type": "string"},
            "changes": {"type": "object", "properties": {}, "additionalProperties": False}},
            "required": ["target", "changes"], "additionalProperties": False}
        with self.assertRaises(BusinessToolRequestError) as raised:
            validate_input(schema, {"private-record-key": "private-field-value"})
        self.assertIn("missing required fields: changes, target", str(raised.exception))
        self.assertIn("unsupported field count: 1", str(raised.exception))
        self.assertNotIn("private-record-key", str(raised.exception))
        self.assertNotIn("private-field-value", str(raised.exception))

    def test_literal_field_contract_rejects_missing_nonstrings_duplicates_and_reference_overlap(self):
        schema = json.dumps({"type": "object", "properties": {"body": {"type": "string"},
            "count": {"type": "integer"}}, "additionalProperties": False})
        for paths in (("unknown",), ("count",), ("body", "body"), ("body",)):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                ToolDefinition("publish_note", "Publish", schema, "write", "contacts.write",
                    ToolOperation("note", "create"), Handler(), literal_inputs=paths,
                    reference_inputs=("body",) if paths == ("body",) else ())

    def test_package_snapshot_and_resource_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "team-notes"
            root.mkdir()
            for name, raw in package().items():
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(raw)
            skill = SkillArtifact.load(root, namespace="contacts")
            context = SkillContext(SkillRegistry((skill,)))
            context.load(skill.skill_id)
            (root / "references/meaning.md").write_text("Changed after approval")
            self.assertEqual(context.read_resource(skill.skill_id, "references/meaning.md"),
                             "Team means the actor's current team.")
            for name in ("../secrets", "/etc/passwd", "references/../SKILL.md", "https://example.org", "assets/tool.py"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    context.read_resource(skill.skill_id, name)
            (root / "references/link.md").symlink_to(root / "SKILL.md")
            with self.assertRaises(ValueError):
                SkillArtifact.load(root, namespace="contacts")

    def test_metadata_is_data_and_does_not_construct_python_or_hide_duplicates(self):
        for front in ("name: team-notes\nname: another\ndescription: x",
                      "name: !!python/object/apply:os.system ['exit 1']\ndescription: x",
                      "name: &a team-notes\ndescription: *a"):
            with self.subTest(front=front), self.assertRaises(ValueError):
                SkillArtifact.from_files("contacts", {"SKILL.md": f"---\n{front}\n---\nbody".encode()})

    def test_loaded_skill_restricts_writes_but_never_installs_a_tool(self):
        handler = Handler()
        tools = ToolRegistry((ToolDefinition("publish_note", "Publish one team note", json.dumps({
            "type": "object", "properties": {"body": {"type": "string", "maxLength": 20000}},
            "required": ["body"], "additionalProperties": False}), "write", "contacts.write",
            ToolOperation("note", "create"), handler),))
        skill = SkillArtifact.from_files("contacts", package())
        runtime = BusinessToolRuntime.from_registry(tools, application_id="contacts",
            skill_registry=SkillRegistry((skill,)), require_skill_for_writes=True)
        with self.assertRaises(BusinessToolRequestError):
            runtime.prepare_mutation("publish_note", {"body": "hello"}, set())
        body = "长正文\n" * 1000
        proposal = runtime.prepare_mutation("publish_note", {"body": body}, {skill.skill_id})
        self.assertEqual(proposal.details[0].text, body)
        self.assertEqual(proposal.skill_digest, skill.artifact_sha256)
        self.assertEqual(handler.calls, [])
        with self.assertRaises(Exception):
            runtime.execute_mutation(proposal, "0" * 64, {skill.skill_id})
        self.assertEqual(handler.calls, [])
        runtime.execute_mutation(proposal, proposal.content_digest, {skill.skill_id})
        self.assertEqual(handler.calls, [{"body": body}])
        with self.assertRaises(Exception):
            runtime.execute_mutation(proposal, proposal.content_digest, {skill.skill_id})
        self.assertEqual(len(handler.calls), 1)
        request = ApprovalRequest(1, "review", "Publish", ("Current team",), 1000,
                                  details=proposal.details)
        self.assertEqual(ApprovalRequest.from_json(request.to_json()).details[0].text, body)

    def test_tool_input_refuses_raw_native_payload(self):
        handler = Handler()
        registry = ToolRegistry((ToolDefinition("publish_note", "Publish", json.dumps({
            "type": "object", "properties": {"body": {"type": "string"}},
            "required": ["body"], "additionalProperties": False}), "write", "contacts.write",
            ToolOperation("note", "create"), handler),))
        with self.assertRaises(BusinessToolRequestError):
            registry.prepare_mutation("publish_note", {"body": "a", "url": "/native"})
        self.assertEqual(handler.calls, [])
