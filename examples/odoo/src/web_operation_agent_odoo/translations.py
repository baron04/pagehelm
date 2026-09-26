"""What the deployed Odoo calls its own labels in Chinese, from its own translation files.

Odoo ships a zh_CN translation for every module: the name a Chinese-speaking user sees for
an entity, a page, a field, a choice or a button. The goal index shows the first of these
beside each English label and the resolver accepts them, so a request that says 源单据
reaches Source Document because the application says so, not because someone wrote the
pair down. The files are read through their own allowlist (translations only), separate
from the source tree the model can search, so they add no noise to source retrieval.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from web_operation_agent.sourcetree import SourceTree

#: Only the Chinese translation of each module; nothing else under the root.
TRANSLATION_POLICY = {
    "repository_id": "odoo/odoo",
    "max_bytes_per_read": 16 * 1024 * 1024,
    "allow_globs": ["addons/*/i18n/zh_CN.po", "odoo/addons/base/i18n/zh_CN.po"],
    "deny_globs": ["**/.env*", "**/*.key", "**/*.pem", "**/secrets/**"],
}

#: A label longer than this is a sentence (help text, a message), not a name.
_MAX_LABEL = 60
#: A name does not end like a sentence.
_SENTENCE_END = (".", "?", "!", ":", "。", "？", "！", "：")
#: Translations kept per label; a label used in several places may be translated apart.
_MAX_FORMS = 2


@dataclass(frozen=True)
class OdooTranslations:
    """English label -> the Chinese names the application gives it, and back."""
    labels: dict[str, tuple[str, ...]] = field(default_factory=dict)
    english: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def of(self, label: str) -> tuple[str, ...]:
        return self.labels.get(label, ())

    def back(self, text: str) -> tuple[str, ...]:
        """The English labels a Chinese name translates, most specific first."""
        return self.english.get(" ".join(str(text or "").split()), ())

    @staticmethod
    def none() -> "OdooTranslations":
        return OdooTranslations()


def parse_po(text: str) -> list[tuple[str, str]]:
    """(msgid, msgstr) pairs of a gettext catalogue, multi-line strings joined."""
    pairs: list[tuple[str, str]] = []
    msgid = msgstr = None
    current = None

    def flush() -> None:
        if msgid and msgstr:
            pairs.append((msgid, msgstr))

    for line in text.splitlines():
        line = line.strip()
        try:
            if line.startswith("msgid "):
                flush()
                msgid, msgstr, current = ast.literal_eval(line[6:]), None, "id"
            elif line.startswith("msgstr "):
                msgstr, current = ast.literal_eval(line[7:]), "str"
            elif line.startswith('"') and current == "id" and msgid is not None:
                msgid += ast.literal_eval(line)
            elif line.startswith('"') and current == "str" and msgstr is not None:
                msgstr += ast.literal_eval(line)
            elif not line:
                current = None
        except (ValueError, SyntaxError):
            current = None
    flush()
    return pairs


def odoo_translations(source_root: str | Path, *, release: str) -> OdooTranslations:
    """The zh_CN names under `source_root`, or none when it holds no translations."""
    tree = SourceTree({**TRANSLATION_POLICY, "release": release}, source_root)
    if not tree.available():
        return OdooTranslations.none()
    labels: dict[str, list[str]] = {}
    for rel in tree.files():
        for msgid, msgstr in parse_po(tree.read_text(rel)):
            name, chinese = " ".join(msgid.split()), " ".join(msgstr.split())
            if (not name or not chinese or name == chinese or len(name) > _MAX_LABEL
                    or len(chinese) > _MAX_LABEL or "%" in name or "{" in name
                    or name.endswith(_SENTENCE_END) or chinese.endswith(_SENTENCE_END)):
                continue
            forms = labels.setdefault(name, [])
            if chinese not in forms and len(forms) < _MAX_FORMS:
                forms.append(chinese)
    english: dict[str, list[str]] = {}
    for name, forms in labels.items():
        for chinese in forms:
            english.setdefault(chinese, []).append(name)
    # A shorter English label is the more general name for the same Chinese word.
    return OdooTranslations(
        labels={name: tuple(forms) for name, forms in labels.items()},
        english={chinese: tuple(sorted(names, key=len)[:4])
                 for chinese, names in english.items()})


__all__ = ["OdooTranslations", "TRANSLATION_POLICY", "odoo_translations", "parse_po"]
