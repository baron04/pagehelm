"""Optional passive lifecycle facts. No request, business value or decision enters here."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
import threading
import time
import uuid


_CURRENT = ContextVar("passive_turn_events", default=None)
_INTERVALS = frozenset({"model", "application", "page", "verification",
    "human_wait", "approval_driver_wait"})
_POINTS = frozenset({"request_received", "first_write_card_visible_observed",
    "final_result_visible_observed"})
_SOURCES = frozenset({"server-ingress", "normal-host", "existing-browser-observer",
    "human-channel", "approval-driver"})


class TurnEvents:
    """One collector clock shared by the normal host and its existing UI observer.

    Callback failures only mark delivery incomplete; they cannot authorize, change or
    retry a business action. The application/evaluator can refuse missing evidence.
    """
    def __init__(self, callback=None, *, clock=time.monotonic_ns):
        if callback is not None and not callable(callback):
            raise TypeError("turn event callback must be callable")
        self.callback, self.clock = callback, clock
        self.clock_id = "clock-" + uuid.uuid4().hex
        self._lock = threading.RLock()
        self._turns = {}
        self._latest = ""
        self._drivers = {}

    @contextmanager
    def turn(self, turn_id):
        with self._lock:
            if turn_id not in self._turns:
                self.accepted(turn_id)
            if self._turns[turn_id].get("executing"):
                raise ValueError("turn identity already observed")
            self._turns[turn_id]["executing"] = True
            self._latest = turn_id
        marker = _CURRENT.set((self, turn_id))
        try:
            yield
        finally:
            _CURRENT.reset(marker)

    def accepted(self, turn_id):
        """Called by the normal channel after acceptance, before it queues the task."""
        with self._lock:
            if turn_id in self._turns:
                return
            self._turns[turn_id] = {"events": [], "receipts": [], "delivery_complete": True,
                "unbound_mutations": 0}
            self.emit("request_received", "server-ingress", turn_id=turn_id)

    @property
    def latest_turn_id(self):
        with self._lock:
            return self._latest

    def emit(self, kind, source, *, turn_id=None, interval_id=""):
        paired = {name+suffix for name in _INTERVALS for suffix in ("_begin", "_end")}
        if (kind not in _POINTS and (kind not in paired or not interval_id)
                or source not in _SOURCES):
            raise ValueError("unknown passive event")
        with self._lock:
            turn_id = turn_id or self._latest
            record = self._turns[turn_id]
            event = {"kind": kind, "source": source, "clock_id": self.clock_id,
                "turn_id": turn_id, "monotonic_ns": self.clock()}
            if interval_id:
                event["interval_id"] = interval_id
            record["events"].append(event)
            if self.callback:
                try:
                    self.callback(dict(event))
                except Exception:
                    record["delivery_complete"] = False

    @contextmanager
    def interval(self, name, source="normal-host", *, turn_id=None):
        interval_id = "interval-" + uuid.uuid4().hex
        turn_id = turn_id or self.latest_turn_id
        self.emit(name + "_begin", source, turn_id=turn_id, interval_id=interval_id)
        try:
            yield
        finally:
            self.emit(name + "_end", source, turn_id=turn_id, interval_id=interval_id)

    def record_dispatch(self, turn_id, receipt):
        import copy
        with self._lock:
            self._turns[turn_id]["receipts"].append(copy.deepcopy(receipt))

    def incomplete(self, turn_id):
        with self._lock:
            self._turns[turn_id]["delivery_complete"] = False

    def unbound_mutation(self, turn_id):
        with self._lock:
            self._turns[turn_id]["unbound_mutations"] += 1

    def begin_approval_driver(self, approval_id):
        with self._lock:
            if approval_id in self._drivers:
                raise ValueError("approval driver interval already started")
            turn_id, interval_id = self._latest, "interval-"+uuid.uuid4().hex
            self._drivers[approval_id] = (turn_id, interval_id)
            self.emit("approval_driver_wait_begin", "approval-driver", turn_id=turn_id, interval_id=interval_id)

    def end_approval_driver(self, approval_id):
        with self._lock:
            pair = self._drivers.pop(approval_id, None)
            if pair:
                self.emit("approval_driver_wait_end", "approval-driver", turn_id=pair[0], interval_id=pair[1])

    def end_approval_drivers(self, turn_id):
        with self._lock:
            for approval_id, pair in list(self._drivers.items()):
                if pair[0] == turn_id:
                    self.end_approval_driver(approval_id)

    def snapshot(self, turn_id):
        import copy
        with self._lock:
            return copy.deepcopy(self._turns[turn_id])


def current_events():
    return _CURRENT.get()


def turn_interval(name, source="normal-host"):
    current = current_events()
    return current[0].interval(name, source, turn_id=current[1]) if current else nullcontext()


def observed_interval(name):
    def decorate(method):
        @wraps(method)
        def observed(*args, **kwargs):
            with turn_interval(name):
                return method(*args, **kwargs)
        return observed
    return decorate


def observed_turn(method):
    """Reuse channel acceptance; direct executor callers start at their own ingress."""
    @wraps(method)
    def observed(self, message):
        events = self.turn_events
        if events is None:
            return method(self, message)
        # Validation and execution retain their normal behavior inside this scope.
        from .product import AgentTurnMessage
        turn_id = AgentTurnMessage.from_wire(message).message_id
        with events.turn(turn_id):
            try:
                return method(self, message)
            finally:
                events.end_approval_drivers(turn_id)
    return observed
