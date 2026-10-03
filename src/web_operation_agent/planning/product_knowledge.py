"""Optional release-bound guides beside raw repository evidence.

Application distributions derive guides offline. This index knows no framework,
route or action vocabulary. A guide is a retrieval aid, not a permission, an
execution receipt, or proof that its prose follows from the cited bytes.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import re

from .source_facts import SourceHit, SourceLookup, terms
from .sourcetree import SourceTree
from ..schemas import EvidenceRef


@dataclass(frozen=True, slots=True)
class KnowledgeSection:
    summary: str
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self):
        if not isinstance(self.summary, str) or not 1 <= len(self.summary) <= 1800:
            raise ValueError("knowledge summary must contain 1 to 1800 characters")
        if (not isinstance(self.evidence, tuple) or not 1 <= len(self.evidence) <= 8
                or any(not isinstance(ref, EvidenceRef) or not ref.excerpt
                       or len(ref.excerpt) > 4000 for ref in self.evidence)):
            raise ValueError("knowledge needs one to eight bounded original excerpts")


@dataclass(frozen=True, slots=True)
class KnowledgeGuide:
    key: str
    title: str
    sections: tuple[KnowledgeSection, ...]

    def __post_init__(self):
        for name, bound in (("key", 300), ("title", 300)):
            value = getattr(self, name)
            if not isinstance(value, str) or not 1 <= len(value) <= bound:
                raise ValueError(f"knowledge {name} must contain 1 to {bound} characters")
        if (not isinstance(self.sections, tuple) or not 1 <= len(self.sections) <= 64
                or any(not isinstance(s, KnowledgeSection) for s in self.sections)):
            raise ValueError("knowledge guide needs one to 64 sections")


class ProductKnowledgeLookup:
    """Locate original evidence through guides; retain optional raw fallback.

    Every selected guide's complete evidence set is reverified. The digest includes
    guide text and all input files, so different derived prose cannot share
    declaration-cache entries. Stale guides fail closed.
    """

    def __init__(self, tree: SourceTree, guides: tuple[KnowledgeGuide, ...], *,
                 fallback: SourceLookup | None = None):
        if not isinstance(tree, SourceTree):
            raise TypeError("knowledge tree must be a SourceTree")
        if (not isinstance(guides, tuple) or len(guides) > 5000
                or any(not isinstance(g, KnowledgeGuide) for g in guides)
                or len({g.key for g in guides}) != len(guides)):
            raise ValueError("knowledge guides must have unique keys and be bounded")
        if fallback is not None and (not isinstance(fallback, SourceLookup)
                                     or fallback.release != tree.release):
            raise ValueError("knowledge fallback must search the same release")
        self.release = tree.release
        self.guides = guides
        self._tree, self._fallback = tree, fallback
        for guide in guides:
            self._verify_guide(guide)
        payload = [[g.key, g.title, [[s.summary, [r.to_json() for r in s.evidence]]
                                    for s in g.sections]] for g in guides]
        self.surface_digest = hashlib.sha256(json.dumps(
            [tree.surface_digest(), payload], ensure_ascii=False,
            sort_keys=True).encode()).hexdigest()
        self._documents = [Counter(terms(g.title) * 3 + terms(" ".join(
            s.summary for s in g.sections))) for g in guides]
        counts = Counter(t for doc in self._documents for t in doc)
        self._idf = {t: math.log(1 + (len(guides) - n + .5) / (n + .5))
                     for t, n in counts.items()}
        self._average = sum(sum(doc.values()) for doc in self._documents) / max(1, len(guides))

    def _verify_guide(self, guide):
        for section in guide.sections:
            for ref in section.evidence:
                if not self._tree.verify(ref):
                    raise ValueError("knowledge evidence no longer matches its release")
                lines = self._tree.read_text(ref.path).splitlines(keepends=True)
                body = "".join(lines[ref.start_line - 1:ref.end_line])
                if ref.excerpt != body.rstrip():
                    raise ValueError("knowledge excerpt must contain its complete cited bytes")

    def rank(self, query: str) -> tuple[KnowledgeGuide, ...]:
        if not isinstance(query, str) or len(query) > 4000:
            raise ValueError("knowledge query must be a bounded string")
        wanted = set(terms(query))
        ranked = []
        for i, doc in enumerate(self._documents):
            norm = 1.2 * (.25 + .75 * sum(doc.values()) / max(1, self._average))
            score = sum(self._idf[t] * doc[t] * 2.2 / (doc[t] + norm)
                        for t in wanted & doc.keys())
            fragments = re.findall(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|[A-Za-z0-9_-]+\.[A-Za-z]+", query)
            if any(fragment in self.guides[i].key for fragment in fragments):
                score += 1000
            if score:
                ranked.append((-score, self.guides[i].key, i))
        return tuple(self.guides[i] for _, _, i in sorted(ranked))

    def search(self, query: str, *, limit: int = 8, route=None) -> tuple[SourceHit, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("knowledge limit must be an integer from 1 through 20")
        selected = self.rank(query)[:2]
        hits, seen = [], set()
        for guide in selected:
            self._verify_guide(guide)
            for section in guide.sections:
                for ref in section.evidence:
                    if ref.key not in seen:
                        seen.add(ref.key)
                        hits.append(SourceHit(ref, f"{guide.title}: {section.summary}",
                                              max_chars=4000))
        if self._fallback is not None:
            # Raw slots keep guards and uncompiled knowledge reachable.
            hits = hits[:max(0, limit - 2)]
            seen = {h.evidence.key for h in hits}
            hits.extend(h for h in self._fallback.search(query, limit=limit, route=route)
                        if h.evidence.key not in seen)
        return tuple(hits[:limit])

    def verify(self, reference: EvidenceRef) -> bool:
        return self._tree.verify(reference)


__all__ = ["KnowledgeGuide", "KnowledgeSection", "ProductKnowledgeLookup"]
