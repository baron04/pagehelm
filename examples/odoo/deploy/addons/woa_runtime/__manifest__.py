{
    "name": "WOA Runtime assets",
    "version": "1.3",
    "category": "Hidden",
    "summary": "Put the assistant on signed-in pages and serve PageController same-origin.",
    "description": (
        "A small local shell keeps the assistant entry visible even when the Agent is "
        "unavailable. `/woa/session-assertion` states who is signed in, and PageController "
        "is served from the application's own origin for the Agent Runtime.\n\n"
        "Third-party PageController files are provisioned rather than committed: "
        "`python -m web_operation_agent_odoo.deploy "
        "provision` fetches them from the pinned npm release, checks every digest against "
        "`web_operation_agent/policies/page_controller.json`, and writes them into "
        "`static/src/`. They are not tracked in Git, so this repository redistributes "
        "nothing, and a deployment cannot quietly serve a different build."
    ),
    "depends": ["web"],
    # One stable shell on the signed-in backend page; the full assistant is the Agent's.
    "data": ["views/assistant_entry.xml"],
    "installable": True,
    "auto_install": False,
    # Odoo's manifest field is an enum, not an SPDX expression. The project itself is MIT.
    "license": "Other OSI approved licence",
}
