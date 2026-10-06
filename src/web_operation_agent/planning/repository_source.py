"""Deterministic, language-neutral retrieval over an allowlisted source tree.

This module indexes bounded line blocks from common source and configuration formats
without requiring a compiler or interpreting application-specific syntax. Every block is cited
through :class:`SourceTree`, so raw retrieval has the same release, line and byte-hash
properties as compiled relations.
"""
from __future__ import annotations

import math
import re
import hashlib
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from .source_facts import SourceHit, SourceLookup, terms
from .source_read import (MAX_SOURCE_PATH_CHARS, MAX_SOURCE_READ_CHARS,
                          MAX_SOURCE_READ_LINES, SourceReadError)
from .sourcetree import AccessDenied, SourceTree
from ..schemas import EvidenceRef


SOURCE_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".java",
    ".xml", ".html", ".htm", ".yaml", ".yml", ".sql", ".md", ".markdown",
    ".toml", ".ini", ".cfg", ".conf", ".json", ".json5", ".properties", ".csv",
})
SOURCE_FILENAMES = frozenset({
    "dockerfile", "makefile", "procfile", "codeowners", ".editorconfig",
})
_K1, _B = 1.2, 0.75
_SYMBOL = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_UPPERCASE = re.compile(r"[A-Z]")


def _symbols(text: str) -> frozenset[str]:
    return frozenset(word for word in set(_SYMBOL.findall(text))
                     if "_" in word or _UPPERCASE.search(word, 1))


def _is_source_file(path: str) -> bool:
    leaf = path.rsplit("/", 1)[-1].lower()
    dot = leaf.rfind(".")
    suffix = leaf[dot:] if dot >= 0 else ""
    return suffix in SOURCE_SUFFIXES or leaf in SOURCE_FILENAMES


def _bounded_blocks(lines: list[str], *, line_limit: int,
                    char_limit: int, overlap: int) -> Iterable[tuple[int, int]]:
    """Cover every line with stable windows while keeping evidence model-sized.

    The split deliberately knows nothing about a programming language.  Blank lines are
    preferred boundaries, but a large function, XML element or SQL statement is still
    covered by overlapping hard windows.
    """
    start = 0
    while start < len(lines):
        end = start
        chars = 0
        last_blank = -1
        while end < len(lines) and end - start < line_limit:
            size = len(lines[end])
            if end > start and chars + size > char_limit:
                break
            chars += size
            if not lines[end].strip():
                last_blank = end
            end += 1
        if end < len(lines) and last_blank >= start + max(3, line_limit // 3):
            end = last_blank + 1
        if end <= start:
            end = start + 1
        first, last = start, end
        while first < last and not lines[first].strip():
            first += 1
        while last > first and not lines[last - 1].strip():
            last -= 1
        if first < last:
            yield first + 1, last
        if end >= len(lines):
            break
        start = max(start + 1, end - overlap)


@dataclass(frozen=True, slots=True)
class RepositoryBlock:
    evidence: EvidenceRef
    language: str
    document: Counter[str]
    symbols: frozenset[str]


class RepositorySourceLookup:
    """In-memory BM25 index of one exact allowlisted repository surface."""

    def __init__(self, tree: SourceTree, *, line_limit: int = 36,
                 char_limit: int = 3600, overlap: int = 6,
                 max_files: int = 50_000, max_blocks: int = 250_000,
                 path_filter: Callable[[str], bool] | None = None):
        if not isinstance(tree, SourceTree):
            raise TypeError("tree must be a SourceTree")
        if not 8 <= line_limit <= 200:
            raise ValueError("line_limit must be from 8 through 200")
        if not 512 <= char_limit <= 12_000:
            raise ValueError("char_limit must be from 512 through 12000")
        if not 0 <= overlap < line_limit:
            raise ValueError("overlap must be smaller than line_limit")
        self.release = tree.release
        self.repository_id = tree.repository_id
        if path_filter is not None and not callable(path_filter):
            raise TypeError("path_filter must be callable or None")
        files = [path for path in tree.files()
                 if _is_source_file(path) and (path_filter is None or path_filter(path))]
        self.surface_digest = tree.surface_digest(files)
        if len(files) > max_files:
            raise ValueError(f"allowlisted source surface has {len(files)} files; "
                             f"the configured limit is {max_files}")
        blocks: list[RepositoryBlock] = []
        lengths: list[int] = []
        document_frequency: Counter[str] = Counter()
        skipped_oversize = 0
        skipped_non_utf8 = 0
        file_digests: dict[str, str] = {}
        for path in files:
            try:
                text = tree.read_text(path)
            except AccessDenied:
                skipped_oversize += 1
                continue
            except UnicodeDecodeError:
                skipped_non_utf8 += 1
                continue
            file_digests[path] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            lines = text.splitlines(keepends=True)
            language = path.rsplit(".", 1)[-1].lower() if "." in path else "config"
            path_words = terms(path) * 3
            for start, end in _bounded_blocks(
                    lines, line_limit=line_limit, char_limit=char_limit, overlap=overlap):
                body = "".join(lines[start - 1:end])
                document = Counter(path_words + terms(body))
                if not document:
                    continue
                # The allowlisted file was read above. Cite that same snapshot
                # instead of resolving and rereading the whole file per window.
                # Live verification and follow-up reads still use the tree.
                evidence = EvidenceRef(
                    repository_id=self.repository_id, release=self.release, path=path,
                    start_line=start, end_line=end,
                    sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    excerpt=body[:char_limit].rstrip())
                evidence = self._context(evidence, lines)
                blocks.append(RepositoryBlock(evidence, language, document, _symbols(evidence.excerpt)))
                lengths.append(sum(document.values()))
                document_frequency.update(document.keys())
                if len(blocks) > max_blocks:
                    raise ValueError(f"allowlisted source surface exceeds {max_blocks} blocks")
        self._tree = tree
        self._file_digests = file_digests
        self._blocks = tuple(blocks)
        symbol_blocks: dict[str, set[int]] = {}
        for index, block in enumerate(blocks):
            for symbol in block.symbols:
                symbol_blocks.setdefault(symbol, set()).add(index)
        self._symbol_blocks = {s: frozenset(indices) for s, indices in symbol_blocks.items()}
        self._lengths = tuple(lengths)
        self._average = sum(lengths) / len(lengths) if lengths else 0.0
        count = len(blocks)
        self._idf = {
            word: math.log(1 + (count - seen + 0.5) / (seen + 0.5))
            for word, seen in document_frequency.items()
        }
        self.stats = {
            "files": len(files), "blocks": len(blocks),
            "skipped_oversize": skipped_oversize,
            "skipped_non_utf8": skipped_non_utf8,
            "surface_digest": self.surface_digest,
            "estimated_index_bytes": sum(
                len(block.evidence.excerpt.encode("utf-8"))
                + sum(len(word.encode("utf-8")) + 8 for word in block.document)
                for block in blocks),
        }

    def __len__(self) -> int:
        return len(self._blocks)

    def search(self, query: str, *, limit: int = 8,
               route: Callable[[str], str] | None = None) -> tuple[SourceHit, ...]:
        del route  # raw repository blocks do not grant navigation
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer from 1 through 20")
        wanted = set(terms(query))
        if not wanted:
            return ()
        scored: list[tuple[float, str, int, int]] = []
        # A model may refine using a path seen in source. A named allowlisted file
        # must outrank incidental matches on its generic directory words.
        paths = tuple(re.findall(r"[\w.-]+(?:/[\w.-]+){2,}", query))
        filenames = set(re.findall(r"(?<![\w/.-])([\w.-]+\.[A-Za-z0-9]+)(?![\w/.-])", query))
        identifiers = _symbols(query)
        for index, block in enumerate(self._blocks):
            shared = wanted & block.document.keys()
            if not shared:
                continue
            score = 0.0
            for word in shared:
                frequency = block.document[word]
                norm = _K1 * (
                    1 - _B + _B * self._lengths[index] / (self._average or 1))
                score += self._idf[word] * frequency * (_K1 + 1) / (frequency + norm)
            if score:
                ref = block.evidence
                basename = ref.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if any(ref.path == p or ref.path.endswith("/" + p) for p in paths):
                    score += 10_000
                elif basename in identifiers or ref.path.rsplit("/", 1)[-1] in filenames:
                    score += 1000
                # Preserve exact symbols when a refined query names a guard/error.
                # Tokenization splits identifiers for multilingual matching; those
                # fragments alone otherwise bury the actual usage among generic hits.
                score += 40 * len(identifiers & block.symbols)
                scored.append((-score, ref.path, ref.start_line, index))
        scored.sort()
        # One bounded lexical hop connects a matched error/validator declaration
        # to its actual usages. No framework parser, feature map or inferred rule.
        # Only identifiers on the lines that match the question participate.
        line_symbols = []
        for _, _, _, index in scored[:8]:
            for line in self._blocks[index].evidence.excerpt.splitlines():
                relevance = sum(self._idf[t] for t in wanted & set(terms(line)))
                if relevance:
                    for symbol in _symbols(line) - identifiers:
                        frequency = len(self._symbol_blocks[symbol])
                        if 2 <= frequency <= 40:
                            line_symbols.append((-relevance, frequency, symbol,
                                                 self._blocks[index].evidence.path))
        linked = {}
        for _, _, symbol, origin in sorted(set(line_symbols)):
            linked.setdefault(symbol, origin)
            if len(linked) == 3:
                break
        if linked and not paths:
            # A referenced implementation need not share any words with the
            # original question (e.g. a translated error and its validator).
            # Admit its allowlisted blocks through the existing bounded symbol
            # index, rather than only re-ranking the original lexical pool.
            candidates = {entry[3]: entry for entry in scored}
            for symbol in linked:
                for index in self._symbol_blocks[symbol]:
                    ref = self._blocks[index].evidence
                    candidates.setdefault(index, (0.0, ref.path, ref.start_line, index))
            expanded = sorted((score - 40 * sum(symbol in self._blocks[index].symbols
                                                and path != origin for symbol, origin in linked.items()),
                               path, start, index) for score, path, start, index in candidates.values())
            # Cross references supplement lexical hits; they cannot displace all
            # original page markup or other evidence that answered the query.
            first = scored[:(limit + 1) // 2]
            held = {entry[3] for entry in first}
            scored = first + [entry for entry in expanded if entry[3] not in held]
        return tuple(
            SourceHit(evidence=ref,
                      text=ref.excerpt,
                      verbatim=True, max_chars=4000, fact_type="implementation")
            for _, _, _, index in scored[:limit]
            for ref in (self._blocks[index].evidence,)
        )

    @staticmethod
    def _context(reference: EvidenceRef, lines: list[str]) -> EvidenceRef:
        """Keep neighbouring guards with a matched message or statement.

        Build from the allowlisted snapshot at index construction, so searches
        keep working after an extraction directory is removed. Live verification
        remains separate. Expansion keeps the match and is bounded by whole lines.
        """
        start, end = reference.start_line, reference.end_line
        size = len("".join(lines[start - 1:end]))
        for distance in range(1, 25):
            for before in (True, False):
                candidate = reference.start_line - distance if before else reference.end_line + distance
                if not 1 <= candidate <= len(lines):
                    continue
                # Never create a gap after a neighbouring line exceeds the budget.
                if candidate != (start - 1 if before else end + 1):
                    continue
                length = len(lines[candidate - 1])
                if size + length > 4000:
                    continue
                size += length
                if before:
                    start = candidate
                else:
                    end = candidate
        body = "".join(lines[start - 1:end])
        return replace(reference, start_line=start, end_line=end,
                       sha256=hashlib.sha256(body.encode()).hexdigest(), excerpt=body[:4000].rstrip())

    def verify(self, reference: EvidenceRef) -> bool:
        return self._tree.verify(reference)

    def read(self, path: str, *, start_line: int, end_line: int) -> SourceHit:
        """Read whole lines from this index's exact release and application-owned scope.

        The file digest was captured when indexing, not supplied by the model. A
        changed file cannot mix new bytes with an old release, even when the requested
        lines themselves stayed unchanged. No truncation hides omitted conditions.
        """
        if (not isinstance(path, str) or not path or len(path) > MAX_SOURCE_PATH_CHARS
                or re.search(r"[\\\x00-\x1f\x7f]", path)
                or any(part in {"", ".", ".."} for part in path.split("/"))):
            raise SourceReadError("invalid_path", "path must be a canonical repository-relative file")
        if (isinstance(start_line, bool) or isinstance(end_line, bool)
                or not isinstance(start_line, int) or not isinstance(end_line, int)
                or not 1 <= start_line <= end_line
                or end_line - start_line + 1 > MAX_SOURCE_READ_LINES):
            raise SourceReadError("invalid_range", f"read 1-{MAX_SOURCE_READ_LINES} positive numbered lines")
        if path not in self._file_digests:
            raise SourceReadError("outside_surface", "file is outside the readable deployment surface")
        try:
            text = self._tree.read_text(path)
        except (AccessDenied, OSError, UnicodeDecodeError) as exc:
            raise SourceReadError("source_unavailable", "deployment source is no longer readable") from exc
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != self._file_digests[path]:
            raise SourceReadError("source_changed", "source file no longer matches the indexed deployment")
        lines = text.splitlines(keepends=True)
        if end_line > len(lines):
            raise SourceReadError("invalid_range", f"requested range exceeds the file's {len(lines)} lines")
        body = "".join(lines[start_line - 1:end_line])
        if len(body) > MAX_SOURCE_READ_CHARS:
            raise SourceReadError("slice_too_large", f"request fewer lines; the limit is {MAX_SOURCE_READ_CHARS} characters")
        if not body.strip():
            raise SourceReadError("empty_slice", "the selected lines contain no source text")
        reference = EvidenceRef(repository_id=self.repository_id, release=self.release,
            path=path, start_line=start_line, end_line=end_line,
            sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(), excerpt=body.rstrip())
        return SourceHit(evidence=reference, text=reference.excerpt,
                         verbatim=True, max_chars=4000, fact_type="implementation")


class CombinedSourceLookup:
    """Merge independent same-release indexes without comparing incomparable BM25 scores.

    Reciprocal rank fusion would produce the same ordering for disjoint results; a bounded
    round-robin is simpler and keeps high-precision compiled facts ahead of raw blocks while
    guaranteeing that raw XML/configuration/code remains visible in every result page.
    """

    def __init__(self, *lookups: SourceLookup):
        if not lookups or any(not isinstance(item, SourceLookup) for item in lookups):
            raise TypeError("lookups must implement SourceLookup")
        releases = {item.release for item in lookups}
        if len(releases) != 1:
            raise ValueError("combined source lookups must use one release")
        self.release = lookups[0].release
        self._lookups = tuple(lookups)

    def search(self, query: str, *, limit: int = 8,
               route: Callable[[str], str] | None = None) -> tuple[SourceHit, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer from 1 through 20")
        ranked = [lookup.search(query, limit=limit, route=route) for lookup in self._lookups]
        out: list[SourceHit] = []
        seen: set[tuple[str, str, int, int, str]] = set()
        for rank in range(limit):
            for results in ranked:
                if rank >= len(results):
                    continue
                hit = results[rank]
                ref = hit.evidence
                key = (ref.repository_id, ref.path, ref.start_line, ref.end_line, ref.sha256)
                if key in seen:
                    continue
                seen.add(key)
                out.append(hit)
                if len(out) == limit:
                    return tuple(out)
        return tuple(out)
