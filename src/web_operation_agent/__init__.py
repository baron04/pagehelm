"""Web Operation Agent: a server-side ReAct agent with a thin browser runtime.

The server owns planning, optional knowledge providers, policy, approvals, and audit state.
The browser observes the current page and executes a fixed set of generic tools. See
``docs/architecture/system-design.md`` for the current component and trust boundaries.
"""
import sys

__version__ = "0.1.0"

#: The interpreters this repository is tested on, as [minimum, first unsupported). Five
#: modules carried "3.12" as a literal and four `pyproject.toml` files repeated it in their
#: metadata; `test_supported_python.py` fails when any of them drifts from this one.
SUPPORTED_PYTHON = ((3, 12), (3, 14))


def python_is_supported(version: tuple[int, int] | None = None) -> bool:
    """Whether an interpreter is inside the tested range."""
    current = tuple(version if version is not None else sys.version_info[:2])
    return SUPPORTED_PYTHON[0] <= current < SUPPORTED_PYTHON[1]


def supported_python_text() -> str:
    """The range as a person reads it: "3.12 or 3.13"."""
    low, high = SUPPORTED_PYTHON
    names = [f"{low[0]}.{minor}" for minor in range(low[1], high[1])]
    return names[0] if len(names) == 1 else " or ".join([", ".join(names[:-1]), names[-1]])
