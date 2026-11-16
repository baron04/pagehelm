"""Packaged task knowledge, loaded independently of native-write enablement.

Each package is one independently judgeable business goal. Reference material that
several goals genuinely share is maintained once under ``skill_references`` and
assembled here into each package's controlled ``references``; packages never keep
their own copies, and completion conditions always stay in the body text.
"""
from importlib import resources
from web_operation_agent.skills import SkillArtifact, SkillRegistry

#: Reviewed shared reference material, keyed by the file every listed package
#: reuses. Assembled read-only; bodies must not depend on a read to know how the
#: goal finishes.
_SHARED_REFERENCES = {
    "disambiguation.md": (
        "crm-contact-creation", "crm-customer-update", "crm-contact-update",
        "crm-customer-pool-release", "crm-customer-receive", "crm-customer-lock",
        "crm-customer-deal-status", "crm-clue-maintenance", "crm-follow-up",
        "crm-receivable-planning", "erp-supplier-maintenance",
        "erp-warehouse-maintenance", "erp-customer-maintenance",
        "erp-product-maintenance", "product-brand-maintenance"),
    "hierarchy-paths.md": (
        "crm-customer-creation", "crm-contact-creation", "crm-customer-update",
        "crm-contact-update", "crm-clue-maintenance", "erp-product-maintenance",
        "system-dept-creation", "product-category-creation"),
    "erp-units.md": (
        "erp-supplier-maintenance", "erp-warehouse-maintenance",
        "erp-customer-maintenance", "erp-product-maintenance"),
    "crm-erp-identity.md": (
        "crm-customer-creation", "crm-contact-creation", "crm-customer-update",
        "crm-contact-update", "crm-clue-maintenance", "erp-customer-maintenance",
        "erp-supplier-maintenance"),
    "mall-erp-identity.md": (
        "erp-product-maintenance", "product-brand-maintenance",
        "product-category-creation"),
    "notice-announcement-identity.md": (
        "system-notice-maintenance", "oa-announcement-publication"),
    "org-records-identity.md": (
        "crm-contact-creation", "crm-contact-update", "system-post-creation",
        "system-dept-creation"),
    "member-definition-identity.md": (
        "member-tag-creation", "member-group-creation"),
}


def packaged_skills():
    root = resources.files("web_operation_agent_yudao").joinpath("skills")
    shared_root = resources.files("web_operation_agent_yudao").joinpath("skill_references")
    shared = {name: shared_root.joinpath(name).read_bytes() for name in _SHARED_REFERENCES}
    injections = {}
    for name, packages in _SHARED_REFERENCES.items():
        for package in packages:
            injections.setdefault(package, []).append(f"references/{name}")
    artifacts = []
    for package in sorted(root.iterdir(), key=lambda p: p.name):
        if not package.is_dir(): continue
        files = {}
        def collect(directory, prefix=""):
            for entry in directory.iterdir():
                relative = prefix + entry.name
                if entry.is_dir(): collect(entry, relative + "/")
                else: files[relative] = entry.read_bytes()
        collect(package)
        for path in sorted(injections.get(package.name, ())):
            if path in files:
                raise ValueError("shared reference collides with a package file")
            files[path] = shared[path.split("/", 1)[1]]
        artifacts.append(SkillArtifact.from_files("yudao", files))
    return SkillRegistry(tuple(artifacts))


def domain_playbooks() -> dict[str, str]:
    """Read-only domain playbooks available across read, navigation and write turns."""
    root = resources.files("web_operation_agent_yudao").joinpath("domain_playbooks")
    try:
        if not root.is_dir():
            return {}
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return {}
    out = {}
    for entry in entries:
        if entry.is_file() and entry.name.endswith(".md"):
            out[entry.name.removesuffix(".md")] = entry.read_text(encoding="utf-8")
    return out
