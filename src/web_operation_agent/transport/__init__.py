"""The transport layer: the browser contract and the in-process remote runtime.

``contract``-level names (BrowserExecutor, TransportError, TurnCancelled) describe what
the server Agent may assume; ``protocol``, ``channel``, ``registry``, ``gateway`` and the
browser client are the embedded shipping implementation behind that contract.
"""
#: name -> submodule, resolved on first access; see the note in ``state/__init__``.
_EXPORTS = {
    "BrowserExecutor": "browser",
    "RemoteBrowserClient": "browser",
    "TransportError": "browser",
    "TurnCancelled": "browser",
    "RemoteSessionChannel": "channel",
    "RemoteSessionLimits": "channel",
    "SessionGrant": "channel",
    "RemoteProtocolError": "protocol",
    "RemoteSessionClosed": "protocol",
    "ensure_safe_browser_payload": "protocol",
    "RemoteSessionRegistry": "registry",
}


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    value = getattr(import_module(f'.{module}', __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(_EXPORTS)


__all__ = sorted(_EXPORTS)
