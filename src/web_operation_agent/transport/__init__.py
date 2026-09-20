"""The transport layer: the browser contract and the in-process remote runtime.

``contract``-level names (BrowserExecutor, TransportError, TurnCancelled) describe what
the server Agent may assume; ``protocol``, ``channel``, ``registry``, ``gateway`` and the
browser client are the embedded shipping implementation behind that contract.
"""
from .browser import BrowserExecutor, RemoteBrowserClient, TransportError, TurnCancelled
from .channel import RemoteSessionChannel, RemoteSessionLimits, SessionGrant
from .protocol import (RemoteProtocolError, RemoteSessionClosed,
                      ensure_safe_browser_payload)
from .registry import RemoteSessionRegistry

__all__ = ["BrowserExecutor", "RemoteBrowserClient", "RemoteProtocolError",
           "RemoteSessionChannel", "RemoteSessionClosed", "RemoteSessionLimits",
           "RemoteSessionRegistry", "SessionGrant", "TransportError", "TurnCancelled",
           "ensure_safe_browser_payload"]
