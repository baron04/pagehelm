"""Web Operation Agent: a server-side ReAct agent with a thin browser runtime.

The server owns planning, optional knowledge providers, policy, approvals, and audit state.
The browser observes the current page and executes a fixed set of generic tools. See
``docs/architecture/system-design.md`` for the current component and trust boundaries.
"""
__version__ = "0.1.0"
