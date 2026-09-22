"""Shared identity fixture; contains no test cases."""
from web_operation_agent.state.identity import PrincipalContext


def principal(*, issued_at: int = 1_000_000,
              expires_at: int = 1_060_000) -> PrincipalContext:
    return PrincipalContext(
        schema_version=1,
        application_id="sample",
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        actor_id="actor-1",
        delegation_id="delegation-1",
        issued_at=issued_at,
        expires_at=expires_at,
        scopes=("companies:read", "page:operate"),
    )
