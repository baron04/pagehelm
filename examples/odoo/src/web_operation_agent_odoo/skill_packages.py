"""Business workflows installed independently of the Odoo tool surface."""
from importlib import resources
import hashlib
import json
import re

from web_operation_agent.skills import SkillArtifact, SkillRegistry


def packaged_skills(extra=()):
    root = resources.files("web_operation_agent_odoo").joinpath("skills")
    artifacts = []
    for folder in sorted(root.iterdir(), key=lambda item: item.name):
        if not folder.is_dir():
            continue
        files = {}
        def collect(directory, prefix=""):
            for entry in directory.iterdir():
                name = prefix + entry.name
                if entry.is_dir():
                    collect(entry, name + "/")
                else:
                    files[name] = entry.read_bytes()
        collect(folder)
        artifacts.append(SkillArtifact.from_files("odoo", files))
    # The published extra_skills call site still accepts read-only BusinessSkill
    # objects. Translate their knowledge, never their routes or tool authority.
    # Remove this bridge when that public argument accepts only standard packages.
    for skill in extra:
        if isinstance(skill, SkillArtifact):
            if not skill.read_only:
                raise ValueError("extra Skills must be read-only")
            artifacts.append(skill)
            continue
        if not skill.read_only:
            raise ValueError("extra Skills must be read-only")
        namespace, _, name = skill.skill_id.rpartition(".")
        if (not re.fullmatch(r"[a-z][a-z0-9.-]{2,79}", namespace)
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)):
            namespace = "odoo"
            name = re.sub(r"[^a-z0-9]+", "-", skill.skill_id.lower()).strip("-")[:40].rstrip("-")
            name = (name or "legacy") + "-" + hashlib.sha256(skill.skill_id.encode()).hexdigest()[:12]
        description = skill.intent or skill.title
        body = "\n\n".join(skill.instructions)
        artifacts.append(SkillArtifact.from_files(namespace, {"SKILL.md": (
            f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\n{body}\n").encode()}))
    return SkillRegistry(tuple(artifacts))
