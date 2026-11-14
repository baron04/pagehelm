"""Web Operation Agent: a server-side ReAct agent with a thin browser runtime.

The server owns planning, optional knowledge providers, policy, approvals, and audit state.
The browser observes the current page and executes a fixed set of generic tools. See
``docs/concepts/overview.md`` for the current component and trust boundaries.
"""
import sys

__version__ = "0.1.0"

#: Tested interpreter range, as [minimum, first unsupported).
#: Distribution metadata is checked against this range by test_supported_python.py.
SUPPORTED_PYTHON = ((3, 13), (3, 14))


def python_is_supported(version: tuple[int, int] | None = None) -> bool:
    """Whether an interpreter is inside the tested range."""
    current = tuple(version if version is not None else sys.version_info[:2])
    return SUPPORTED_PYTHON[0] <= current < SUPPORTED_PYTHON[1]


def supported_python_text() -> str:
    """The supported version range for user-facing messages."""
    low, high = SUPPORTED_PYTHON
    names = [f"{low[0]}.{minor}" for minor in range(low[1], high[1])]
    return names[0] if len(names) == 1 else " or ".join([", ".join(names[:-1]), names[-1]])
