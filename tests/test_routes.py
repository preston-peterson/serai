"""The app's route table, asserted exactly.

main.py is a thin app that includes one router per area, so the ordering that
used to guarantee path-matching order is gone. This is the contract that
replaces it: every path, with its methods, and nothing else. Compared as a set --
the routers can be included in any order, but a route added, removed, renamed or
given a new method shows up here.
"""

from __future__ import annotations

from serai.main import app

EXPECTED = {
    ("/openapi.json", "GET,HEAD"),
    ("/docs", "GET,HEAD"),
    ("/docs/oauth2-redirect", "GET,HEAD"),
    ("/redoc", "GET,HEAD"),
    ("/", "GET,HEAD"),
    ("/favicon.ico", "GET,HEAD"),
    ("/static", "MOUNT"),
    ("/api/auth/status", "GET"),
    ("/api/login", "POST"),
    ("/api/logout", "POST"),
    ("/api/setup", "POST"),
    ("/api/users", "GET"),
    ("/api/users", "POST"),
    ("/api/users/{username}", "DELETE"),
    ("/api/network", "GET"),
    ("/api/network", "POST"),
    ("/api/users/{username}/password", "POST"),
    ("/api/settings", "GET"),
    ("/api/settings", "PUT"),
    ("/api/updates", "GET"),
    ("/api/updates/check", "POST"),
    ("/api/updates/apply", "POST"),
    ("/api/hosts", "GET"),
    ("/api/hosts", "POST"),
    ("/api/kinds", "GET"),
    ("/api/sessions", "GET"),
    ("/api/sessions/saved", "GET"),
    ("/api/files", "GET"),
    ("/api/files/op", "POST"),
    ("/api/file", "GET"),
    ("/api/broadcast", "POST"),
    ("/api/rename", "POST"),
    ("/api/kill", "POST"),
    ("/api/sessions/restart", "POST"),
    ("/api/sessions/relaunch", "POST"),
    ("/api/args", "POST"),
    ("/api/tags", "POST"),
    ("/api/favorite", "POST"),
    ("/api/launch", "POST"),
    ("/api/files/transfer", "POST"),
    ("/api/dir", "POST"),
    ("/api/upload", "POST"),
    ("/ws/attach", "WS"),
}


def route_table() -> set[tuple[str, str]]:
    """(path, methods) for every route, flattening the included routers.

    Methods are the sorted, comma-joined set a route answers to; a mounted
    subtree is "MOUNT" and a websocket is "WS", neither of which has methods.
    """
    rows: set[tuple[str, str]] = set()

    def walk(routes) -> None:
        for r in routes:
            if type(r).__name__ == "Mount":
                rows.add((r.path, "MOUNT"))
            elif getattr(r, "original_router", None) is not None:
                walk(r.original_router.routes)    # a router added with include_router
            elif getattr(r, "methods", None) is not None:
                rows.add((r.path, ",".join(sorted(r.methods))))
            else:
                rows.add((r.path, "WS"))

    walk(app.routes)
    return rows


def test_route_table_is_exactly_the_documented_set():
    table = route_table()
    assert table - EXPECTED == set(), f"unexpected routes: {sorted(table - EXPECTED)}"
    assert EXPECTED - table == set(), f"missing routes: {sorted(EXPECTED - table)}"
    assert len(table) == len(EXPECTED) == 43
