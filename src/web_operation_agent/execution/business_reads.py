"""Read-only typed business tools and evidence-backed navigation decisions."""
from __future__ import annotations

import hashlib
import re

from ..answer import answer_protocol
from ..transport.browser import TransportError, TurnCancelled
from ..business.errors import BusinessToolError, BusinessToolPermissionError
from ..business.entities import QUERY_ENTITY_RECORDS_TOOL
from ..business.goal_candidates import RESOLVE_GOAL_CANDIDATES_TOOL
from .knowledge_tools import finish_with_clarification
from .goal_metrics import materialize_goal_metrics
from .prompts import CLARIFICATION_CHOICE_LIMIT, _assistant_call, _tool_result, _trim
from .routes import observation_is_at_route
from .runstate import _Next, _Turn

#: The third read in one turn whose content repeats an earlier one ends the turn as
#: `no_progress`. Two are answered with a short notice instead of the whole result.
MAX_REPEATED_READS = 3


def execute_business_read(session, turn: "_Turn", call: dict, name: str, args: dict,
                          operation_context, operation_identity: dict) -> "_Next":
    result, t0, step = turn.result, turn.started_at, turn.step
    messages, obs = turn.messages, turn.obs
    successful_business_tools = turn.successful_business_tools
    tool_result = None
    if session._cancel_requested():
        return _Next(session._finish(
            result, t0, "user_cancelled",
            "the user stopped this task before tool dispatch", steps=step))
    try:
        tool_result = session.business_tools.execute(
            name, args, session._loaded_business_skill_ids,
            context=operation_context)
        session.ledger.append(
            "business_tool_result", step=step, tool=name,
            result_digest=tool_result.content_digest,
            audit=tool_result.audit,
            data_classes=list(tool_result.data_classes),
            route_count=len(tool_result.allowed_routes),
            **({"navigation_target_digest": hashlib.sha256(
                tool_result.navigation_target.encode()).hexdigest()}
               if tool_result.navigation_target else {}),
            side_effect_possible=False, identity=operation_identity)
        session._business_routes.update(tool_result.allowed_routes)
        session._latest_business_routes = set(tool_result.allowed_routes)
        if name == QUERY_ENTITY_RECORDS_TOOL:
            session._business_record_routes.update(
                record["ui_path"] for record in tool_result.content.get("records", ())
                if isinstance(record, dict) and record.get("ui_path") in tool_result.allowed_routes)
        session.latest_business_tool_result = tool_result
        session._form_draft = (
            tool_result.form_draft if session.allow_form_prefill else None)
        recorded_refs = session._answers.collect_evidence(
            tool_result, step=step, read_tool=name)
        goal_metric_refs = materialize_goal_metrics(
            session.evidence, recorded_refs, tool=name,
            goals=session._answers.answer_goals, step=step)
        session._answers.generated_goal_metric_refs.update(goal_metric_refs)
        session._latest_business_query_page_actions = (
            session.page_actions if tool_result.query_answer is not None else None)
        if tool_result.query_answer is not None:
            session._feed.note(
                stage="read_done", tool=name,
                count=tool_result.query_answer.matched_count,
                scope=session._scope_label(tool_result.query_answer.scope))
        # Candidate metadata is not a completed business-data read. A plain model reply
        # after this tool alone must not be projected as a successful answer.
        if name != RESOLVE_GOAL_CANDIDATES_TOOL:
            successful_business_tools += 1
        first_step = session._read_digests.setdefault(tool_result.content_digest, step)
        if first_step != step:
            # The application answered exactly as it did before. Handing the whole
            # result back again is what let a turn spend its budget rereading one list
            # between clicks; the earlier result and its evidence refs still stand.
            session._repeated_reads += 1
            session.ledger.append(
                "business_read_repeated", step=step, tool=name,
                first_step=first_step, repeated=session._repeated_reads,
                result_digest=tool_result.content_digest, side_effect_possible=False)
            if session._repeated_reads >= MAX_REPEATED_READS:
                result.agent_said = (
                    "I couldn't make progress: the same business read kept returning "
                    "the same result, so I stopped without claiming completion.")
                return _Next(session._finish(
                    result, t0, "no_progress", result.agent_said, steps=step))
            feedback = (
                f"SAME RESULT: this read returned exactly what step {first_step} "
                "returned. No new facts, and its evidence refs still apply. Do not "
                "repeat it; use what you have, change the investigation, or finish.")
        else:
            feedback = ("BUSINESS TOOL RESULT (current runtime fact):\n"
                        + tool_result.render())
        if session._form_draft is not None:
            feedback += (
                "\n\nAPPLICATION FORM DRAFT READY: Call prefill_form with no "
                "arguments. The server will use the exact current route, fields "
                "and values above, stop before submission, and hand control to "
                "the user.")
        if session._evidence_feedback and first_step == step:
            feedback += "\n\n" + session._evidence_feedback
        if goal_metric_refs and first_step == step:
            feedback += (
                "\n\nDECLARED GOAL METRICS READY (full executed scope; cite these "
                "refs directly in finish instead of calling compute again):\n"
                + answer_protocol.evidence_index(session.evidence, goal_metric_refs))
        if session._clarification_feedback:
            feedback += "\n\n" + session._clarification_feedback
        if tool_result.query_answer is not None:
            feedback += (
                "\n\nSERVER QUERY ANSWER READY: For a list or filtered lookup, "
                "answer now without a browser action. Navigate only when the "
                "user explicitly asked to open a record or collection.")
            projection = tool_result.query_answer
            if projection.complete and projection.matched_count == 0:
                # Completeness describes this tool's query scope, not whether
                # its entity and filters answer the user's question. A number
                # searched as a customer name is a complete empty query, but
                # says nothing about the customer of that numbered order.
                if any(goal.target_read_tool == name
                       for goal in session._answers.answer_goals):
                    feedback += (
                        "\n\nEMPTY DECLARED GOAL SCOPE: This requested collection has "
                        "an exact zero under the executed conditions. If the subject "
                        "identity and filter match the user's request, compute count "
                        "and any adapter-certified empty sum on THIS query reference. "
                        "Other collections may help investigate identity, but their "
                        "numbers cannot replace this goal's count or total.")
                else:
                    feedback += (
                        "\n\nEMPTY QUERY SCOPE: No records matched this particular "
                        "entity and filter. Do not conclude that the requested "
                        "object is absent until its identity and query scope match "
                        "the user's request. Check another relevant entity or "
                        "relation if needed.")
            # Several records answer the person's own identifying words only
            # when the read carried them. A read that named nothing returns the
            # collection, and "which one did you mean" is then a question they
            # did not ask: "打开金额最高的销售订单" was answered with a list of
            # every order. That request stays with the model, which can compute
            # the one it names.
            identified = any(getattr(item, "looked_for", "")
                             for item in tool_result.evidence or ())
            if (session.allow_clarification and session._navigation_only and identified
                    and projection.complete
                    and 1 < projection.matched_count
                    <= CLARIFICATION_CHOICE_LIMIT
                    and len(session._clarification_choices)
                    == projection.matched_count
                    and all(skill.read_only
                            for skill in session._task_business_skills)):
                # The current typed read already proved both the ambiguity and
                # the complete set of choices. Asking the model to copy those
                # choices into ask_user adds latency and lets it omit or rename
                # records without adding judgment. Keep this transition in the
                # server contract; the reply still grants no route authority and
                # the next turn must read the application again before opening.
                # A set larger than one question can offer stays with the model,
                # which asks the person to narrow it in its own words.
                question = (
                    "找到多个匹配项，请选择要打开的记录："
                    if re.search(r"[\u3400-\u9fff]", session.task.prompt)
                    else "Multiple records match. Choose the one to open:")
                return _Next(finish_with_clarification(
                    session, result, t0, step=step, question=question,
                    choice_ids=list(session._clarification_choices),
                    deterministic="complete_navigation_query"))
        # A request that names a second read collection may want a related
        # record's page ("the customer of order S1"). The record this read found is
        # then only the way there, so the model, not this shortcut, decides what
        # to open.
        if (session.deterministic_read_navigation and session._navigation_only
                and sum(skill.read_only for skill in session._task_business_skills) == 1
                and "goto" not in session.disabled_actions
                and tool_result.navigation_target):
            target = tool_result.navigation_target
            if session._cancel_requested():
                return _Next(session._finish(
                    result, t0, "user_cancelled",
                    "the user stopped this task before browser dispatch",
                    steps=step))
            reason, detail = session._approvals.confirm_operation(
                step, obs, action="goto", url=target)
            if reason:
                return _Next(session._finish(result, t0, reason, detail, steps=step))
            prior_page_epoch = obs.epoch
            try:
                session._browser_attempts += 1
                receipt = session.browser.execute(
                    "goto", {"url": target}, expect_navigation=True)
            except TurnCancelled as exc:
                session.ledger.append(
                    "browser_action_cancelled_before_dispatch", step=step,
                    tool="goto", dispatched=False, side_effect_possible=False)
                return _Next(session._finish(
                    result, t0, "user_cancelled", str(exc), steps=step))
            except TransportError as exc:
                return _Next(session._finish(
                    result, t0, session._transport_terminal(
                        "goto", may_write=False), str(exc), steps=step))
            session._approvals.settle_operation()
            if receipt.error_code != "stale_observation":
                session.page_actions += 1
            write = session._writes.write_outcome(
                receipt, step, action_may_write=False)
            session.ledger.append(
                "action_receipt", step=step, tool=receipt.action,
                command_id=receipt.command_id,
                write_attribution="unattributed", ok=receipt.ok,
                error_code=receipt.error_code, navigated=receipt.navigated,
                url_after=receipt.url_after, elapsed_ms=receipt.elapsed_ms,
                write=write["state"], relogin=receipt.relogin,
                deterministic_business_navigation=True)
            if receipt.error_code == "stale_observation":
                # The command did not reach the page and the receipt carries a
                # replacement observation. Repeating the decision against that
                # observation is recovery, not a no-progress loop.
                session._repeats.reset()
            reason, detail = session._writes.settle_write(write)
            if reason:
                return _Next(session._finish(result, t0, reason, detail, steps=step))
            if receipt.observation is not None:
                obs = receipt.observation
            reached = bool(
                receipt.ok and receipt.observation is not None
                and receipt.observation.epoch > prior_page_epoch
                and receipt.observation.render_state == "ready"
                and observation_is_at_route(receipt.observation, target)
                and not session._page_goal_unmet(receipt.observation))
            if reached:
                session._answer_action_receipt = {
                    "kind": "navigation", "url": obs.url}
                result.agent_said = (
                    "已打开请求的页面。"
                    if re.search(r"[\u3400-\u9fff]", session.task.prompt)
                    else "Opened the requested page.")
                session.ledger.append(
                    "deterministic_navigation_completed", step=step,
                    url_digest=hashlib.sha256(obs.url.encode()).hexdigest(),
                    observation_epoch=obs.epoch, side_effect_possible=False,
                    source="business_result")
                return _Next(session._finish(
                    result, t0, "agent_finished", result.agent_said,
                    steps=step))
            note = session._ground(
                obs, receipt, write["state"], write["refused_requests"])
            feedback += (
                "\n\nThe server tried the adapter's unique current route, but "
                "the fresh page did not verify that target. Replan from the "
                "current page.\n\n" + session._render(obs, receipt, note, write))
    except BusinessToolPermissionError as exc:
        # The account's own authority refused this read. The turn goes on with
        # what it already read -- ending it threw away the half of the question
        # this account may see -- but the refusal closes the page as a source of
        # facts, because handing the refusal back with the page still readable is
        # how a refused scope gets read off the screen instead (owner ruling
        # D13). From here every answer must be typed and evidence-backed, page
        # evidence is discarded and refused, and the answer says what was not
        # read whether or not the model chooses to.
        label = session._answers.operation_label(name)
        detail = str(exc)[:500]
        session.ledger.append(
            "business_tool_permission_denied", step=step, tool=name,
            error_code=exc.code, layer=exc.layer,
            required_scope=exc.required_scope,
            dispatched=exc.layer == "application", turn_continued=True,
            side_effect_possible=False, identity=operation_identity)
        if label not in session._refused_reads:
            # An adapter with no name for it in this language still records the
            # refusal, so the answer cannot read as a whole answer.
            session._refused_reads.append(label)
        session._refused_detail = detail
        result.refused_operation = label or result.refused_operation
        session._answer_mode = True
        session.evidence.discard_pages()
        session._page_answer_digest = ""
        session._form_draft = None
        session._latest_business_routes = set()
        session._clarification_choices = {}
        session._clarification_feedback = ""
        feedback = (
            "BUSINESS TOOL REFUSED BY AUTHORIZATION: " + detail +
            " This account may not perform that read, and no other route to the "
            "same facts is open in this turn: page content is no longer accepted "
            "as evidence. Answer only from the typed reads you already have, and "
            "state what you could not read as an undetermined conclusion. If you "
            "have read nothing, finish with outcome=unable.")
    except BusinessToolError as exc:
        session.latest_business_tool_result = None
        session._form_draft = None
        session._latest_business_routes = set()
        session._clarification_choices = {}
        session._clarification_feedback = ""
        session._latest_business_query_page_actions = None
        detail = str(exc)[:500]
        session.ledger.append(
            "business_tool_rejected", step=step, tool=name,
            error_code=getattr(exc, "code", "business_tool"),
            reason=detail, side_effect_possible=False,
            identity=operation_identity)
        feedback = "BUSINESS TOOL FAILED: " + detail
    session._sync_plan_message(messages)
    messages += [_assistant_call(call), _tool_result(call, feedback)]
    messages = _trim(messages, session.keep_observations)
    return session._resume(turn, messages, obs, successful_business_tools)
