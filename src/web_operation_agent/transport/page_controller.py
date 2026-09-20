"""The pinned PageController build, and the check that a deployment really serves it.

The Runtime (`web_runtime/assistant.js`) imports PageController from an absolute module URL that the
application deployment supplies. That is the right boundary -- the asset belongs to the
application's origin, not to this wheel -- but it leaves one thing unsaid: the URL does not
attest which build answered it. A deployment can serve a different version, a patched file,
or nothing at all, and the Runtime finds out by breaking.

So the version is a fact this package states, once, in `policies/page_controller.json`, and
everything else reads it: the Runtime name reported to a host, the third-party notice, and
the provisioning step that fetches the asset. Before this file existed the statement lived in
prose in two places and was wrong in both -- 1.12.2 in `THIRD_PARTY.md` and in the Runtime
name, while every qualified environment served 1.12.4 byte for byte.

Nothing here fetches anything. Fetching is a deployment step in the application's own
distribution; this module says what a correct result looks like and can recognise one.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


PINNED_PAGE_CONTROLLER = json.loads(
    (Path(__file__).parents[1] / "policies" / "page_controller.json").read_text())

#: The module file a page loads. The others travel with it: the mask chunk because the entry
#: file lazily imports it, and the licence because serving the asset redistributes it.
ENTRY_FILE = "page-controller.js"


class PageControllerAssetError(RuntimeError):
    """A served PageController asset is absent, or is not the pinned build."""


def pinned_version() -> str:
    return str(PINNED_PAGE_CONTROLLER["version"])


def runtime_name() -> str:
    """What the Runtime calls itself to a host, derived rather than written twice."""
    return f"woa-embedded/page-controller-{pinned_version()}"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_served_directory(directory: str | Path) -> dict:
    """Check that `directory` holds exactly the pinned build, and say what is wrong if not.

    Returns one bounded report rather than raising, because the two callers want different
    things from the same answer: provisioning wants to know whether its own fetch succeeded,
    and a release gate wants to name every file that disagrees rather than the first.
    """
    root = Path(directory)
    files = {}
    problems = []
    for name, expected in sorted(PINNED_PAGE_CONTROLLER["served_files"].items()):
        target = root / name
        if not target.is_file():
            files[name] = {"present": False, "sha256": "", "matches": False}
            problems.append(f"{name} is not served")
            continue
        observed = _digest(target)
        matches = observed == expected["sha256"]
        files[name] = {"present": True, "sha256": observed, "matches": matches}
        if not matches:
            problems.append(
                f"{name} is served as sha256:{observed[:16]}… but the pinned "
                f"{PINNED_PAGE_CONTROLLER['version']} build is sha256:{expected['sha256'][:16]}…")
    return {
        "package": PINNED_PAGE_CONTROLLER["package"],
        "version": pinned_version(),
        "directory": str(root),
        "files": files,
        "problems": problems,
        "verified": not problems,
    }


def require_served_directory(directory: str | Path) -> dict:
    """`verify_served_directory`, refusing instead of reporting. For a provisioning step."""
    report = verify_served_directory(directory)
    if not report["verified"]:
        raise PageControllerAssetError("; ".join(report["problems"]))
    return report


__all__ = [
    "ENTRY_FILE", "PINNED_PAGE_CONTROLLER", "PageControllerAssetError", "pinned_version",
    "require_served_directory", "runtime_name", "verify_served_directory",
]
