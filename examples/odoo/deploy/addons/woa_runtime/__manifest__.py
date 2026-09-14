{
    "name": "WOA Runtime assets",
    "version": "1.0",
    "category": "Hidden",
    "summary": "Serve the pinned PageController same-origin for the agent Runtime.",
    "description": (
        "The embedded Runtime imports PageController as a module from the application's "
        "own origin. This addon is the smallest thing that puts it there.\n\n"
        "It serves files it does not contain: `python -m web_operation_agent_odoo.deploy "
        "provision` fetches them from the pinned npm release, checks every digest against "
        "`web_operation_agent/policies/page_controller.json`, and writes them into "
        "`static/src/`. They are not tracked in Git, so this repository redistributes "
        "nothing, and a deployment cannot quietly serve a different build."
    ),
    "depends": ["web"],
    "assets": {
        "web.assets_backend": [
            "woa_runtime/static/src/loader.js",
        ],
    },
    "installable": True,
    "auto_install": False,
    "license": "MIT",
}
