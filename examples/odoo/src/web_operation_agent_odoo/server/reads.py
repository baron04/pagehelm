"""Typed Odoo reads, form preparation, and guarded JSON-RPC writes."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, time as time_of_day, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.answer.evidence import Origin, QueryEvidence, RecordEvidence
from web_operation_agent.business.runtime import (APPLICATION_SAVES_ON_LEAVE,
                                                  BusinessMutationRefusedError,
                                                  BusinessSelectionOption,
                                                  BusinessToolContractError,
                                                  BusinessToolError,
                                                  BusinessToolRequestError,
                                                  BusinessToolResult,
                                                  FormDraft, FormDraftField,
                                                  business_text_key,
                                                  normalize_business_text)
from web_operation_agent.state.identity import OperationContext

from .specs import (APPLICATION_ID, EDITABLE_CUSTOMER_FIELDS,
                    PREPARE_CUSTOMER_EDIT_TOOL,
                    PREPARE_CUSTOMER_FORM_SCHEMA, PREPARE_CUSTOMER_FORM_TOOL,
                    QUERY_SCHEMAS, _BY_TOOL, _CUSTOMERS, _LABEL_CHARS,
                    _MAX_PAGES, _PAGE_SIZE)


_UTC_DATETIME_FIELDS = frozenset({"date_order"})


def _in_zone(value: str, zone: str) -> str:
    """One stored UTC datetime, as the application's own screens would show it."""
    if not zone:
        return value
    try:
        moment = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
    local = moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone))
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _utc_boundary(day: date, zone: str) -> str:
    """The person's midnight, as the UTC datetime Odoo compares against."""
    if not zone:
        return f"{day.isoformat()} 00:00:00"
    local = datetime.combine(day, time_of_day(), ZoneInfo(zone))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class OdooReadTools:
    """Typed collection reads and one-turn form preparation."""

    @staticmethod
    def _identity_audit(context: OperationContext | None) -> dict:
        if context is None:
            return {"identity_binding": "unverified"}
        return {"identity_binding": "principal_bound",
                "identity": context.audit_projection()}

    # -- reads --------------------------------------------------------------------------
    def _read_args(self, name: str, args: dict) -> tuple[str, int, list]:
        unknown = set(args) - set(QUERY_SCHEMAS[name]["function"]["parameters"]["properties"])
        if unknown:
            raise BusinessToolRequestError(f"{name} received unknown arguments")
        needle = args.get("name_contains", "")
        if (not isinstance(needle, str) or len(needle) > 100
                or re.search(r"[\x00-\x1f\x7f]", needle)):
            raise BusinessToolRequestError(
                "name_contains must be a string of at most 100 characters")
        limit = args.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BusinessToolRequestError("limit must be an integer from 1 to 50")
        domain = []
        if "record_id" in args:
            record_id = args["record_id"]
            if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id < 1:
                raise BusinessToolRequestError("record_id must be a positive integer")
            domain.append(["id", "=", record_id])
        if "customer_id" in args:
            customer_id = args["customer_id"]
            if isinstance(customer_id, bool) or not isinstance(customer_id, int) or customer_id < 1:
                raise BusinessToolRequestError("customer_id must be a positive integer")
            domain.append(["partner_id", "=", customer_id])
        dates = {}
        for key, operator in (("date_from", ">="), ("date_until", "<")):
            if key not in args:
                continue
            value = args[key]
            try:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError()
                dates[key] = date.fromisoformat(value)
            except ValueError:
                raise BusinessToolRequestError(f"{key} must be a valid YYYY-MM-DD date") from None
        if len(dates) == 2 and dates["date_from"] >= dates["date_until"]:
            raise BusinessToolRequestError("date_from must precede the exclusive date_until")
        # Only now, with every argument accepted, may this reach the application: asking it
        # for the user's zone is a call, and a refused argument must make none.
        field = "date_order" if _BY_TOOL[name].model == "sale.order" else "invoice_date"
        for key, operator in (("date_from", ">="), ("date_until", "<")):
            if key not in dates:
                continue
            if field == "date_order":
                # A day boundary is the person's midnight, not the server's: orders "from
                # the 15th" in Shanghai must not include the one placed at 16:30 UTC on the
                # 14th, which is 00:30 on the 15th where they are.
                domain.append([field, operator, _utc_boundary(dates[key], self.time_zone())])
            else:
                domain.append([field, operator, dates[key].isoformat()])
        return needle.strip(), limit, domain

    def _scan(self, collection: _Collection, extra_domain: list | None = None
              ) -> tuple[list[dict], int, dict]:
        """Read the whole scoped collection, and say plainly whether the scan finished.

        `search_count` under the same actor and the same domain is the completeness oracle:
        a truthful "I saw all of them" needs a number that did not come from the same page
        of results being counted.
        """
        domain = [list(clause) for clause in collection.domain] + (extra_domain or [])
        total = self.call(collection.model, "search_count", [domain])
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise BusinessToolContractError("Odoo returned an invalid record count")
        rows: list[dict] = []
        seen: set[int] = set()
        pages = 0
        while pages < _MAX_PAGES and len(rows) < total:
            page = self.call(collection.model, "search_read", [domain],
                             {"fields": list(collection.fields), "limit": _PAGE_SIZE,
                              "offset": len(rows), "order": "id"})
            if not isinstance(page, list):
                raise BusinessToolContractError("Odoo returned an invalid record page")
            pages += 1
            for row in page:
                record_id = row.get("id") if isinstance(row, dict) else None
                if (isinstance(record_id, bool) or not isinstance(record_id, int)
                        or record_id <= 0 or record_id in seen):
                    raise BusinessToolContractError("Odoo returned a missing or repeated record identity")
                seen.add(record_id)
                rows.append(row)
            if not page:
                break
        complete = len(rows) == total
        return rows, total, {
            "pages_scanned": pages,
            "records_scanned": len(rows),
            "complete": complete,
            "incomplete_reason": "" if complete else "page_budget_exhausted",
        }

    def _label(self, row: dict, field: str) -> str:
        value = row.get(field)
        if field in _UTC_DATETIME_FIELDS and isinstance(value, str) and value:
            return _in_zone(value, self.time_zone())
        if isinstance(value, list) and len(value) == 2:
            value = value[1]
        if value is False or value is None:
            return ""
        if isinstance(value, float):
            numeric = Decimal(str(value))
            if not numeric.is_finite():
                raise BusinessToolContractError("Odoo returned a non-finite number")
            return format(numeric, f".{max(2, -numeric.as_tuple().exponent)}f")
        text = str(value)
        # A value longer than the display bound is shown shortened, and says so. It was cut
        # to its first 300 characters in silence, and the answer then presented that prefix
        # as the field's value -- a claim about the record the record does not support.
        if len(text) > _LABEL_CHARS:
            return text[:_LABEL_CHARS] + "…"
        return text

    @staticmethod
    def _currency(row: dict) -> str:
        # sale.order uses its pricelist's currency, account.move its invoice currency,
        # and product.template exposes the catalog currency. A company's currency is
        # not a substitute for any of these record-level denominators.
        value = row.get("currency_id")
        if (not isinstance(value, list) or len(value) != 2
                or isinstance(value[0], bool) or not isinstance(value[0], int) or value[0] <= 0
                or not isinstance(value[1], str) or not value[1].strip()
                or len(value[1]) > 32 or re.search(r"[\x00-\x1f\x7f]", value[1])):
            raise BusinessToolContractError("Odoo returned money without its record currency")
        return value[1].strip()

    _NUMERIC_FIELDS = {"amount_total": "money", "list_price": "money", "amount_residual": "money"}
    #: Dates and timestamps Odoo returns in ISO-8601. Typed so a question about a period can
    #: be answered by the server's own comparison instead of the model sorting text by eye.
    _MOMENT_FIELDS = {"date_order": "datetime", "invoice_date": "date",
                      "invoice_date_due": "date", "validity_date": "date"}
    _FIELD_MEANINGS = {
        "date_order": "order confirmation time / quotation creation time",
        "invoice_date": "invoice calendar date", "invoice_date_due": "invoice due date",
        "validity_date": "quotation expiry date", "amount_total": "total including taxes",
        "amount_residual": "invoice amount due", "list_price": "catalog sales price",
    }

    def _evidence(self, collection: _Collection, pairs, *, name: str, observed_at: str,
                  matched: int, scanned, whole: bool, conditions: tuple[str, ...],
                  looked_for: str = "", count_exact: bool = False,
                  exact_matches: int | None = None) -> tuple:
        """Turn what this read returned into evidence an answer can cite.

        The projection beside it stays a display view. This is the same read expressed so a
        conclusion can stand on it: the record's own identity, the field's own name, whether
        the field was actually read, and -- for a number -- what it is denominated in.
        """
        reference = f"{name}#{len(pairs)}"
        origin = Origin(kind="typed", reference=reference, observed_at=observed_at,
                        scope=collection.scope + "; " + "; ".join(conditions))
        items = [QueryEvidence(
            evidence_id="query#1", origin=origin, collection=collection.tool.removeprefix("query_"),
            conditions=conditions,
            matched_count=matched, records_scanned=scanned, covers_whole_scope=whole,
            looked_for=looked_for, count_exact=count_exact, exact_matches=exact_matches,
            summable_fields=tuple(field for field in collection.fields
                                  if field in self._NUMERIC_FIELDS))]
        index = 0
        for row, _item in pairs:
            record_id = row.get("id")
            if not isinstance(record_id, int):
                # Without the application's own identity there is nothing stable to cite,
                # and a row number would be an identity this invented.
                continue
            for field_name in collection.fields:
                value_type = self._NUMERIC_FIELDS.get(
                    field_name, self._MOMENT_FIELDS.get(field_name, "text"))
                raw = row.get(field_name)
                index += 1
                items.append(RecordEvidence(
                    evidence_id=f"record#{index}", origin=origin,
                    record_id=f"{collection.model}/{record_id}",
                    field_name=field_name,
                    field_meaning=self._FIELD_MEANINGS.get(field_name, field_name.replace("_", " ")),
                    field_was_read=field_name in row,
                    value=(self._label(row, field_name) if field_name in row
                           and raw is not False and raw is not None else None),
                    value_type=value_type if field_name in row else "",
                    unit=self._currency(row) if value_type == "money" and field_name in row else ""))
        return tuple(items)

    def _project(self, collection: _Collection, row: dict) -> dict:
        record_id = row.get("id")
        item = {}
        for output, source in collection.projection_fields:
            value = self._label(row, source)
            # A draft `account.move` has no number. The descriptor supplies the honest
            # display fallback; no query-specific control branch decides it here.
            item[output] = value or collection.empty_value_labels.get(output, "")
        if not isinstance(record_id, bool) and isinstance(record_id, int) and record_id > 0:
            item["ui_path"] = f"{collection.route}/{record_id}"
        return item

    @staticmethod
    def _selection_option(collection: _Collection, row: dict,
                          item: dict) -> BusinessSelectionOption | None:
        record_id = row.get("id")
        if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0:
            return None
        values = [str(item.get(field) or "").strip()
                  for field in collection.selection_fields]
        values = [value for value in values if value]
        if not values:
            return None
        return BusinessSelectionOption(
            record_ref=f"{collection.model}/{record_id}",
            label=" — ".join(values)[:200],
        )

    def execute(self, name: str, args: dict, *,
                context: OperationContext | None = None) -> BusinessToolResult:
        if name == PREPARE_CUSTOMER_FORM_TOOL:
            normalized = self._normalize_customer(args)
            self._assert_no_duplicate(normalized["name"], normalized["email"])
            route = "/odoo/customers/new"
            draft = FormDraft(
                route=route,
                fields=(
                    FormDraftField("name", "客户名称", normalized["name"], "combobox"),
                    FormDraftField("email", "邮箱", normalized["email"]),
                ),
            )
            observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return BusinessToolResult(
                content={
                    "form_ui_path": route,
                    "form_fields": [item.to_json() for item in draft.fields],
                    "persistence": draft.persistence,
                    "submission": "user_only",
                    "observed_at": observed_at,
                },
                audit={
                    "application": APPLICATION_ID,
                    "operation": PREPARE_CUSTOMER_FORM_TOOL,
                    "field_count": len(draft.fields),
                    "persistence": draft.persistence,
                    "observed_at": observed_at,
                    **self._identity_audit(context),
                },
                allowed_routes=(route,),
                form_draft=draft,
            )
        if name == PREPARE_CUSTOMER_EDIT_TOOL:
            return self._prepare_customer_edit(args, context)
        collection = _BY_TOOL.get(name)
        if collection is None:
            raise BusinessToolRequestError("unknown Odoo business tool")
        needle, limit, extra_domain = self._read_args(name, args)
        field = collection.match_field
        # Let Odoo's indexed/domain query discard unrelated rows before pagination. Keep the
        # literal Python comparison below as the final semantic check. PostgreSQL LIKE uses
        # backslash escaping, so user `%` and `_` remain ordinary name characters here.
        scan_domain = list(extra_domain)
        if needle:
            escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            # `field` names the projection key ("reference" for orders and invoices). Odoo
            # stores that value in each model's `name`; `sale.order.reference` is the payment
            # reference and `account.move` has no `reference` at all.
            scan_domain.append(["name", "ilike", escaped])
        rows, total, scan = self._scan(collection, scan_domain)
        conditions = tuple(f"{p[0]} {p[1]} {p[2]}" for p in [*collection.domain, *extra_domain])
        if needle:
            conditions += (f"name case-insensitive substring {needle!r}",)
        conditions = conditions or ("no filter",)
        page_matches = not collection.route_scope_note and not needle and not extra_domain
        # Kept paired with the row it came from: the projection is a display view -- labels
        # for people -- while evidence needs the record's own identity, and re-deriving one
        # from the other after filtering is how they drift apart.
        pairs = [(row, self._project(collection, row)) for row in rows]
        folded = needle.casefold().strip()
        if folded:
            pairs = [pair for pair in pairs if folded in pair[1][field].casefold()]
        matched = len(pairs)
        visible_pairs = pairs[:limit]
        visible = [item for _row, item in visible_pairs]
        exact_match = bool(
            folded and matched == 1
            and business_text_key(str(visible[0].get(field, "")))
            == business_text_key(needle))
        # A read narrowed to one record -- by a name that matched it alone, or by its own id --
        # is the record the user asked for. Two customers of the same name are reachable only
        # by the second: no name singles either of them out.
        by_record_id = any(term[0] == "id" for term in extra_domain)
        unique_match = bool((folded or by_record_id) and matched == 1)
        # A name search matches parts of names; how many of the matches carry the whole name
        # is what an answer needs before it may say that a record of that name exists.
        exact_matches = (sum(1 for _row, item in pairs
                             if business_text_key(str(item.get(field, "")))
                             == business_text_key(needle)) if folded else None)
        selection_options = tuple(
            option for row, item in visible_pairs
            if (option := self._selection_option(collection, row, item)) is not None)
        # Opening a record is a reversible read. A single current match is therefore enough
        # to navigate, even when the user supplied a prefix such as "Acme". More than one
        # current match remains a choice; the user must decide which record they meant.
        record_routes = tuple(
            item["ui_path"] for item in visible if isinstance(item.get("ui_path"), str))
        allowed_routes = ((collection.route,) + record_routes
                          if not folded or unique_match else (collection.route,))
        source_complete = scan["complete"]
        result_truncated = matched > limit or not source_complete
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        completeness = {
            "source_total_known": True,
            "source_complete": source_complete,
            "result_truncated": result_truncated,
            "pages_scanned": scan["pages_scanned"],
            "records_scanned": scan["records_scanned"],
            "incomplete_reason": scan["incomplete_reason"],
        }
        return BusinessToolResult(
            content={
                collection.tool.removeprefix("query_"): visible,
                "collection_ui_path": collection.route,
                # Whether that page shows this exact set. Only two of the five collections
                # can promise it, and the ones that cannot say so here rather than leaving
                # the model to assume the page and the answer agree.
                "collection_ui_path_shows_this_scope": page_matches,
                **({"collection_ui_path_note": collection.route_scope_note}
                   if collection.route_scope_note else {}),
                "matched_count": matched,
                "source_total": total,
                "scope": collection.scope,
                "conditions": list(conditions),
                "record_route_resolution": (
                    "exact" if exact_match else
                    "unique" if unique_match else
                    "confirmation_required" if folded and matched > 1 else
                    "no_match" if folded else "not_requested"),
                "consistency": "bounded scan, not a database snapshot",
                "observed_at": observed_at,
                "completeness": completeness,
            },
            audit={
                "application": APPLICATION_ID,
                "operation": name,
                "model": collection.model,
                "matched_count": matched,
                "returned_count": len(visible),
                "source_total": total,
                "scope": collection.scope,
                "collection_ui_path_shows_this_scope": page_matches,
                "observed_at": observed_at,
                **completeness,
                **self._identity_audit(context),
            },
            allowed_routes=allowed_routes,
            evidence=self._evidence(collection, visible_pairs, name=name,
                                    observed_at=observed_at, matched=matched,
                                    scanned=scan["records_scanned"],
                                    whole=source_complete and not result_truncated,
                                    # The scan read every matching record and search_count
                                    # agreed, so the count is exact however few rows the
                                    # limit then let through.
                                    count_exact=source_complete,
                                    conditions=conditions, looked_for=needle,
                                    exact_matches=exact_matches),
            selection_options=selection_options,
            navigation_target=(record_routes[0] if unique_match else ""),
            query_answer=StructuredQueryAnswer(
                collection=collection.tool.removeprefix("query_"),
                fields=collection.answer_fields,
                records=tuple(tuple(item.get(key, "") for key in collection.answer_fields)
                              for item in visible),
                source=f"Odoo delegated JSON-RPC {collection.model}",
                scope=collection.scope,
                observed_at=observed_at,
                source_complete=source_complete,
                result_truncated=result_truncated,
                matched_count=matched,
                source_total=total,
                records_scanned=scan["records_scanned"],
                incomplete_reason=scan["incomplete_reason"],
            ),
        )

    # -- customer creation ---------------------------------------------------------------
    def _prepare_customer_edit(self, args: dict,
                               context: OperationContext | None) -> BusinessToolResult:
        """Stage an edit to one existing customer on that customer's own form.

        Nothing is written here and nothing is typed here: this reads the record under the
        delegated identity -- which is also the permission check -- and issues the draft the
        server will show as a confirmation and then fill. Odoo saves an open record form
        when the page is left, so the draft says so and the person is asked first.
        """
        if not isinstance(args, dict) or not args:
            raise BusinessToolRequestError(
                f"{PREPARE_CUSTOMER_EDIT_TOOL} requires a record_id and at least one field")
        unknown = set(args) - ({"record_id"} | set(EDITABLE_CUSTOMER_FIELDS))
        if unknown:
            raise BusinessToolRequestError(
                "this deployment can prepare only "
                + "、".join(sorted(EDITABLE_CUSTOMER_FIELDS)) + " on a customer")
        record_id = args.get("record_id")
        if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id < 1:
            raise BusinessToolRequestError("record_id must be an exact positive id")
        changes: dict[str, str] = {}
        for field in EDITABLE_CUSTOMER_FIELDS:
            if field not in args:
                continue
            if field == "email":
                value = self._normalize_email(args.get("email"))
            else:
                value = normalize_business_text(
                    args.get(field), field=f"customer {field}",
                    maximum=200 if field == "name" else 64)
            changes[field] = value
        if not changes:
            raise BusinessToolRequestError(
                "name it with at least one field to change")
        rows, _total, _scan = self._scan(_CUSTOMERS, [["id", "=", record_id]])
        if len(rows) != 1:
            raise BusinessToolRequestError(
                "that customer is not one this account can read")
        row = rows[0]
        # Nothing to prepare is worth saying rather than staging a change that changes
        # nothing and asking the person to confirm it.
        same = [field for field, value in changes.items()
                if self._label(row, field) == value]
        if len(same) == len(changes):
            raise BusinessMutationRefusedError(
                "the customer already holds these values", reason="unchanged")
        route = f"/odoo/customers/{record_id}"
        label = self._label(row, "name") or f"res.partner/{record_id}"
        fields = tuple(
            FormDraftField(field, EDITABLE_CUSTOMER_FIELDS[field][0], value,
                           EDITABLE_CUSTOMER_FIELDS[field][1])
            for field, value in changes.items())
        targets = tuple(
            f"res.partner · {label} · {EDITABLE_CUSTOMER_FIELDS[field][0]}："
            f"{self._label(row, field) or '（空）'} → {value}"
            for field, value in changes.items())
        draft = FormDraft(
            route=route, fields=fields, persistence=APPLICATION_SAVES_ON_LEAVE,
            effect=(f"在 Odoo 中为客户 {label} 填好 {len(changes)} 个字段的新值（由你保存；"
                    "离开该页面时 Odoo 会保存）")[:300],
            targets=targets)
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return BusinessToolResult(
            content={
                "form_ui_path": route,
                "form_fields": [item.to_json() for item in draft.fields],
                "persistence": draft.persistence,
                "submission": "user_only",
                "record_id": record_id,
                "current_values": {field: self._label(row, field) for field in changes},
                "observed_at": observed_at,
            },
            audit={
                "application": APPLICATION_ID,
                "operation": PREPARE_CUSTOMER_EDIT_TOOL,
                "record_id": record_id,
                "field_count": len(draft.fields),
                "persistence": draft.persistence,
                "observed_at": observed_at,
                **self._identity_audit(context),
            },
            allowed_routes=(route,),
            form_draft=draft,
        )
