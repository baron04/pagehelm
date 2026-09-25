"""Evaluator-only verification of release-bound citations in the visible answer.

Citation block wire form (optionally inside Markdown):
    addons/module/models/model.py:10-14@release sha256:<64 lowercase hex digits>
The hash covers the exact UTF-8 source lines, including their original line endings.
The root and whole-file hashes come from the sealed evaluator binding, never the answer.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from types import MappingProxyType

_CITATION = re.compile(
    r'(?<![\w/.-])(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)'
    r':(?P<start>[1-9][0-9]{0,6})-(?P<end>[1-9][0-9]{0,6})'
    r'@(?P<release>[A-Za-z0-9_.+:-]+)[ \t]*(?:\r?\n[ \t]*)?'
    r'sha256[:=](?P<digest>[0-9a-f]{64})(?![0-9a-f])')


class CitationSource:
    """An explicit, sealed source-file allowlist; no ambient environment or remote reads."""

    def __init__(self, root: Path, *, releases: list[str], files: dict[str, str]):
        self.root = root.resolve(strict=True)
        self.releases = frozenset(releases)
        self.files = MappingProxyType(dict(files))
        if not self.releases or not self.files:
            raise ValueError('source binding is empty')
        for path in self.files:
            self.read_lines(path)  # Preflight before any model call.

    def read_lines(self, relative: str) -> list[bytes]:
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or str(path) != relative:
            raise ValueError('invalid source path')
        if relative not in self.files:
            raise ValueError('source path is not sealed')
        candidate = (self.root / relative).resolve(strict=True)
        if not candidate.is_relative_to(self.root) or candidate.stat().st_size > 8_388_608:
            raise ValueError('source path is outside the bound tree or too large')
        raw = candidate.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.files[relative]:
            raise ValueError('sealed source file changed')
        raw.decode('utf-8', errors='strict')
        return raw.splitlines(keepends=True)

    def verified_locations(self, answer: str) -> str:
        """Return only byte-verified file:lines@release locations, plus covered line atoms.

        Line atoms let a task require its exact rule lines without depending on whether a
        compiler cites an XML element and a generic retriever cites an enclosing block.
        No method names or other prose from the answer enter the matching surface.
        """
        locations = []
        if len(answer) > 200_000:
            return ''
        matches = list(_CITATION.finditer(answer))
        if len(matches) > 64:
            return ''
        for match in matches:
            path, release = match['path'], match['release']
            if release not in self.releases:
                continue
            start, end = int(match['start']), int(match['end'])
            if end < start or end - start >= 512:
                continue
            try:
                lines = self.read_lines(path)
            except (OSError, ValueError):
                continue
            if end > len(lines):
                continue
            digest = hashlib.sha256(b''.join(lines[start - 1:end])).hexdigest()
            if digest != match['digest']:
                continue
            locations.append(f'{path}:{start}-{end}@{release}')
            locations.extend(f'{path}:{line}@{release}' for line in range(start, end + 1))
        return '\n'.join(locations)


def bound_source(doc: dict, root: Path, release: str) -> CitationSource | None:
    binding = doc.get('source_binding')
    if binding is None:
        return None
    if release != binding['deployment_release']:
        raise ValueError('configured source release differs from frozen rules binding')
    return CitationSource(root, releases=binding['citation_releases'], files=binding['files'])
