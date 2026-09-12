#!/usr/bin/env python3
"""Lock the dependency closure, publish an SBOM, and refuse to let the notice drift from it.

`THIRD_PARTY.md` has been careful prose for a long time, and prose was exactly the problem:
it said the product was qualified against PageController 1.12.2 while every environment
served 1.12.4, and its own release gate said "these are resolver ranges, not a release lock"
with nothing to make that stop being true. This script is what turns both into facts.

Two halves, deliberately split by whether they need the network:

* **`--write` resolves and records.** It asks pip to resolve each distribution's runtime
  closure and writes `requirements.lock` -- exact versions and sha256 hashes, installable
  with `--require-hashes` -- plus `sbom.json` over the Python and npm components together.
  It reaches the index, so it is a deliberate release step, not a gate.
* **`--check` reads what is committed.** It re-derives every cross-reference offline: lock
  against SBOM, SBOM against the npm lockfiles, and both against `THIRD_PARTY.md`. That is
  the half CI runs, and it is the half that catches a notice nobody updated.

No new dependency. pip is already required to install this project, npm lockfiles are already
committed, and the rest is stdlib -- the same reason `check_core_boundary.py` and
`check_wheel_contents.py` are hand-written here rather than pulled in.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "requirements.lock"
SBOM_FILE = ROOT / "sbom.json"
NOTICE_FILE = ROOT / "THIRD_PARTY.md"
#: Every distribution whose runtime closure ships. The examples declare the core as their
#: only dependency, so resolving them separately is what proves that is still true.
DISTRIBUTIONS = ("", "examples/ofbiz", "examples/twenty", "examples/odoo")
#: npm lockfiles whose components are part of the release inventory even though no wheel
#: carries them: one builds the evaluation baseline, one builds the assistant panel.
NPM_LOCKS = {
    "evals/pageagent": "evaluation baseline (not shipped in any wheel)",
    "frontend": "assistant panel bundle (built into the core wheel)",
}
SBOM_VERSION = 1


def _licence_of(metadata: dict) -> tuple[str, str]:
    """What the package says about its own licence, and where it said it.

    Recorded with its source because the two are not equally strong: a PEP 639 expression is
    a declaration in a standard field, a classifier is a category someone ticked. Neither is
    a legal opinion, and an inventory that flattened them would imply one.
    """
    expression = metadata.get("license_expression")
    if isinstance(expression, str) and expression.strip():
        return expression.strip(), "license_expression"
    for classifier in metadata.get("classifier") or []:
        if classifier.startswith("License :: OSI Approved :: "):
            return classifier.split(" :: ")[-1].removesuffix(" License"), "classifier"
    declared = metadata.get("license")
    if isinstance(declared, str) and declared.strip() and len(declared) < 120:
        return declared.strip(), "license"
    return "", "undeclared"


#: This project's own distributions. They are the thing being inventoried, not third-party
#: components, and none of them is published, so they never appear in the lock.
OWN_PREFIX = "web-operation-agent"


def _declared(target: Path) -> set[str]:
    """The names a distribution's own `pyproject.toml` asks for, however they are pinned."""
    text = (target / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
    if not block:
        return set()
    return {match.casefold() for match in
            re.findall(r"[\"']\s*([A-Za-z0-9][A-Za-z0-9._-]*)", block.group(1))}


def resolve_python(project: str, *, find_links: Path | None = None) -> list[dict]:
    """One distribution's resolved runtime closure, with the hash of every artifact.

    The example distributions require the core, which is unpublished, so resolving them needs
    it on a local `--find-links` path. That is also what makes the result meaningful: if an
    example ever grows a dependency of its own, a new third-party name appears here.
    """
    target = ROOT / project if project else ROOT
    report = ROOT / ".sbom-report.json"
    links = ["--find-links", str(find_links)] if find_links else []
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet",
             "--ignore-installed", *links, "--report", str(report), str(target)],
            check=True, capture_output=True, timeout=900)
        payload = json.loads(report.read_text())
    finally:
        report.unlink(missing_ok=True)
    components = []
    for item in payload.get("install", []):
        metadata = item.get("metadata") or {}
        if metadata["name"].casefold().startswith(OWN_PREFIX):
            continue
        archive = (item.get("download_info") or {}).get("archive_info") or {}
        licence, source = _licence_of(metadata)
        components.append({
            "ecosystem": "pypi",
            "name": metadata["name"],
            "version": metadata["version"],
            "sha256": (archive.get("hashes") or {}).get("sha256", ""),
            "license": licence,
            "license_source": source,
            # pip marks the project itself as "requested" and everything it pulls as not,
            # so that flag called the one dependency this repository declares transitive --
            # and the notice rule below, which exempts transitive packages, then constrained
            # nothing on the Python side at all. What "direct" has to mean here is what the
            # distribution's own metadata asks for.
            "direct": metadata["name"].casefold() in _declared(target),
            # Every resolved Python artifact is installed alongside the wheel.
            "shipped": True,
            "required_by": project or "web-operation-agent",
        })
    return components


def read_npm(directory: str) -> list[dict]:
    """Components from a committed npm lockfile, exactly as locked.

    `direct` is read from the root package's own declared dependencies, not from where npm
    happened to put a package on disk. npm hoists transitives to the top level, so treating
    "installed at depth one" as "we chose this" would demand a written notice for every
    package a dependency dragged in -- which is not what a direct dependency is.
    """
    lock = json.loads((ROOT / directory / "package-lock.json").read_text())
    packages = lock.get("packages") or {}
    root = packages.get("", {})
    shipped = set(root.get("dependencies") or {})
    tooling = set(root.get("devDependencies") or {})
    declared = shipped | tooling
    components = []
    for path, entry in sorted(packages.items()):
        if not path.startswith("node_modules/") or not entry.get("version"):
            continue
        name = path.split("node_modules/", 1)[1]
        components.append({
            "ecosystem": "npm",
            "name": name,
            "version": entry["version"],
            "integrity": entry.get("integrity", ""),
            "license": entry.get("license", ""),
            "license_source": "package-lock" if entry.get("license") else "undeclared",
            "direct": name in declared and path.count("node_modules/") == 1,
            # Whether this package's own bytes travel to a user. A build tool is inventoried
            # like everything else and does not need its own paragraph of prose; a library
            # compiled into a shipped bundle does.
            "shipped": name in shipped and path.count("node_modules/") == 1,
            "required_by": directory,
        })
    return components


def _local_index() -> Path:
    """Build the core wheel so the examples can resolve against it instead of PyPI."""
    staging = ROOT / ".sbom-index"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet",
                    "--wheel-dir", str(staging), str(ROOT)],
                   check=True, capture_output=True, timeout=900)
    return staging


#: The interpreters and platforms this lock has to install on. A lock resolved only where
#: it was generated pins whatever wheels that machine happened to want: this one pinned
#: `macosx_11_0_arm64`, so `--require-hashes` refused to install on Linux with no
#: explanation, and "a reviewer can run it" was false for every reviewer not on a Mac.
#: pip picks the artifact matching the machine it runs on, so a requirement carrying every
#: target's hash is portable without being ambiguous.
TARGETS = (
    # The machine this is generated on contributes its own resolution.
    (),
    # glibc x86_64. Two tag families because the projects here do not agree on one:
    # playwright still publishes `manylinux1`, greenlet publishes `manylinux_2_28`.
    ("--platform", "manylinux1_x86_64", "--platform", "manylinux2014_x86_64",
     "--platform", "manylinux_2_28_x86_64"),
    ("--platform", "manylinux2014_aarch64", "--platform", "manylinux_2_28_aarch64"),
)


def resolve_for_targets(project: str, *, find_links: Path | None = None) -> list[dict]:
    """Resolve on this machine, then collect the same versions' hashes for every target.

    Versions come from the local resolution; the extra passes only add the artifacts other
    platforms would install. A target that cannot supply one of those versions is an error
    worth stopping for -- a lock that silently omits a platform is the state this replaces.
    """
    components = resolve_python(project, find_links=find_links)
    if not components:
        return components
    pinned = [f"{item['name']}=={item['version']}" for item in components]
    for flags in TARGETS[1:]:
        for name, digest in _hashes_for(pinned, flags, find_links=find_links).items():
            for item in components:
                if item["name"].casefold() == name:
                    item.setdefault("extra_sha256", [])
                    if digest not in item["extra_sha256"] and digest != item["sha256"]:
                        item["extra_sha256"].append(digest)
    return components


def _hashes_for(pinned: list[str], flags: tuple, *, find_links: Path | None) -> dict:
    """Download each pinned version for one target and hash what came back."""
    links = ["--find-links", str(find_links)] if find_links else []
    with tempfile.TemporaryDirectory() as directory:
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--no-deps",
                 "--quiet", *flags, "--python-version", "3.12", "--implementation", "cp",
                 "--abi", "cp312", *links, "-d", directory, *pinned],
                check=True, capture_output=True, timeout=900)
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                "a locked version has no artifact for " + " ".join(flags) + ":\n"
                + (exc.stderr or b"").decode()[-600:]) from None
        found = {}
        for path in sorted(Path(directory).glob("*")):
            found[path.name.split("-", 1)[0].replace("_", "-").casefold()] = \
                hashlib.sha256(path.read_bytes()).hexdigest()
        return found


def build_inventory() -> dict:
    python: dict[tuple[str, str], dict] = {}
    index = _local_index()
    for project in DISTRIBUTIONS:
        for component in resolve_for_targets(
                project, find_links=index if project else None):
            key = (component["name"].casefold(), component["version"])
            # One component, every distribution that pulls it: a package required by three
            # of them is one entry that says so, not three entries that look independent.
            if key in python:
                existing = python[key]["required_by"].split(", ")
                if component["required_by"] not in existing:
                    python[key]["required_by"] = ", ".join(
                        sorted(existing + [component["required_by"]]))
                python[key]["direct"] = python[key]["direct"] or component["direct"]
                continue
            python[key] = component
    shutil.rmtree(index, ignore_errors=True)
    npm = [row for directory in NPM_LOCKS for row in read_npm(directory)]
    return {"python": sorted(python.values(), key=lambda row: row["name"].casefold()),
            "npm": sorted(npm, key=lambda row: (row["required_by"], row["name"]))}


def render_lock(inventory: dict) -> str:
    """A pip-installable lock. Hashes make `--require-hashes` possible, which is the point."""
    lines = [
        "# Resolved runtime closure of every distribution in this repository.",
        "#",
        "# Generated by scripts/lock_and_sbom.py --write, and checked against sbom.json and",
        "# THIRD_PARTY.md by --check. Install it with:",
        "#",
        "#     python -m pip install --require-hashes -r requirements.lock",
        "#",
        "# Each requirement carries an artifact hash for every platform this is generated",
        "# for -- this machine, glibc x86_64 and glibc aarch64, all CPython 3.12. pip picks",
        "# the one matching the machine it runs on. A lock resolved only where it was",
        "# written pins that machine's wheels and refuses to install anywhere else, with no",
        "# message that says why.",
        "#",
        "# The example distributions depend on the core and on nothing else; if that ever",
        "# stops being true, a new name appears here.",
        "",
    ]
    for component in inventory["python"]:
        if not component["sha256"]:
            raise SystemExit(
                f"{component['name']} resolved without an artifact hash; a lock without one "
                "cannot be verified on install")
        lines.append(f"# {component['license'] or 'licence undeclared'} "
                     f"(required by {component['required_by']})")
        digests = [component["sha256"], *component.get("extra_sha256", [])]
        lines.append(f"{component['name']}=={component['version']} \\")
        # One requirement, every target's artifact. pip installs the one matching the
        # machine it runs on and refuses anything whose hash is not among these.
        lines.extend(f"    --hash=sha256:{digest} \\" for digest in digests[:-1])
        lines.append(f"    --hash=sha256:{digests[-1]}")
    return "\n".join(lines) + "\n"


def render_sbom(inventory: dict) -> dict:
    components = []
    for component in inventory["python"] + inventory["npm"]:
        entry = {
            "type": "library",
            "name": component["name"],
            "version": component["version"],
            "purl": (f"pkg:pypi/{component['name'].casefold()}@{component['version']}"
                     if component["ecosystem"] == "pypi"
                     else f"pkg:npm/{component['name']}@{component['version']}"),
            "licenses": ([{"expression": component["license"]}] if component["license"]
                         else []),
            "properties": [
                {"name": "woa:licenseSource", "value": component["license_source"]},
                {"name": "woa:requiredBy", "value": component["required_by"]},
                {"name": "woa:direct", "value": str(component["direct"]).lower()},
                {"name": "woa:shipped", "value": str(component["shipped"]).lower()},
            ],
        }
        digest = component.get("sha256") or component.get("integrity")
        if digest:
            entry["hashes"] = [{
                "alg": "SHA-256" if component.get("sha256") else "npm-integrity",
                "content": digest}]
        components.append(entry)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": SBOM_VERSION,
        "metadata": {
            "component": {"type": "application", "name": "web-operation-agent"},
            "properties": [
                {"name": "woa:note",
                 "value": ("Resolved runtime closure plus the npm components the evaluation "
                           "baseline and the assistant panel are built from. Browsers "
                           "installed by `playwright install`, container images and the "
                           "local fixture applications are separate artifacts and are "
                           "inventoried in THIRD_PARTY.md, not here.")},
            ],
        },
        "components": components,
    }


# ----------------------------------------------------------------------------- checking

def _notice_names() -> set[str]:
    """Every component name `THIRD_PARTY.md` mentions, however it spells it.

    Both spellings the document actually uses: a backticked package name and a markdown link
    whose text is the project's name. Requiring one form would make the notice worse to read
    in order to make this function simpler, which is the wrong trade for a document whose
    whole job is to be read.
    """
    text = NOTICE_FILE.read_text()
    backticked = re.findall(r"`([A-Za-z0-9@/._-]{2,})`", text)
    linked = re.findall(r"\[([A-Za-z0-9@/._ -]{2,}?)\]\(", text)
    return {name.strip().casefold() for name in backticked + linked}


def _locked_requirements(text: str) -> dict:
    """Every requirement in the lock, with the set of artifact hashes it accepts.

    A requirement may carry more than one: the lock is resolved for several platforms, and
    pip installs whichever artifact matches the machine and refuses anything else.
    """
    found: dict[tuple[str, str], set[str]] = {}
    current: tuple[str, str] | None = None
    for line in text.splitlines():
        head = re.fullmatch(r"([A-Za-z0-9._-]+)==(\S+?) *\\?", line)
        if head:
            current = (head.group(1).casefold(), head.group(2))
            found.setdefault(current, set())
            continue
        digest = re.fullmatch(r" +--hash=sha256:([0-9a-f]{64}) *\\?", line)
        if digest and current:
            found[current].add(digest.group(1))
        elif line.strip() and not line.startswith("#"):
            current = None
    return found


def check_committed() -> list[str]:
    """Re-derive every cross-reference from what is committed. No network, no index."""
    problems: list[str] = []
    if not LOCK_FILE.is_file() or not SBOM_FILE.is_file():
        return [f"{LOCK_FILE.name} and {SBOM_FILE.name} must both be committed; "
                "run scripts/lock_and_sbom.py --write"]
    sbom = json.loads(SBOM_FILE.read_text())
    # Keyed by name *and* version: one name can legitimately appear twice at different
    # versions, because the evaluation baseline and the panel are built from separate
    # lockfiles. A name-only index silently dropped one of each such pair.
    inventoried: set[tuple[str, str]] = {
        (component["name"].casefold(), component["version"])
        for component in sbom.get("components", [])}
    versions: dict[str, list[str]] = {}
    for component in sbom.get("components", []):
        versions.setdefault(component["name"].casefold(), []).append(component["version"])

    # 1. The lock and the SBOM describe the same Python artifacts, at the same digests.
    #    Names and versions agreeing was all this used to check, so a hash could be edited
    #    to anything at all and still pass: pip would have caught it on install, but the
    #    gate whose job is the dependency inventory would not have.
    locked = _locked_requirements(LOCK_FILE.read_text())
    recorded = {
        (component["name"].casefold(), component["version"]): entry["content"]
        for component in sbom.get("components", [])
        for entry in component.get("hashes", []) if entry.get("alg") == "SHA-256"}
    for (name, version), digests in sorted(locked.items()):
        if not digests:
            problems.append(
                f"{name}=={version} carries no sha256 hash, so --require-hashes cannot "
                "verify what it installs")
        if (name, version) not in inventoried:
            problems.append(
                f"{name}=={version} is locked and {SBOM_FILE.name} inventories "
                f"{versions.get(name) or 'nothing by that name'}")
            continue
        digest = recorded.get((name, version))
        if digest is None:
            problems.append(f"{SBOM_FILE.name} records no sha256 for {name}=={version}")
        elif digest not in digests:
            problems.append(
                f"{name}=={version} is locked at {', '.join(sorted(d[:12] for d in digests))}"
                f" and {SBOM_FILE.name} records {digest[:12]}")

    # 2. The npm components are exactly what the committed lockfiles say.
    for directory in NPM_LOCKS:
        for component in read_npm(directory):
            if (component["name"].casefold(), component["version"]) not in inventoried:
                problems.append(
                    f"{component['name']}@{component['version']} is in "
                    f"{directory}/package-lock.json and not in {SBOM_FILE.name}")

    # 3. Anything a person has to reason about legally is named in the notice. Transitive
    #    packages are inventoried but not each prose-described, which is what the notice
    #    itself says it does; direct ones have no such excuse.
    named = _notice_names()
    for component in sbom.get("components", []):
        properties = {prop["name"]: prop["value"] for prop in component.get("properties", [])}
        if properties.get("woa:direct") != "true":
            continue
        # A build tool is inventoried and covered by the notice's own blanket paragraph.
        # A library whose bytes reach a user is a redistribution, and redistribution is what
        # a licence has terms about, so that one is named.
        if properties.get("woa:shipped") != "true":
            continue
        if component["name"].casefold() not in named:
            problems.append(
                f"{component['name']} ships to users and {NOTICE_FILE.name} "
                "does not mention it")

    # 4. The pinned browser asset agrees with the package that states it.
    pinned = json.loads(
        (ROOT / "src/web_operation_agent/policies/page_controller.json").read_text())
    if (pinned["package"].casefold(), pinned["version"]) not in inventoried:
        problems.append(
            f"{pinned['package']} is pinned at {pinned['version']} and "
            f"{SBOM_FILE.name} inventories "
            f"{versions.get(pinned['package'].casefold()) or 'nothing by that name'}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true",
                        help="resolve from the index and rewrite the lock and the SBOM")
    parser.add_argument("--check", action="store_true",
                        help="verify what is committed, offline; this is the gate")
    arguments = parser.parse_args(argv)
    if not (arguments.write or arguments.check):
        parser.error("choose --write or --check")

    if arguments.write:
        inventory = build_inventory()
        LOCK_FILE.write_text(render_lock(inventory))
        SBOM_FILE.write_text(
            json.dumps(render_sbom(inventory), indent=2, sort_keys=True) + "\n")
        print(f"wrote {LOCK_FILE.name} ({len(inventory['python'])} Python artifacts) "
              f"and {SBOM_FILE.name} "
              f"({len(inventory['python']) + len(inventory['npm'])} components)")

    if arguments.check:
        problems = check_committed()
        for problem in problems:
            print(f"FAIL: {problem}")
        if problems:
            print("\nThe dependency inventory has drifted. Re-run "
                  "`scripts/lock_and_sbom.py --write` and update THIRD_PARTY.md to match.")
            return 1
        print("PASS: the lock, the SBOM, the npm lockfiles and THIRD_PARTY.md agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
