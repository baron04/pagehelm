"""Verify native proxy actors with Yudao; keep only bounded credential digests."""
import hashlib
import json
import threading
import time
from collections import OrderedDict

from ..reads.client import YudaoClient
from .page_guard import GuardRefused


class GuardIdentity:
    def __init__(self, upstream, *, lifetime_s=60, limit=2048, clock=time.monotonic):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 4096:
            raise ValueError("identity cache limit is invalid")
        if isinstance(lifetime_s, bool) or not isinstance(lifetime_s, (int, float)) or not 0 < lifetime_s <= 60:
            raise ValueError("identity cache lifetime is invalid")
        self.upstream, self.lifetime_s, self.limit, self.clock = upstream, lifetime_s, limit, clock
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def __call__(self, token, tenant):
        if (not isinstance(token, str) or not token or len(token) > 8192
                or len(str(tenant)) > 32 or not str(tenant).isdigit()):
            raise GuardRefused("identity_required")
        key = hashlib.sha256(json.dumps([tenant, token], separators=(",", ":")).encode()).digest()
        now = self.clock()
        with self._lock:
            held = self._cache.get(key)
            if held is not None and held[0] > now:
                self._cache.move_to_end(key)
                return held[1]
        try:
            data = YudaoClient.for_token(self.upstream, token, str(tenant), timeout_s=5).get(
                "system/auth/get-permission-info")
            actor = (data or {}).get("user", {}).get("id")
            if isinstance(actor, bool) or not str(actor).isdigit():
                raise GuardRefused("identity_required")
        except Exception:
            raise GuardRefused("identity_required") from None
        bound = hashlib.sha256(json.dumps([str(tenant), str(actor)], separators=(",", ":")).encode()).hexdigest()
        with self._lock:
            self._cache[key] = (self.clock() + self.lifetime_s, bound)
            self._cache.move_to_end(key)
            while len(self._cache) > self.limit:
                self._cache.popitem(last=False)
        return bound


__all__ = ["GuardIdentity"]
