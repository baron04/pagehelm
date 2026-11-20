"""Same-actor read-only reconciliation for indeterminate Yudao mutations."""
from __future__ import annotations

import re

from web_operation_agent.composition.ports import MutationReconciliation
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.state.identity import PrincipalContext, PrincipalError

from ..reads.client import YudaoClient, YudaoReadRefused, _matches_fenced_get, re_safe_native_path

_MISSING_RECORD = re.compile(r"不存在|未找到|找不到|not found", re.IGNORECASE)


class YudaoMutationReconciler:
    """Verify indeterminate Yudao mutations with same-actor fenced GET readback.

    Never issues non-GET requests and never re-dispatches a write.
    """

    def __init__(
        self,
        client: YudaoClient,
        identity,
        *,
        supported_tools: frozenset[str],
        writing_gets: frozenset[str] = frozenset(),
        application_id: str = "yudao",
    ) -> None:
        self._client = client
        self._identity = identity
        self._supported_tools = frozenset(supported_tools)
        self._writing_gets = frozenset(writing_gets)
        self._application_id = application_id

    @property
    def supported_tools(self) -> frozenset[str]:
        return self._supported_tools

    def _fenced_get(self, path: str, **params):
        if _matches_fenced_get(path, self._writing_gets):
            raise YudaoReadRefused(
                f"{path}: fenced writing GET refused",
                code=403,
                reason="fenced_writing_get",
            )
        with self._client.fenced_read_only(self._writing_gets):
            return self._client.get(path, **params)

    def reconcile(
        self,
        name: str,
        business_key: dict,
        *,
        evidence: dict,
        principal: PrincipalContext | None,
    ) -> MutationReconciliation:
        if name not in self._supported_tools:
            raise ValueError("mutation is absent from the reconciliation contract")
        self._identity.validate(principal, application_id=self._application_id)
        assert principal is not None
        if "yudao.read" not in principal.scopes:
            raise PrincipalError("reconciliation requires yudao.read scope")

        info = self._fenced_get("system/auth/get-permission-info") or {}
        user = info.get("user") if isinstance(info, dict) else None
        if (
            not isinstance(user, dict)
            or str(user.get("id") or "") != principal.actor_id
            or str(self._client.tenant) != principal.tenant_id
        ):
            raise PrincipalError("reconciliation client actor does not match principal")

        bk = business_key if isinstance(business_key, dict) else {}
        ev = evidence if isinstance(evidence, dict) else {}
        unverified_effect = ev.get("unverified_effect") or bk.get("unverified_effect")
        if not unverified_effect and isinstance(ev.get("effects"), list):
            for item in ev["effects"]:
                if isinstance(item, dict) and item.get("verified") is False:
                    unverified_effect = str(item.get("effect") or "postcondition")
                    break
        if unverified_effect:
            return MutationReconciliation(
                outcome="unknown",
                requires_human=True,
                reason=f"partial effect '{unverified_effect}' requires manual verification",
            )

        entity = str(ev.get("entity") or bk.get("entity") or "").strip("/")
        record_id = (
            ev.get("id") if ev.get("id") is not None
            else (ev.get("record_id") if ev.get("record_id") is not None else bk.get("id"))
        )
        expected = ev.get("expected") if isinstance(ev.get("expected"), dict) else bk.get("expected")
        before = ev.get("before") if isinstance(ev.get("before"), dict) else bk.get("before")

        if not entity or not re_safe_native_path(entity):
            return MutationReconciliation(
                outcome="unknown",
                requires_human=True,
                reason="missing or invalid entity path for readback",
            )
        if record_id is None or record_id == "":
            return MutationReconciliation(
                outcome="unknown",
                requires_human=True,
                reason="no confirmed record id is bound to this mutation; manual verification required",
            )
        if not isinstance(expected, dict) or not expected:
            return MutationReconciliation(
                outcome="unknown",
                requires_human=True,
                reason="no expected target fields are recorded for automated readback",
            )

        read_path = str(
            ev.get("read_path")
            or bk.get("read_path")
            or ("product/spu/get-detail" if entity == "product/spu" else f"{entity}/get")
        ).strip("/")
        composite_id = (
            ev.get("composite_id")
            if isinstance(ev.get("composite_id"), dict)
            else (bk.get("composite_id") if isinstance(bk.get("composite_id"), dict) else {})
        )
        id_param = str(ev.get("id_param") or bk.get("id_param") or "id")
        id_field = str(ev.get("id_field") or bk.get("id_field") or id_param)
        is_create = name.endswith("_create") or name == "create_record"
        try:
            record = self._fenced_get(read_path, **{**composite_id, id_param: record_id})
        except YudaoReadRefused as exc:
            if exc.reason == "fenced_writing_get":
                return MutationReconciliation(
                    outcome="unknown",
                    requires_human=True,
                    reason=f"readback endpoint /{read_path} is blocked by writing-GET fence",
                )
            if exc.code in (401, 403):
                raise PrincipalError("same-actor readback is not authorized") from exc
            if _MISSING_RECORD.search(exc.reason or str(exc)):
                if is_create:
                    return MutationReconciliation(
                        outcome="failed",
                        requires_human=False,
                        reason=f"same-actor GET {read_path}?id={record_id} confirmed record does not exist",
                    )
                return MutationReconciliation(
                    outcome="unknown",
                    requires_human=True,
                    reason=f"target record {record_id} not found during readback",
                )
            return MutationReconciliation(
                outcome="unknown",
                requires_human=True,
                reason=f"readback failed: {exc.reason or exc.code}",
            )
        except DataRefused as exc:
            return MutationReconciliation(
                outcome="unknown",
                requires_human=True,
                reason=f"readback unavailable: {exc}",
            )

        if record is None:
            if is_create:
                return MutationReconciliation(
                    outcome="failed",
                    requires_human=False,
                    reason=f"same-actor GET {read_path}?id={record_id} returned no record",
                )
            return MutationReconciliation(
                outcome="unknown",
                requires_human=True,
                reason=f"target record {record_id} returned empty during readback",
            )
        if name == "assign_record_relations" and isinstance(record, list):
            if len(expected) == 1 and isinstance(next(iter(expected.values())), list):
                wanted_list = next(iter(expected.values()))
                if {str(x) for x in record} == {str(x) for x in wanted_list}:
                    return MutationReconciliation(
                        outcome="completed",
                        requires_human=False,
                        reason=f"same-actor GET {read_path} confirmed relation set",
                    )
                if isinstance(before, dict) and len(before) == 1 and isinstance(next(iter(before.values())), list):
                    before_list = next(iter(before.values()))
                    if {str(x) for x in record} == {str(x) for x in before_list}:
                        return MutationReconciliation(
                            outcome="failed",
                            requires_human=False,
                            reason=f"same-actor GET {read_path} confirmed pre-mutation relation set",
                        )
            return MutationReconciliation(
                outcome="unknown",
                requires_human=True,
                reason=f"same-actor GET {read_path} did not confirm expected relation set",
            )
        if not isinstance(record, dict) or str(record.get(id_field)) != str(record_id):
            return MutationReconciliation(
                outcome="unknown",
                requires_human=True,
                reason="readback response did not match target record id",
            )
        def _field_matches(key: str, wanted) -> bool:
            actual = record.get(key)
            if isinstance(actual, list) and isinstance(wanted, list) and (
                key in {"contactIds", "businessIds"} or name == "assign_record_relations"
            ):
                return len(actual) == len(wanted) and set(actual) == set(wanted)
            return actual == wanted

        if all(_field_matches(k, v) for k, v in expected.items()):
            return MutationReconciliation(
                outcome="completed",
                requires_human=False,
                reason=f"same-actor GET {read_path}?id={record_id} confirmed all expected fields",
            )
        if isinstance(before, dict) and before and all(record.get(k) == v for k, v in before.items()):
            return MutationReconciliation(
                outcome="failed",
                requires_human=False,
                reason=f"same-actor GET {read_path}?id={record_id} confirmed record retains pre-mutation values",
            )
        return MutationReconciliation(
            outcome="unknown",
            requires_human=True,
            reason=f"same-actor GET {read_path}?id={record_id} neither matched expected values nor confirmed pre-mutation values",
        )


__all__ = ["YudaoMutationReconciler"]
