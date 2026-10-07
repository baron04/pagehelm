"""Which page of a list is shown and how many rows it holds, on the page's own controls.

The application's page integration marks its pagination controls and reports each
completed list read (page, page size, a digest of what narrowed it). This procedure presses
those controls with the existing generic click and accepts only a later read of the same
list, narrowed as before, that reports the page or the size that was asked. A control that
is not shown, or a read that does not report it, is a paging that was not done.
"""
import re
from urllib.parse import urlsplit


def _read(obs, path=""):
    read = obs.page_read
    if (not isinstance(read, dict) or not isinstance(read.get("query_fields"), dict)
            or any(type(read.get(k)) is not int for k in ("page", "page_size", "revision"))
            or path and read.get("path") != path):
        return None
    return read


def _press(executor, s, element, route):
    """One confirmed generic click, settled like every other page action of the turn."""
    reason, detail = s.approvals.confirm_operation(s.step + 1, s.obs, action="click")
    if reason:
        s.terminal_reason, s.detail = reason, detail
        return False
    args = {"ref": element.ref, "epoch": s.obs.epoch}
    s.step += 1
    s.writes.expect_decision(False)
    s.ledger.append("tool_call", step=s.step, tool="click", args=args)
    with s.receipt.stage("ui"):
        receipt = s.browser.execute_with_budget("click", args, timeout_s=s.budget.check())
    s.approvals.settle_operation()
    outcome = s.writes.write_outcome(receipt, s.step, action_may_write=False)
    s.ledger.append("write_outcome", step=s.step, **{k: v for k, v in outcome.items() if k != "detail"})
    s.ledger.append("action_receipt", step=s.step, tool="click", command_id=receipt.command_id,
                    ok=receipt.ok, error_code=receipt.error_code)
    s.page_actions += 1
    if receipt.observation is not None:
        s.obs = receipt.observation
    reason, detail = s.writes.settle_write(outcome)
    if reason:
        s.terminal_reason, s.detail = reason, detail
        return False
    return bool(receipt.ok and not receipt.navigated
                and executor._wait_ready(s, route, record=False, requires_page_route=True))


def _find(executor, s, wanted):
    """The controls that answer, scrolling the page down a bounded number of times: a
    list's pagination sits under its rows, often below what the page shows at first."""
    for attempt in range(5):
        found = [e for e in s.obs.elements if wanted(e)]
        if found or attempt == 4:
            return found
        # Anchored on the last thing the page shows, so the content that holds it scrolls
        # rather than whichever container the page happens to list first.
        anchor = {"ref": s.obs.elements[-1].ref, "epoch": s.obs.epoch} if s.obs.elements else {}
        if executor._dispatch(s, "scroll", {"direction": "down", "pages": 3, **anchor}) is None:
            return []
    return []


def _moved(before, after, **wanted):
    return (after is not None and after["path"] == before["path"]
            and after["revision"] > before["revision"]
            and after["query_fields"] == before["query_fields"]
            and all(after[k] == v for k, v in wanted.items()))


def apply_paging(executor, state, plan) -> str:
    """Settle the plan's page size and page; the page state reached, or "" when not."""
    s = state
    route = plan.expected_path
    if (not route or urlsplit(s.obs.url).path != route or s.obs.page_route != route
            or s.obs.render_state != "ready"):
        return ""
    before = _read(s.obs)
    if before is None:
        s.ledger.append("goal_paging_settled", step=s.step, complete=False, why="no list read reported")
        return ""
    if plan.page_size and before["page_size"] != plan.page_size:
        sizes = _find(executor, s, lambda e: e.attrs.get("page_control") == "page_size")
        if not sizes or not _open_and_choose(executor, s, sizes[0], plan.page_size, route):
            s.ledger.append("goal_paging_settled", step=s.step, complete=False, why="page size")
            return ""
        after = _read(s.obs, before["path"])
        if not _moved(before, after, page_size=plan.page_size):
            s.ledger.append("goal_paging_settled", step=s.step, complete=False, why="page size not applied")
            return ""
        before = after
    target = before["page"] + plan.page_step if plan.page_step else plan.page_number
    if plan.page_step or (target and before["page"] != target):
        buttons = [] if target < 1 else _find(
            executor, s, lambda e: e.attrs.get("page_control") == "pagination"
            and e.attrs.get("page_number") == str(target))
        if not buttons or not _press(executor, s, buttons[0], route):
            s.ledger.append("goal_paging_settled", step=s.step, complete=False, why="page control")
            return ""
        after = _read(s.obs, before["path"])
        if not _moved(before, after, page=target, page_size=before["page_size"]):
            s.ledger.append("goal_paging_settled", step=s.step, complete=False, why="page not applied")
            return ""
        before = after
    s.ledger.append("goal_paging_settled", step=s.step, complete=True, page=before["page"],
                    page_size=before["page_size"], rows=before.get("rows"), total=before.get("total"))
    return f"{before['page']}/{before['page_size']}"


def _open_and_choose(executor, s, control, size, route) -> bool:
    if not _press(executor, s, control, route):
        return False
    wanted = re.compile(rf"\s*{size}(?!\d)")
    options = [e for e in s.obs.elements if e.role == "option" and wanted.match(e.name or "")]
    return len(options) == 1 and _press(executor, s, options[0], route)
