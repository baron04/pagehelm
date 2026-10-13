"""A Skill declaration executes within the existing goal turn, budget and audit."""
from __future__ import annotations
from dataclasses import replace

from ..business.errors import (BusinessMutationIndeterminate, BusinessToolError,
    BusinessToolPermissionError, BusinessToolReferenceError,
    BusinessMutationBlockedError, BusinessMutationRefusedError, BusinessToolRequestError)
from ..business.write_service import BusinessWriteService
from ..business.effects import canonical
from ..skills import SkillContext
from .skill_plan import declare_plan, bind_references, bind_goal_references
from .spec import GoalSpec
from .ground import ground, days, render as render_values
from .narrow import KnownReference, locate_names, neighbourhood
from .ports import capabilities_of
from .complete_task import declare_complete_task
from .skill_preflight import CorrectionBudget, preflight_plan
from .source_scope import scope_page_keys


def skills_for_scope(operations, parts, *, limit=4):
    """Choose knowledge from reviewed application identities after ordinary scope.

    A knowledge-only package can declare entities/pages independently of tools.
    Write supporters also inherit their tools' reviewed entity identities.
    Neither selection nor installation grants a capability or write authority.
    """
    entities = {part.get("entity") for part in parts if part.get("entity")}
    pages = set(scope_page_keys(parts))
    tools = {definition.name: definition for definition in operations.tools.definitions}
    return tuple(skill.skill_id for skill in operations.skills.artifacts
        if entities.intersection(skill.scope_entities) or pages.intersection(skill.scope_pages)
        or any(name in tools and entities.intersection(tools[name].scope_entities)
               for name in skill.write_operations))[:limit]


def load_skill_context(executor, state, skill_ids):
    """Load knowledge independently of the decision to propose business writes."""
    s = state
    operations = executor.application.ports.operations
    context = SkillContext(operations.skills)
    for sid in skill_ids:
        skill = context.load(sid)
        s.ledger.append("business_skill_loaded", step=s.step, skill_id=sid,
                        skill_sha256=skill.artifact_sha256, side_effect_possible=False)
    return context


def _reference_clarification(state, choices):
    chinese = state.chinese
    labels = []
    for number, display in enumerate(choices, start=1):
        labels.append({"choice_id": str(number), "label": display[:200],
                       "record_ref": display[:500]})
    numbered = "\n".join(f"{item['choice_id']}. {item['label']}" for item in labels)
    question = (("无法唯一定位目标记录，请选择：\n" + numbered + "\n回复编号或完整名称。")
                if chinese else
                ("The target could not be resolved. Which record should I use?\n" +
                 numbered + "\nReply with the number or the complete name."))
    state.clarification = {"schema_version": 1,
        "clarification_id": f"{state.run_id}-reference"[:100],
        "task_id": state.run_id[:100], "question": question[:500],
        "response_kind": "single_choice", "choices": labels}


def execute_skill_plan(executor, state, skill_ids, *, context=None, native_proposals=False,
                       allow_yield=False):
    s = state
    operations = executor.application.ports.operations
    context = context if context is not None else load_skill_context(executor, s, skill_ids)
    supported = {name for skill in context.loaded for name in skill.write_operations}
    principal = s.approvals.context(s.step, "catalog")
    schemas = executor.application.business_tools.available_schemas(
        allow_mutations=s.approvals.mutations_enabled,
        actor_scopes=principal.principal.scopes if principal is not None else (),
        writes=frozenset(supported), loaded_ids=set(skill_ids))
    available = {schema["function"]["name"] for schema in schemas}
    definitions = tuple(d for d in operations.tools.definitions
                        if d.effect == "write" and d.name in available
                        and (native_proposals or s.write_intent or any(p.get("kind") == "handoff" and not p.get("rules")
                                                  for p in s.scope_parts)))
    complete_request = any(d.effect_contract is not None for d in definitions)
    # Reuse the actor-scoped grounding port before the task declaration.
    # Skills do not supply current records or disambiguate names.
    # A compound operation may be scoped to its record while referring to
    # several other entities. Read candidates on the reviewed scopes of only
    # the tools actually offered to this actor; a Skill never supplies records.
    entities = tuple(dict.fromkeys((
        *(p["entity"] for p in s.scope_parts if p.get("entity")),
        *(entity for definition in definitions for entity in definition.scope_entities),
    )))
    planning_index = s.index
    if complete_request:
        focused = neighbourhood(s.full_index, entities, request=s.message.text, rules=False,
                                page_keys=scope_page_keys(s.scope_parts))
        # Reuse neighborhood selection, but keep ALL business fields of its
        # entities, including held/unwritable fields, for independent intent.
        if focused.entities:
            planning_index = replace(focused, entities=tuple(
                replace(s.full_index.entity(e.key), rules=()) for e in focused.entities))
    names = tuple(dict.fromkeys(n for p in s.scope_parts for n in p.get("names", ())))
    if names:
        with s.receipt.stage("business"):
            found = locate_names(executor.goal_port, names, entities=entities,
                context=s.approvals.context(s.step, "locate"))
        s.references = tuple(s.references) + found
    if executor.values and "values" in capabilities_of(executor.goal_port):
        # Lookup hints are not unique references.  Offer unresolved scoped names
        # to the existing candidate port as well, so ambiguity can be shown to the
        # planner without claiming that a record has already been selected.
        hints = tuple(KnownReference(n, entity, n) for entity in entities for n in names
            if not any(k.text == n and k.entity == entity for k in s.references))
        with s.receipt.stage("business"):
            s.candidates = ground(executor.goal_port, entities=entities, known=(*s.references, *hints),
                dates=days(s.message.text, s.today), context=s.approvals.context(s.step, "candidates"))
    def resource_loaded(skill, path, text):
        import hashlib
        s.ledger.append("business_skill_resource_loaded", step=s.step,
            skill_id=skill.skill_id, skill_sha256=skill.artifact_sha256,
            resource=path, resource_sha256=hashlib.sha256(text.encode()).hexdigest(),
            side_effect_possible=False)
    def declaration_invalid(error_type, detail, retry):
        s.ledger.append("skill_declaration_invalid", error_type=error_type,
            detail=detail[:200], correction_allowed=retry, side_effect_possible=False)
    def capability_rechecked():
        s.ledger.append("skill_capability_rechecked", correction_allowed=True,
                        side_effect_possible=False)
    def intent_rechecked():
        s.ledger.append("skill_intent_rechecked", correction_allowed=True,
                        side_effect_possible=False)
    def fields_reviewed(requested, unrequested, plan_reused):
        s.ledger.append("skill_fields_reviewed", requested=requested,
            unrequested=unrequested, plan_reused=plan_reused, side_effect_possible=False)
    def task_reconciled(errors):
        s.ledger.append("skill_task_reconciled", disagreement_count=len(errors),
                        correction_allowed=True, side_effect_possible=False)
    service = BusinessWriteService(executor.application.business_tools, s.approvals, set(skill_ids))
    def read_tool(name, args):
        s.budget.check()
        read_context = s.approvals.context(s.step, name)
        with s.receipt.stage("business"):
            result = executor.application.business_tools.execute(name, args, set(skill_ids), context=read_context)
        s.ledger.append("skill_planning_read", step=s.step, tool=name,
                        result_digest=result.content_digest, side_effect_possible=False)
        return result.content
    def preflight(plan):
        def prepare(position, step):
            op_context = s.approvals.context(s.step + position + 2, step["operation"],
                                            hold_ms=s.approvals.approval_hold_ms)
            with s.receipt.stage("business"):
                return service.prepare(step["operation"], step["inputs"], context=op_context,
                                       skill_id=step["skill_id"])
        def check_goal(goal):
            from .executor import _needs_beyond
            missing = set().union(*(_needs_beyond(deliverable, capabilities_of(executor.goal_port))
                                   for deliverable in goal.deliverables))
            if missing:
                raise BusinessToolRequestError("application does not offer requested capabilities: " +
                                               ", ".join(sorted(missing)))
        preflight_plan(plan, definitions, prepare=prepare, check_goal=check_goal,
            recover_references=executor.complete_request_check == "single" and any(
                schema["function"]["name"] in executor.application.business_tools.read_tool_names
                for schema in schemas))
        s.ledger.append("skill_plan_preflight", steps=len(plan["steps"]),
                        side_effect_possible=False)
    try:
        with s.receipt.stage("interpret"):
            if complete_request:
                from .complete_task import declare_single_task, declare_complete_task
                declarer = (declare_single_task if executor.complete_request_check == "single"
                            else declare_complete_task)
                callbacks = ({"correction_budget": CorrectionBudget(), "preflight": preflight,
                              "declaration_invalid": declaration_invalid,
                              "read_tools": tuple(schema for schema in schemas
                                  if schema["function"]["name"] in executor.application.business_tools.read_tool_names),
                              "read_tool": read_tool}
                             if executor.complete_request_check == "single" else
                             {"futures": s.model_futures, "reconciled": task_reconciled,
                              "candidates": s.candidates})
                if allow_yield:
                    callbacks["allow_yield"] = True
            else:
                declarer = declare_plan
                callbacks = {"declaration_invalid": declaration_invalid, "capability_rechecked": capability_rechecked,
                             "intent_rechecked": intent_rechecked, "fields_reviewed": fields_reviewed}
            plan = declarer(s.chat, request=s.message.text, index=planning_index, context=context,
                definitions=definitions, run_id=s.run_id,
                remaining=s.budget.remaining, cancelled=s.cancelled,
                conversation=executor.conversation_state.model_context(), resource_loaded=resource_loaded,
                **callbacks,
                today=s.today, time_zone=executor.config.time_zone,
                current_values=render_values(s.candidates), scope={"parts": s.scope_parts,
                    "write_intent": s.write_intent,
                    "unsupported": s.unsupported, "known_references": [
                        {"text": k.text, "entity": k.entity, "name": k.name} for k in s.references]})
            if allow_yield and (plan.get("unsupported") or not (
                    plan.get("question") or any(p["kind"] == "operation" for p in plan["steps"]))):
                from .skill_admission import SkillProposalYield
                raise SkillProposalYield(plan.get("unsupported", ""))
            if complete_request and executor.complete_request_check != "single":
                preflight(plan)
    except BusinessToolPermissionError as exc:
        s.terminal_reason = "permission_denied"
        s.ledger.append("skill_preflight_refused", error_code=exc.code, side_effect_possible=False)
        return "当前账号没有执行这项操作的权限。" if s.chinese else "The current actor cannot perform this operation."
    except BusinessToolReferenceError as exc:
        if exc.choices and not s.mutation_outcomes:
            _reference_clarification(s, exc.choices)
            s.terminal_reason = "needs_clarification"
            return s.clarification["question"]
        s.terminal_reason = "agent_refused"
        return str(exc)
    except (ValueError, BusinessToolError) as exc:
        s.terminal_reason = "agent_refused"
        s.ledger.append("skill_declaration_refused", error_type=type(exc).__name__,
                        error_code=getattr(exc, "code", "skill_declaration"), side_effect_possible=False)
        return "未能完整声明这项任务，尚未执行业务写入。" if s.chinese else "The task declaration was invalid; no business write ran."
    s.step += 1
    if native_proposals:
        # A complete proposal supersedes an incomplete routing declaration.
        # Do not repair its later reads with the ordinary interpreter's scope.
        s.unsupported = plan.get("unsupported", "")
        s.dropped = ()
        s.read = None
    s.write_intent = plan["intent"] == "execute" and any(p["kind"] == "operation" for p in plan["steps"])
    s.ledger.append("skill_task_declared", intent=plan["intent"], steps=len(plan["steps"]),
                    operations=[p["operation"] for p in plan["steps"] if p["kind"] == "operation"])
    if s.write_intent:
        s.ledger.append("skill_inputs_bound", side_effect_possible=False,
            operations=sum(p["kind"] == "operation" for p in plan["steps"]))
    if plan.get("question"):
        s.terminal_reason = "needs_clarification"
        s.clarification = {"schema_version": 1, "clarification_id": f"{s.run_id}-skill"[:100],
            "task_id": s.run_id[:100], "question": plan["question"][:500], "response_kind": "free_text", "choices": []}
        return plan["question"]
    if (plan.get("unsupported") or (s.unsupported and not complete_request)) and any(p["kind"] == "operation" for p in plan["steps"]):
        # A known unmet constraint must be resolved before creating a lesser
        # business effect. Runtime failures later still retain completed receipts.
        s.terminal_reason = "agent_refused"
        return plan.get("unsupported") or s.unsupported
    completed, outcomes, effect_position = [], {}, 0
    for claim in plan.get("knowledge", []):
        completed.append(claim["span"])
    if plan.get("unsupported"):
        completed.append(plan["unsupported"])
    for position, step in enumerate(plan["steps"]):
        s.budget.check()
        s.step += 1
        if step["kind"] == "operation":
            if not s.approvals.mutations_enabled:
                s.terminal_reason = "agent_refused"
                completed.append("当前部署未启用业务写入。" if s.chinese else "Business writes are disabled in this deployment.")
                break
            op_context = s.approvals.context(s.step, step["operation"], hold_ms=s.approvals.approval_hold_ms)
            identity = op_context.audit_projection() if op_context else {}
            prepared = False
            try:
                definition = operations.tools.get(step["operation"])
                inputs = bind_references(step["inputs"], outcomes, definition.reference_inputs)
                with s.receipt.stage("business"):
                    proposal = service.prepare(step["operation"], inputs, context=op_context, skill_id=step["skill_id"])
                    prepared = True
                    if complete_request:
                        if proposal.effect_projection is None:
                            raise BusinessToolError("application did not project its prepared effect")
                        # Dependency names are refreshed only from verified receipts.
                        expected = plan["locked_effects"][effect_position]
                        reference_fields = tuple(field for path, _, field in definition.input_fields
                                                 if path in definition.reference_inputs)
                        expected = replace(expected, target=bind_references(
                            {"target": expected.target}, outcomes, ("target",))["target"],
                            values_json=canonical(bind_references(expected.values, outcomes,
                                                                  reference_fields)))
                        proposal.effect_projection.require(expected)
                        s.ledger.append("skill_effect_bound", step=s.step, tool=step["operation"],
                            proposal_digest=proposal.content_digest, side_effect_possible=False)
                    settlement = service.settle(proposal, step=s.step, context=op_context, identity=identity)
                if not settlement.approval.approved:
                    s.terminal_reason = settlement.approval.reason
                    completed.append("后续操作已停止，未派发本次修改。" if s.chinese else "Further operations stopped; this change was not dispatched.")
                    break
                result = settlement.result
                if complete_request:
                    effect_position += 1
                outcomes[position] = result.content
                s.mutation_outcomes.append({"position": position, "effect": proposal.effect,
                    "targets": list(proposal.targets), "verified": True, "receipt": result.content})
                s.ledger.append("business_mutation_result", step=s.step, tool=step["operation"],
                    proposal_digest=proposal.content_digest, result_digest=result.content_digest,
                    audit=result.audit, completed=True, side_effect_possible=True, identity=identity)
                operations.invalidate_reads(frozenset({result.content.get("entity", "")}))
                completed.append(("已完成并核对：" if s.chinese else "Completed and verified: ") +
                                  proposal.effect + " — " + "、".join(proposal.targets))
            except BusinessMutationIndeterminate as exc:
                s.approvals.mutation_indeterminate()
                s.terminal_reason = "indeterminate_write"
                s.ledger.append("business_mutation_indeterminate", step=s.step, tool=step["operation"],
                    reconciliation=exc.reconciliation, side_effect_possible=True)
                completed.append("这一步可能已部分完成，停止后续操作；不会重发。" if s.chinese
                                 else "This step may have partially completed. Further operations stopped; it will not be retried.")
                break
            except BusinessToolPermissionError:
                s.terminal_reason = "permission_denied"
                completed.append("当前账号没有执行这项操作的权限。" if s.chinese else "The current actor cannot perform this operation.")
                break
            except BusinessToolReferenceError as exc:
                # No prior mutation may have landed before a user clarification.
                # The next turn gets a fresh plan, approval and actor read; it never
                # retries an acknowledged or uncertain dispatch.
                if exc.choices and not prepared and not s.mutation_outcomes:
                    _reference_clarification(s, exc.choices)
                    s.terminal_reason = "needs_clarification"
                    s.ledger.append("skill_reference_clarification_asked",
                                    step=s.step, entity=exc.entity,
                                    choices=len(exc.choices), side_effect_possible=False)
                    completed.append(s.clarification["question"])
                    break
                s.terminal_reason = "agent_refused"
                s.ledger.append("skill_operation_refused", step=s.step,
                                error_code=exc.code, detail=str(exc)[:300],
                                side_effect_possible=False)
                completed.append(str(exc))
                break
            except (BusinessToolError, ValueError) as exc:
                s.terminal_reason = ("business_mutation_blocked" if isinstance(exc, BusinessMutationBlockedError)
                    else "business_mutation_refused" if isinstance(exc, BusinessMutationRefusedError) else "agent_refused")
                s.ledger.append("skill_operation_refused", step=s.step, error_code=getattr(exc, "code", "skill_dependency"),
                                detail=str(exc)[:300], side_effect_possible=False)
                completed.append(str(exc))
                break
        else:
            # Reads resolve at their position, after preceding writes invalidated the
            # adapter's cache. The existing read/page port receives only GoalSpec.
            spec = GoalSpec.from_json(bind_goal_references(step["goal"], outcomes))
            completed.append(executor._execute_declared_goal(s, spec))
            if s.terminal_reason == "agent_finished":
                s.terminal_reason = ""
            else:
                break
    if not s.terminal_reason and s.source_requests and not s.source_answer:
        executor._complete_sources(s, s.chat)
    if not s.terminal_reason:
        s.terminal_reason = "agent_refused" if plan.get("unsupported") or not completed else "agent_finished"
    return "\n\n".join(p for p in completed if p)
