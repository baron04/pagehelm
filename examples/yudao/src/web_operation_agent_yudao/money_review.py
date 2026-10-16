"""Release-bound, individually verified API-to-page money display values."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re


_COMPONENT = re.compile(r"[A-Za-z0-9_/-]{1,160}")
_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
_ENDPOINT = re.compile(r"/[A-Za-z0-9_/-]{1,160}")
_FUNCTIONS = frozenset({"src/utils/index.ts", "src/utils/formatter.ts"})
_FORMATTER = re.compile(r':formatter="([A-Za-z][A-Za-z0-9_]*)"')
_DIRECT_DIVIDE = re.compile(r"\b(?:scope\.)?row\.([A-Za-z][A-Za-z0-9_]*)\s*/\s*100(?:\.0)?\b")
_DIRECT_FEN = re.compile(r"\bfenToYuan\s*\(\s*(?:scope\.)?row\.([A-Za-z][A-Za-z0-9_]*)\s*\)")
_ROW_FIELD = re.compile(r"\b(?:scope\.)?row\.([A-Za-z][A-Za-z0-9_]*)\b")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compiled_factor(component_text: str, field: str) -> str | None:
    from .ui_source import _TABLE_COLUMN, _COLUMN_PROP
    factors = []
    for match in _TABLE_COLUMN.finditer(component_text):
        attrs, body = match.groups()
        prop = _COLUMN_PROP.search(attrs)
        if prop is None or prop[1] != field or ":prop" in attrs:
            continue
        formatter = _FORMATTER.search(attrs)
        if formatter and not (body or "").strip():
            factors.append({"erpPriceTableColumnFormatter": "1",
                            "fenToYuanFormat": "0.01"}.get(formatter[1]))
        elif not formatter and body:
            used = _ROW_FIELD.findall(body)
            converted = _DIRECT_DIVIDE.findall(body) + _DIRECT_FEN.findall(body)
            factors.append("0.01" if used == [field] and converted == [field] else None)
    return factors[0] if len(factors) == 1 else None


@dataclass(frozen=True)
class VerifiedMoneyField:
    route: str
    endpoint: str
    field: str
    factor: Decimal
    display_unit: str

    @staticmethod
    def convertible(raw) -> bool:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return False
        try:
            number = Decimal(str(raw))
            return number.is_finite() and abs(number) < Decimal("1e15")
        except InvalidOperation:
            return False

    def display_value(self, raw):
        if not self.convertible(raw):
            return raw
        shown = Decimal(str(raw)) * self.factor
        if shown == shown.to_integral_value():
            return int(shown)
        return float(shown)


class VerifiedMoneyReview:
    """Only fields with reviewed nonzero page/API matches on exact deployed source."""

    def __init__(self, fields: tuple[VerifiedMoneyField, ...]):
        self._by_endpoint = {}
        for item in fields:
            self._by_endpoint.setdefault(item.endpoint, {})[item.field] = item

    @classmethod
    def load(cls, path: Path, *, ui_source: Path, release: str, ui):
        path, ui_source = Path(path), Path(ui_source)
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError("cannot read the verified money policy JSON") from exc
        if (not isinstance(document, dict)
                or set(document) != {"schema_version", "release", "review", "source_functions", "fields"}
                or document["schema_version"] != 1 or document["release"] != release
                or not isinstance(document["review"], str)
                or not 0 < len(document["review"].strip()) <= 200
                or not isinstance(document["source_functions"], dict)
                or set(document["source_functions"]) != _FUNCTIONS
                or not isinstance(document["fields"], list)
                or not 1 <= len(document["fields"]) <= 100):
            raise ValueError("verified money policy must match the deployment release")
        root = ui_source.resolve()
        for relative, expected in document["source_functions"].items():
            source = root / relative
            if not source.resolve().is_relative_to(root):
                raise ValueError("verified money policy source escapes the UI tree")
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("verified money policy has an invalid source hash")
            try:
                actual = _sha(source)
            except OSError as exc:
                raise ValueError("verified money policy source is unavailable") from exc
            if actual != expected:
                raise ValueError("verified money policy formatter source changed")
        fields = []
        keys = set()
        for item in document["fields"]:
            if (not isinstance(item, dict)
                    or set(item) != {"route", "endpoint", "field", "factor", "display_unit",
                                    "component", "component_sha256", "nonzero_samples"}
                    or not isinstance(item["route"], str) or not _ENDPOINT.fullmatch(item["route"])
                    or not isinstance(item["endpoint"], str) or not _ENDPOINT.fullmatch(item["endpoint"])
                    or not isinstance(item["field"], str) or not _FIELD.fullmatch(item["field"])
                    or not isinstance(item["factor"], str) or item["factor"] not in {"1", "0.01"}
                    or item["display_unit"] != "元"
                    or not isinstance(item["component"], str) or not _COMPONENT.fullmatch(item["component"])
                    or item["component"].startswith("/") or "//" in item["component"]
                    or not isinstance(item["component_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["component_sha256"])
                    or type(item["nonzero_samples"]) is not int
                    or not 1 <= item["nonzero_samples"] <= 10000):
                raise ValueError("verified money policy contains an invalid field")
            key = (item["endpoint"], item["field"])
            if key in keys:
                raise ValueError("verified money policy has duplicate endpoint fields")
            keys.add(key)
            source = root / "src/views" / (item["component"] + ".vue")
            if not source.resolve().is_relative_to(root / "src/views"):
                raise ValueError("verified money policy component escapes the UI tree")
            try:
                actual = _sha(source)
            except OSError as exc:
                raise ValueError("verified money policy component is unavailable") from exc
            if actual != item["component_sha256"]:
                raise ValueError("verified money policy component source changed")
            if _compiled_factor(source.read_text(encoding="utf-8"), item["field"]) != item["factor"]:
                raise ValueError("verified money policy factor no longer matches the page")
            view = ui.views.get(item["component"])
            if view is None or view.endpoint != item["endpoint"]:
                raise ValueError("verified money policy endpoint no longer matches the page")
            fields.append(VerifiedMoneyField(item["route"], item["endpoint"], item["field"],
                                             Decimal(item["factor"]), item["display_unit"]))
        return cls(tuple(fields))

    def fields_for(self, endpoint: str) -> dict[str, VerifiedMoneyField]:
        return self._by_endpoint.get(endpoint, {})
