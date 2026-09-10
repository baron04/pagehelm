"""Self-contained onboarding application for Web Operation Agent."""

from .app import (DEMO_CONTACTS, MinimalContactsAdapter,
                  MinimalContactsApplication, ScriptedDemoChat, run_demo)

__all__ = [
    "DEMO_CONTACTS",
    "MinimalContactsAdapter",
    "MinimalContactsApplication",
    "ScriptedDemoChat",
    "run_demo",
]
