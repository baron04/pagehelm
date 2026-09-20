"""Durable server-side state: audit journal, conversation, identity and ownership.

``ledger`` is the append-only session journal, ``conversation`` the multi-turn state,
``identity`` the principal and operation contexts, ``store`` the SQLite ownership
leases, ``quota`` the shared provider-request limits and ``authcache`` the expiring
nonces and revocation watermarks that authorize messages.
"""
#: name -> submodule, resolved on first access (PEP 562). Eager imports would make
#: ``business.runtime -> state.identity`` pull in ``state.conversation -> business.runtime``
#: and deadlock the import order of whoever starts second.
_EXPORTS = {
    "SQLiteExpiringNonceStore": "authcache",
    "SQLitePrincipalRevocations": "authcache",
    "ConversationState": "conversation",
    "SQLiteConversationState": "conversation",
    "prune_sqlite_conversations": "conversation",
    "IdentityAssertion": "identity",
    "OperationContext": "identity",
    "PrincipalContext": "identity",
    "PrincipalError": "identity",
    "PrincipalRevokedError": "identity",
    "actor_authority_digest": "identity",
    "bind_matching_identity": "identity",
    "SessionLedger": "ledger",
    "SQLiteProviderQuota": "quota",
    "DurableStateError": "store",
    "SessionOwner": "store",
    "SQLiteSessionStore": "store",
    "principal_scope": "store",
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
