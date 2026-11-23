"""Frozen reason codes, cell result schema, and JSONL ledger for the surface census."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Iterable

FAMILIES: tuple[str, ...] = ("find", "detail", "aggregate", "open_page", "filter_page")
STATUSES: tuple[str, ...] = ("pass", "fail", "unsupported", "untested")

#: Fixed reason-code table frozen in Step 0. Keep compact and generic.
REASON_CODES: dict[str, str] = {
    "label_mismatch": "列名或编码选项显示标签与运行页面/独立数据源不一致",
    "undecoded": "编码列返回原始代码而未提供可读字典标签",
    "unit_mismatch": "数值或金额的显示单位/换算倍率与运行页面不一致",
    "value_mismatch": "读取字段值、总数、计数、求和或极值与独立对照不一致",
    "incomplete_read": "读取未完全即返回或行上限低于总行数导致截断",
    "page_size_rejected": "按模型可见接口说明允许的最大页大小传参却被产品或接口拒绝",
    "missing_tie_note": "极值存在同值并列却未给出并列提示",
    "value_rejected": "按用户写法传入的值被工具或表单校验拒绝",
    "api_rejected": "合法读取请求被应用接口拒绝",
    "answer_check_false_block": "正确答复被数字溯源或交付检查误拦",
    "receipt_mismatch": "产品回给模型的页面回执与页面实际请求或渲染结果不一致",
    "candidate_missing": "find_record 未命中所抽真实记录或同名多实体记录未全部列出",
    "page_reloaded": "同路径再次 goto 触发整页重载导致页面上下文重建",
    "page_not_reached": "浏览器最终路由未到达目标页面或页面加载阻塞",
    "filter_ineffective": "页面自身发出的列表请求或返回行未体现所填筛选条件",
    "control_unavailable": "页面筛选控件无法通过 fill_fields 填写或触发查询",
    "tool_error": "非预期工具拒绝、执行异常或回合超时",
}

SECURITY_SURFACE_REASON_CODES: frozenset[str] = frozenset({
    "answer_check_false_block",
    "receipt_mismatch",
})


@dataclass(frozen=True)
class CellResult:
    cell_id: str
    family: str
    entity: str
    domain: str
    operation: str
    field_or_control: str
    field_or_control_type: str
    status: str
    reason_code: str = ""
    evidence_path: str = ""
    detail: str = ""
    flaky: bool = False
    attempts: int = 1
    elapsed_s: float = 0.0

    def __post_init__(self) -> None:
        if self.family not in FAMILIES:
            raise ValueError(f"unknown family: {self.family!r}")
        if self.status not in STATUSES:
            raise ValueError(f"unknown status: {self.status!r}")
        if self.status == "fail":
            if self.reason_code not in REASON_CODES:
                raise ValueError(f"fail cell {self.cell_id!r} requires a frozen reason_code, got {self.reason_code!r}")
        elif self.reason_code and self.reason_code not in REASON_CODES and self.status == "pass":
            raise ValueError(f"pass cell {self.cell_id!r} must not carry unknown reason_code {self.reason_code!r}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CellResult":
        return cls(**data)


class Ledger:
    """Append-safe JSONL census ledger with deterministic summary & class grouping."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._records: dict[str, CellResult] = {}
        if self.path.is_file():
            self.load()

    def load(self) -> None:
        self._records.clear()
        if not self.path.is_file():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            item = CellResult.from_dict(json.loads(line))
            self._records[item.cell_id] = item

    def append(self, cell: CellResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._records[cell.cell_id] = cell
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(cell.to_dict(), ensure_ascii=False) + "\n")

    def rewrite(self, cells: Iterable[CellResult]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._records.clear()
        lines = []
        for cell in cells:
            self._records[cell.cell_id] = cell
            lines.append(json.dumps(cell.to_dict(), ensure_ascii=False))
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    @property
    def cells(self) -> list[CellResult]:
        return list(self._records.values())

    def has(self, cell_id: str) -> bool:
        return cell_id in self._records

    def get(self, cell_id: str) -> CellResult | None:
        return self._records.get(cell_id)

    def failure_classes(self) -> list[dict]:
        groups: dict[tuple[str, str], list[CellResult]] = defaultdict(list)
        for cell in self._records.values():
            if cell.status == "fail":
                groups[(cell.reason_code, cell.field_or_control_type)].append(cell)
        out = []
        for (reason_code, control_type), members in sorted(
            groups.items(),
            key=lambda kv: (-len(kv[1]), kv[0][0], kv[0][1]),
        ):
            is_security = reason_code in SECURITY_SURFACE_REASON_CODES
            qualifies = len(members) >= 3 or is_security
            out.append({
                "reason_code": reason_code,
                "field_or_control_type": control_type,
                "count": len(members),
                "security_surface": is_security,
                "qualifies_for_fix": qualifies,
                "families": sorted({m.family for m in members}),
                "cell_ids": [m.cell_id for m in members],
            })
        return out

    def summary(self, *, inventory_meta: dict | None = None) -> dict:
        by_family: dict[str, dict] = {}
        overall_counts = Counter(c.status for c in self._records.values())
        for fam in FAMILIES:
            fam_cells = [c for c in self._records.values() if c.family == fam]
            counts = Counter(c.status for c in fam_cells)
            tested = counts["pass"] + counts["fail"]
            entities = {c.entity for c in fam_cells}
            tested_entities = {c.entity for c in fam_cells if c.status in ("pass", "fail")}
            entry = {
                "cells": len(fam_cells),
                "pass": counts["pass"],
                "fail": counts["fail"],
                "unsupported": counts["unsupported"],
                "untested": counts["untested"],
                "tested": tested,
                "fail_rate": round(counts["fail"] / tested, 4) if tested else 0.0,
                "entities_in_ledger": len(entities),
                "entities_tested": len(tested_entities),
                "flaky_cells": sum(1 for c in fam_cells if c.flaky),
                "elapsed_s": round(sum(c.elapsed_s for c in fam_cells), 2),
                "reason_breakdown": dict(Counter(c.reason_code for c in fam_cells if c.status == "fail")),
            }
            if inventory_meta and fam in inventory_meta:
                meta = inventory_meta[fam]
                applicable = meta.get("applicable_entities", 0)
                covered = len(entities & set(meta.get("applicable_entity_keys", ())))
                entry["applicable_entities"] = applicable
                entry["covered_entities"] = covered
                entry["coverage_rate"] = round(covered / applicable, 4) if applicable else 1.0
                entry["inapplicable_entities"] = meta.get("inapplicable_entities", 0)
                entry["unclassified_entities"] = meta.get("unclassified_entities", 0)
                entry["unclassified_rate"] = meta.get("unclassified_rate", 0.0)
            by_family[fam] = entry
        total_tested = overall_counts["pass"] + overall_counts["fail"]
        return {
            "total_cells": len(self._records),
            "pass": overall_counts["pass"],
            "fail": overall_counts["fail"],
            "unsupported": overall_counts["unsupported"],
            "untested": overall_counts["untested"],
            "tested": total_tested,
            "fail_rate": round(overall_counts["fail"] / total_tested, 4) if total_tested else 0.0,
            "flaky_cells": sum(1 for c in self._records.values() if c.flaky),
            "total_elapsed_s": round(sum(c.elapsed_s for c in self._records.values()), 2),
            "by_family": by_family,
            "failure_classes": self.failure_classes(),
            "reason_codes": REASON_CODES,
        }

    def write_summary(self, out_path: Path, *, inventory_meta: dict | None = None) -> dict:
        data = self.summary(inventory_meta=inventory_meta)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return data
