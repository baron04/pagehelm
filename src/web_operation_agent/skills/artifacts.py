"""Standard SKILL.md packages snapshotted at an explicitly installed root.

Resources are read once at installation. Neither a turn nor an approval can follow a
changed file, symlink, URL, script, or instruction to a different authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

import yaml

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_OPERATION = re.compile(r"[a-z][a-z0-9_]{1,63}")
_TEXT = frozenset({".md", ".txt", ".csv", ".json", ".toml", ".yaml", ".yml"})
_ASSET = frozenset({".png", ".jpg", ".jpeg", ".webp", ".svg"})
MAX_FILE_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 512 * 1024
MAX_FILES = 64


class _UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("Skill metadata keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _relative(path: str) -> str:
    if (not isinstance(path, str) or not path or len(path) > 240
            or "\\" in path or ":" in path or "%" in path
            or re.search(r"[\x00-\x20\x7f]", path)):
        raise ValueError("Skill resource must be a safe relative path")
    parts = PurePosixPath(path).parts
    if path.startswith("/") or any(p.startswith(".") for p in parts):
        raise ValueError("Skill resource escapes its package")
    if str(PurePosixPath(path)) != path:
        raise ValueError("Skill resource path must be canonical")
    return path


@dataclass(frozen=True, slots=True)
class SkillArtifact:
    namespace: str
    name: str
    description: str
    body: str
    write_operations: tuple[str, ...]
    artifact_sha256: str
    _files: tuple[tuple[str, bytes], ...]
    diagnostics: tuple[str, ...] = ()
    scope_entities: tuple[str, ...] = ()
    scope_pages: tuple[str, ...] = ()

    @classmethod
    def from_files(cls, namespace: str, files: dict[str, bytes]) -> "SkillArtifact":
        if not re.fullmatch(r"[a-z][a-z0-9.-]{2,79}", namespace):
            raise ValueError("Skill namespace must identify an application")
        if not 1 <= len(files) <= MAX_FILES or "SKILL.md" not in files:
            raise ValueError("Skill package requires SKILL.md and at most 64 files")
        total = 0
        for path, raw in files.items():
            _relative(path)
            if (not isinstance(raw, bytes) or len(raw) > MAX_FILE_BYTES
                    or PurePosixPath(path).suffix.lower() not in _TEXT | _ASSET
                    or path != "SKILL.md" and path != "agents/woa.toml"
                    and PurePosixPath(path).parts[0] not in {"references", "assets"}):
                raise ValueError("unsupported or oversized Skill resource")
            total += len(raw)
        if total > MAX_PACKAGE_BYTES:
            raise ValueError("Skill package exceeds 512 KiB")
        try:
            text = files["SKILL.md"].decode("utf-8")
            match = re.fullmatch(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", text, re.S)
            if not match:
                raise ValueError("SKILL.md requires YAML frontmatter and Markdown")
            front, body = match.groups()
            if len(front.encode()) > 8192:
                raise ValueError("Skill metadata exceeds 8 KiB")
            tokens = list(yaml.scan(front, Loader=_UniqueSafeLoader))
            if any(isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken,
                                  yaml.tokens.TagToken)) for t in tokens):
                raise ValueError("Skill metadata aliases, anchors and tags are unsupported")
            metadata = yaml.load(front, Loader=_UniqueSafeLoader)
        except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
            raise ValueError("invalid safe Skill metadata") from exc
        if not isinstance(metadata, dict):
            raise ValueError("Skill metadata must be an object")
        name, description = metadata.get("name"), metadata.get("description")
        if not isinstance(name, str) or len(name) > 64 or not _NAME.fullmatch(name):
            raise ValueError("Skill name must be 1-64 lowercase letters, digits and hyphens")
        if (not isinstance(description, str) or not description.strip()
                or len(description) > 1024 or not body.strip()):
            raise ValueError("Skill requires a 1-1024 character description and a body")
        if set(metadata) - {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}:
            raise ValueError("unsupported Skill frontmatter field")
        sidecar = tomllib.loads(files.get("agents/woa.toml", b"").decode("utf-8"))
        if sidecar and (set(sidecar) - {"schema_version", "write_operations", "scope_entities", "scope_pages"}
                        or type(sidecar.get("schema_version")) is not int
                        or sidecar.get("schema_version") != 1):
            raise ValueError("unsupported Skill host policy")
        operations = sidecar.get("write_operations", [])
        if (not isinstance(operations, list) or len(operations) > 64
                or any(not isinstance(op, str) or not _OPERATION.fullmatch(op) for op in operations)
                or len(set(operations)) != len(operations)):
            raise ValueError("Skill write_operations must be unique operation identifiers")
        scope = {}
        for key in ("scope_entities", "scope_pages"):
            values = sidecar.get(key, [])
            if (not isinstance(values, list) or len(values) > 80
                    or any(not isinstance(value, str) or not 0 < len(value) <= 200
                           or re.search(r"[\x00-\x20\x7f]", value) for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError("Skill scopes must be bounded unique application identities")
            scope[key] = tuple(values)
        manifest = [(p, hashlib.sha256(raw).hexdigest()) for p, raw in sorted(files.items())]
        digest = hashlib.sha256(json.dumps([namespace, manifest], separators=(",", ":")).encode()).hexdigest()
        diagnostics = (("allowed-tools is descriptive; host scopes and fresh approval still apply",)
                       if "allowed-tools" in metadata else ())
        return cls(namespace, name, description.strip(), body, tuple(operations), digest,
                   tuple(sorted(files.items())), diagnostics, **scope)

    @classmethod
    def load(cls, root: Path, *, namespace: str) -> "SkillArtifact":
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Skill root must be an installed directory")
        files = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Skill packages cannot contain symlinks")
            if path.is_file():
                if path.stat().st_size > MAX_FILE_BYTES or len(files) >= MAX_FILES:
                    raise ValueError("Skill resource limit exceeded")
                files[path.relative_to(root).as_posix()] = path.read_bytes()
        return cls.from_files(namespace, files)

    def read_resource(self, path: str) -> str:
        path = _relative(path)
        if not path.startswith(("references/", "assets/")) or PurePosixPath(path).suffix not in _TEXT:
            raise ValueError("only installed text resources may enter model context")
        try:
            return dict(self._files)[path].decode("utf-8")
        except (KeyError, UnicodeError) as exc:
            raise ValueError("Skill text resource is unavailable") from exc

    @property
    def skill_id(self) -> str:
        return f"{self.namespace}.{self.name}"

    # Presentation bridge for the existing step host. These carry knowledge and a
    # proposed-write whitelist, never tool registration or route authority.
    @property
    def title(self): return self.name
    @property
    def intent(self): return self.description
    @property
    def instructions(self): return tuple(self.body[i:i+2000] for i in range(0, len(self.body), 2000))
    @property
    def tool_names(self): return self.write_operations
    @property
    def read_only(self): return not self.write_operations
    @property
    def triggers(self): return ()
    @property
    def provenance(self): return ()
    @property
    def routes(self): return ()

    def candidate_card(self, *, loaded=False):
        return {"skill_id": self.skill_id, "name": self.name, "description": self.description,
                "write_operations": list(self.write_operations), "loaded": loaded}

    @property
    def text_resources(self) -> tuple[str, ...]:
        available = []
        for path, _ in self._files:
            try:
                self.read_resource(path)
            except ValueError:
                continue
            available.append(path)
        return tuple(available)

    def loaded_card(self):
        return {**self.candidate_card(loaded=True), "instructions": list(self.instructions),
                "resources": list(self.text_resources),
                "artifact_sha256": self.artifact_sha256, "diagnostics": list(self.diagnostics)}


class SkillRegistry:
    def __init__(self, artifacts: tuple[SkillArtifact, ...] = ()):
        skills = {s.skill_id: s for s in artifacts}
        if len(skills) != len(artifacts):
            raise ValueError("duplicate installed Skill identity")
        self._skills = MappingProxyType(skills)

    @classmethod
    def discover(cls, root: Path, *, namespace: str):
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Skill installation root must be a directory")
        artifacts = []
        for child in sorted(root.iterdir()):
            if child.is_symlink() or not child.is_dir():
                raise ValueError("Skill installation contains an unsupported entry")
            artifact = SkillArtifact.load(child, namespace=namespace)
            if child.name != artifact.name:
                raise ValueError("Skill directory must match its standard name")
            artifacts.append(artifact)
        return cls(tuple(artifacts))

    @property
    def artifacts(self): return tuple(self._skills.values())

    def get(self, skill_id: str) -> SkillArtifact:
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise ValueError("Skill is not installed") from exc

    def catalog(self) -> tuple[dict, ...]:
        return tuple(s.candidate_card() for s in self._skills.values())


class SkillContext:
    """Per-turn loaded snapshots and explicit support for each proposed operation."""
    def __init__(self, registry: SkillRegistry):
        self.registry = registry
        self._loaded: dict[str, SkillArtifact] = {}

    def load(self, skill_id: str) -> SkillArtifact:
        artifact = self.registry.get(skill_id)
        self._loaded[skill_id] = artifact
        return artifact

    @property
    def loaded(self): return tuple(self._loaded.values())

    def get_loaded(self, skill_id):
        if skill_id not in self._loaded:
            raise ValueError("Skill is not loaded in this turn")
        return self._loaded[skill_id]

    def support(self, skill_id: str, operation: str) -> str:
        skill = self._loaded.get(skill_id)
        if skill is None or operation not in skill.write_operations:
            raise ValueError("operation requires a loaded supporting Skill")
        return skill.artifact_sha256

    def read_resource(self, skill_id: str, path: str) -> str:
        if skill_id not in self._loaded:
            raise ValueError("load the Skill before reading its resources")
        return self._loaded[skill_id].read_resource(path)
