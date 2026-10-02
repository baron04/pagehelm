"""Narrow native tab clicks under the deployment's acknowledged API write fence."""
import re
from urllib.parse import urlsplit


class NativeTabPolicy:
    def __init__(self, record_routes):
        self._routes = tuple(re.compile(re.escape(route).replace(r"\{id\}", r"[0-9]+") + r"\Z")
                             for route in record_routes if isinstance(route, str) and "{id}" in route)

    def decide(self, observation, element):
        path = urlsplit(observation.url).path
        if observation.render_state != "ready" or observation.record_route != path:
            return "the application has not verified this rendered record"
        if not any(route.fullmatch(path) for route in self._routes):
            return "this is not a deployed record detail page"
        if (element.role != "tab" or not element.attrs.get("aria-controls")
                or not element.attrs.get("id")
                or str(element.attrs.get("aria-disabled", "false")).lower() == "true"
                or str(element.attrs.get("disabled", "false")).lower() not in {"", "false"}):
            return "only an enabled native tab is allowed here"
        return ""


__all__ = ["NativeTabPolicy"]
