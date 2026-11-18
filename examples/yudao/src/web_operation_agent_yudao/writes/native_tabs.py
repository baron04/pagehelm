"""Narrow native tab clicks under the deployment's acknowledged API write fence."""
import re
from urllib.parse import urlsplit


class NativeTabPolicy:
    def __init__(self, record_routes, list_routes=()):
        self._routes = tuple(re.compile(re.escape(route).replace(r"\{id\}", r"[0-9]+") + r"\Z")
                             for route in record_routes if isinstance(route, str) and "{id}" in route)
        self._list_routes = frozenset(route for route in list_routes if isinstance(route, str))

    def decide(self, observation, element):
        path = urlsplit(observation.url).path
        record = observation.record_route == path and any(route.fullmatch(path) for route in self._routes)
        list_page = (path in self._list_routes and observation.page_route == path
                     and not observation.record_route and observation.page_read is not None
                     and observation.page_query is not None
                     and observation.page_query["path"] == observation.page_read["path"])
        if observation.render_state != "ready" or not (record or list_page):
            return "the application has not verified this rendered tab page"
        if (element.role != "tab" or not element.attrs.get("aria-controls")
                or not element.attrs.get("id")
                or str(element.attrs.get("aria-disabled", "false")).lower() == "true"
                or str(element.attrs.get("disabled", "false")).lower() not in {"", "false"}):
            return "only an enabled native tab is allowed here"
        return ""


class NativeNavigationPolicy:
    """Default-menu facts checked against the current actor's complete page index.

    This grants neither writes nor arbitrary buttons. The existing visible approval,
    stale-ref checks, navigation receipt and API write fence remain required.
    """
    def __init__(self, origin, page_index, tabs=None):
        self._origin = origin.rstrip("/")
        self._index, self._tabs = page_index, tabs

    def decide(self, observation, element):
        kind = element.attrs.get("navigation_kind")
        if kind not in {"branch", "page"}:
            return self._tabs.decide(observation, element) if self._tabs else "no verified menu target"
        url = urlsplit(observation.url)
        if (f"{url.scheme}://{url.netloc}" != self._origin
                or observation.render_state != "ready" or observation.page_route != url.path
                or element.epoch != observation.epoch
                or element not in observation.elements
                or element.role != "menuitem"
                or element.attrs.get("form_scope")
                or str(element.attrs.get("aria-disabled", "false")).lower() == "true"
                or str(element.attrs.get("disabled", "false")).lower() not in {"", "false"}):
            return "the application has not verified this current menu target"
        path = element.attrs.get("navigation_path", "")
        if (not isinstance(path, str) or not re.fullmatch(r"/(?!/)[^?#\\\s%]*", path)
                or path == "/" or "/../" in path or "/./" in path or path.endswith(("/..", "/."))):
            return "no canonical application menu destination"
        try:
            pages = {page.key for page in self._index().pages if page.key.startswith("/")}
        except Exception:
            return "the actor's page index is unavailable"
        allowed = path in pages if kind == "page" else any(p.startswith(path.rstrip("/") + "/") for p in pages)
        if not allowed or kind == "page" and element.attrs.get("href") != path:
            return "this menu target is outside the actor's page index"
        return ""


__all__ = ["NativeTabPolicy", "NativeNavigationPolicy"]
