"""Sortable, pageable tables: one place for the ?sort=&dir= convention so every list behaves the same."""
from collections.abc import Callable
from urllib.parse import urlencode


def resolve_sort(sort: str | None, direction: str | None, allowed: dict, default: str, default_dir: str = "asc"):
    """Return (key, dir, order_by clauses) for a SQL query. `allowed` maps a key to a tuple of columns."""
    key = sort if sort in allowed else default
    d = direction if direction in ("asc", "desc") else default_dir
    order = [c.desc() if d == "desc" else c.asc() for c in allowed[key]]
    return key, d, order


def sort_url_factory(path: str, params: dict, sort: str, direction: str) -> Callable[[str], str]:
    """Builds header links: clicking the active column flips direction, any other column starts ascending."""
    def sort_url(key: str) -> str:
        nd = "desc" if (key == sort and direction == "asc") else "asc"
        return path + "?" + urlencode({k: v for k, v in {**params, "sort": key, "dir": nd}.items() if v})
    return sort_url


def page_url_factory(path: str, params: dict) -> Callable[[int], str]:
    def page_url(n: int) -> str:
        return path + "?" + urlencode({k: v for k, v in {**params, "page": n}.items() if v})
    return page_url
