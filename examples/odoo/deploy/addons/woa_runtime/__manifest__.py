{
    "name": "WOA Runtime assets",
    "version": "1.2",
    "category": "Hidden",
    "summary": "Put the assistant on signed-in pages and serve PageController same-origin.",
    "description": (
        "Three things, and nothing else: a script tag that puts the assistant on every "
        "signed-in backend page, `/woa/session-assertion` that states who is signed in, "
        "and PageController served from the application's own origin, which the Runtime "
        "imports as a module.\n\n"
        "It serves files it does not contain: `python -m web_operation_agent_odoo.deploy "
        "provision` fetches them from the pinned npm release, checks every digest against "
        "`web_operation_agent/policies/page_controller.json`, and writes them into "
        "`static/src/`. They are not tracked in Git, so this repository redistributes "
        "nothing, and a deployment cannot quietly serve a different build."
    ),
    "depends": ["web"],
    # One script tag on the signed-in backend page; the assistant itself is the agent's.
    "data": ["views/assistant_entry.xml"],
    "installable": True,
    "auto_install": False,
    # Odoo's manifest field is an enum, not an SPDX expression. The project itself is MIT.
    "license": "Other OSI approved licence",
}
