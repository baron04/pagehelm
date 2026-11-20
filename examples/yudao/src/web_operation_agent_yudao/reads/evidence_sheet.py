"""Compile-time S3 ErpStockRecordBizTypeEnum bridge and probe helpers for ctrl_base."""
from __future__ import annotations

from dataclasses import dataclass
import re

_TIME_KEY = re.compile(r"(?:Time|Date)$", re.I)
_NOT_A_QUANTITY = re.compile(
    r"(?:^|[a-z])(?:Id|No|Status|State|Type|Sort|Level|Sex|Code|Enable|Flag)$"
    r"|^(?:id|no|status|state|type|sort|level|sex|code|enable|flag)$"
)
_TAG_PREFIX = re.compile(r"^(?:管理后台\s*-\s*)?(?:ERP\s+)?")


@dataclass(frozen=True, slots=True)
class S3BridgeEdge:
    """One S3 header-document -> inventory-ledger sibling link compiled from ERP bizTypes."""

    edge_id: str
    target_path: str
    fixed_params: tuple[tuple[str, object], ...]


def _coerce_param(raw_val: object, param_schema: dict | None) -> object:
    kind = ((param_schema or {}).get("schema") or {}).get("type")
    if kind == "integer":
        try:
            return int(str(raw_val))
        except (TypeError, ValueError):
            return raw_val
    return raw_val


class S3ErpStockRecordIndex:
    """Only S3 (`ErpStockRecordBizTypeEnum` -> `erp/stock-record/page`) compiled without S1/S2."""

    def __init__(self, edges_by_path: dict[str, tuple[S3BridgeEdge, ...]]):
        self._edges = edges_by_path

    def edges_for(self, path: str) -> tuple[S3BridgeEdge, ...]:
        return self._edges.get(path, ())

    @classmethod
    def build(
        cls,
        by_path: dict[str, dict],
        writing_gets: frozenset[str],
        *,
        codes_fn=None,
    ) -> "S3ErpStockRecordIndex":
        if codes_fn is None:
            return cls({})
        edges: dict[str, list[S3BridgeEdge]] = {}
        ledger_path = "/admin-api/erp/stock-record/page"
        ledger_op = by_path.get(ledger_path)
        if not ledger_op or ledger_path in writing_gets:
            return cls({})
        qparams = {
            q.get("name"): q for q in ledger_op["op"].get("parameters", ())
            if isinstance(q, dict) and q.get("in") == "query"
        }
        if "bizType" not in qparams:
            return cls({})
        try:
            biz_map = (codes_fn("erp/stock-record/page") or {}).get("bizType") or {}
        except Exception:
            biz_map = {}
        if not isinstance(biz_map, dict) or not biz_map:
            return cls({})
        active_biz = [
            (str(code), str(lbl).strip())
            for code, lbl in biz_map.items()
            if str(lbl or "").strip() and not str(lbl).strip().endswith("（作废）")
        ]
        if not active_biz:
            return cls({})
        erp_pages = sorted(
            (p, op) for p, op in by_path.items()
            if p.startswith("/admin-api/erp/") and p.endswith("/page")
            and p != ledger_path and p not in writing_gets and "{" not in p
        )
        for hdr_path, hdr_op in erp_pages:
            raw_tag = str(hdr_op.get("tag") or "")
            raw_summary = str(hdr_op.get("summary") or "")
            hdr_text = f"{raw_tag} {raw_summary}"
            noun = _TAG_PREFIX.sub("", raw_tag).strip().rstrip("单").strip()
            matched = []
            for code, lbl in active_biz:
                if lbl in hdr_text or (len(noun) >= 4 and noun in lbl):
                    matched.append((code, lbl))
            if len(matched) == 1:
                code, _lbl = matched[0]
                coerced = _coerce_param(code, qparams.get("bizType"))
                edges[hdr_path] = [
                    S3BridgeEdge(
                        edge_id=f"s3:{hdr_path}->{ledger_path}:{coerced}",
                        target_path=ledger_path,
                        fixed_params=(("bizType", coerced),),
                    )
                ]
        return cls({k: tuple(v) for k, v in edges.items()})


def metric_columns(rows: list[dict], fields: tuple[str, ...] = ()) -> list[str]:
    """Identify numeric quantity columns (excluding ids, flags, codes, and timestamps)."""
    if not rows:
        return []
    sample_keys = list(dict.fromkeys(k for r in rows[:5] if isinstance(r, dict) for k in r))
    candidates = [k for k in fields if k in sample_keys] if fields else sample_keys
    out: list[str] = []
    for k in candidates:
        if _TIME_KEY.search(k) or _NOT_A_QUANTITY.search(k):
            continue
        vals = [r.get(k) for r in rows if isinstance(r, dict) and r.get(k) is not None]
        if vals and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            out.append(k)
    return out


def should_trigger_s3_probe(
    rows: list | None,
    *,
    fields: tuple[str, ...] = (),
    current_request: str = "",
    col_labels: dict[str, str] | None = None,
) -> str:
    """Trigger S3 only on empty rows or all-zero quantity metrics."""
    if rows is None:
        return ""
    if len(rows) == 0:
        return "empty_rows"
    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not dict_rows:
        return ""
    metrics = metric_columns(dict_rows, fields)
    if not metrics:
        return ""
    sums = {k: sum(float(r.get(k) or 0) for r in dict_rows) for k in metrics}
    if current_request and col_labels and not fields:
        matched = [
            k for k in metrics
            if len(str(col_labels.get(k) or "").strip()) >= 2
            and str(col_labels[k]).strip()[:2] in current_request
        ]
        if matched and all(sums[k] == 0.0 for k in matched):
            return "all_zero_metrics"
    if all(v == 0.0 for v in sums.values()):
        return "all_zero_metrics"
    return ""


def map_sibling_params(
    src_filters: dict,
    edge: S3BridgeEdge,
    sib_params_meta: list[dict],
    *,
    page_size: int = 100,
) -> dict | None:
    """Build compatible read-only query params for the sibling stock-record endpoint."""
    sib_published = {
        q["name"]: q for q in sib_params_meta
        if isinstance(q, dict) and q.get("in") == "query" and isinstance(q.get("name"), str)
    }
    out: dict = {}
    for k, v in (src_filters or {}).items():
        if k in sib_published:
            out[k] = v
    if not any(isinstance(v, list) and len(v) == 2 for v in out.values()):
        src_range = next(
            (v for v in (src_filters or {}).values()
             if isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) for x in v)),
            None,
        )
        if src_range is not None:
            for candidate_key in ("createTime", "inTime", "outTime", "orderTime", "bizTime"):
                if candidate_key in sib_published:
                    out[candidate_key] = list(src_range)
                    break
    for k, v in edge.fixed_params:
        if k in sib_published:
            out[k] = v
    if "pageNo" in sib_published:
        out["pageNo"] = 1
    if "pageSize" in sib_published:
        out["pageSize"] = page_size
    missing_req = [
        name for name, meta in sib_published.items()
        if meta.get("required") is True and name not in out
    ]
    if missing_req:
        return None
    return out


__all__ = [
    "S3BridgeEdge",
    "S3ErpStockRecordIndex",
    "map_sibling_params",
    "metric_columns",
    "should_trigger_s3_probe",
]
