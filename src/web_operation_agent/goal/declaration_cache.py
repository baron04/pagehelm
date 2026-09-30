"""Bounded, executor-local cache of parsed read-only intent, never business answers."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import threading
import time


class DeclarationCache:
    def __init__(self, *, capacity=128, ttl_s=120, clock=time.monotonic):
        self.capacity, self.ttl_s, self.clock = capacity, ttl_s, clock
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        self._generation = 0

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._generation += 1

    def key(self, namespace, chat, messages, tools):
        # Store a digest rather than another copy of prompts/record names. Independent
        # readings use different namespaces; repairs and votes bypass this cache.
        payload = [namespace, chat.model, getattr(chat, "options", None), messages, tools]
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":"), default=str).encode()).digest()
        with self._lock:
            return self._generation, digest

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            when, value = entry
            if self.clock() - when >= self.ttl_s:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            value = deepcopy(value)
        value[-1].update(attempts=0, cache_hit=True)
        return value

    def put(self, key, value):
        with self._lock:
            if key[0] != self._generation:
                return  # A previous conversation's in-flight read cannot repopulate it.
            self._entries[key] = (self.clock(), deepcopy(value))
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)


def cached_read(cache, namespace, chat, messages, tools, cancelled):
    if cache is None:
        return None, None
    if cancelled is not None and cancelled():
        from ..modeling.provider import ProviderError
        raise ProviderError("provider call cancelled before cache lookup",
                            code="provider_cancelled", retryable=False, dispatched=False)
    key = cache.key(namespace, chat, messages, tools)
    return key, cache.get(key)
