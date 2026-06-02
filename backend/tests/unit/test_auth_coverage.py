"""
Auth Coverage Tests
===================
Statically verify that every non-allowlisted HTTP endpoint enforces
at least one authentication dependency.

This test makes no network connections and requires no running services.
It introspects the live FastAPI route table and dependency graph at import
time using FastAPI's internal `dependant` structure.

Design
------
FastAPI compiles a `Dependant` object for every route at registration time.
`Dependant.dependencies` is a recursive list of `Dependant` nodes, one for
each FastAPI dep in the call chain (function params, router-level deps, and
route-level deps are all merged here).  We walk this tree depth-first to
collect every callable in the chain and assert that at least one of the two
root auth sentinels is present:

  • get_current_user_id  — validates Bearer JWT or access-token cookie
  • require_admin_role   — validates Bearer JWT AND asserts role == "admin"

All derived auth dependencies (get_current_user, require_role, CurrentUserID,
AdminUserID, CurrentUser) ultimately reach one of these two functions, so
they are detected transitively.

Public route allowlist
----------------------
Routes intentionally exempt from authentication are listed in
``_PUBLIC_ROUTES``.  Every entry must explain *why* no auth is needed.
A failing test means either a new unprotected route was introduced (which
must be justified and listed) or a formerly public route now requires auth
(remove the allowlist entry).

Adding a new genuinely public endpoint is a conscious security decision:
update the allowlist here, or the test will fail on every CI run as an
automatic checkpoint.

In-band auth paths
------------------
Some routes authenticate outside of FastAPI's dependency system:
  • SSE endpoint (/ai/stream): JWT passed as ?token= query param because the
    browser EventSource API cannot send custom headers.
  • WebSocket routes: first-frame JWT auth — these are WebSocketRoute objects
    which are excluded from iteration entirely.
"""
from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.routing import APIRoute

from app.core.auth import require_admin_role
from app.core.security import get_current_user_id

# ── Auth sentinels ─────────────────────────────────────────────────────────────
_AUTH_SENTINELS: frozenset = frozenset({get_current_user_id, require_admin_role})


# ── Public allowlist ───────────────────────────────────────────────────────────
# Keyed as frozenset of (HTTP_METHOD, exact_path_pattern).
#
# Each entry must carry a brief rationale in the adjacent comment so that
# code reviewers can evaluate the security decision without reading docs.
_PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset({
    # ── Auth bootstrap ─────────────────────────────────────────────────────
    ("POST", "/api/v1/auth/register"),
    # Public self-service registration (creates viewer-role accounts only).

    ("POST", "/api/v1/auth/login"),
    # Public login — credential is the password, not a JWT.

    ("POST", "/api/v1/auth/refresh"),
    # Refresh token is its own credential (validated inside the handler).
    # Cannot protect with JWT dep because the access token may be expired.

    ("POST", "/api/v1/auth/dev-login"),
    # Development convenience endpoint.  Runtime-gated: returns HTTP 404
    # when settings.is_production is True.  Safe in production.

    # ── Root health / Kubernetes probes ────────────────────────────────────
    ("GET", "/health"),
    # K8s liveness probe registered at root level by create_app().

    ("GET", "/health/ready"),
    # K8s readiness probe registered at root level by create_app().

    ("GET", "/health/detailed"),
    # Extended health detail for ops tooling, registered at root level.

    # ── API-prefixed ML health probes ───────────────────────────────────────
    ("GET", "/api/v1/health/ml"),
    # ML system health check — queried by the training orchestrator to detect
    # degraded inference before submitting a challenger run.

    ("GET", "/api/v1/health/ml/ready"),
    # ML readiness probe — used by K8s to gate traffic after rolling deploys.

    ("GET", "/api/v1/health/ml/live"),
    # ML liveness probe — simple "is the process alive" check.

    # ── Observability ──────────────────────────────────────────────────────
    ("GET", "/metrics"),
    # Prometheus scrape endpoint — consumed by the Prometheus container on
    # the internal docker network.  Not exposed externally via the firewall.

    # ── ML inference health ────────────────────────────────────────────────
    ("GET", "/api/v1/ml/health"),
    # Intentionally public (documented in handler summary).  Used by the
    # training orchestrator and monitoring stack to check whether a production
    # model is loaded before submitting a challenger run.  Exposes only
    # operational state (versions, weights) — no user data or predictions.
})


# ── In-band auth paths ────────────────────────────────────────────────────────
# Routes that authenticate outside FastAPI's dependency system.
# These paths will NOT have an auth sentinel in their dep tree; they must be
# explicitly listed here rather than the public allowlist so we document that
# auth *is* enforced — just not via a FastAPI dep.
_IN_BAND_AUTH_PATHS: frozenset[str] = frozenset({
    "/api/v1/ai/stream",
    # SSE endpoint.  The browser EventSource API cannot send custom headers,
    # so the JWT is passed as ?token= query param and validated manually via
    # decode_token() at the top of the handler before any event is yielded.
    # Docs: app/api/v1/ai_stream.py module docstring.
})


# ── Dependency tree traversal ─────────────────────────────────────────────────

def _iter_dep_callables(dependant, _seen: set | None = None) -> Iterator:
    """
    Depth-first iterator over all dependency callables in a Dependant tree.

    FastAPI compiles each route's full dependency chain (router-level deps,
    path-operation deps, and function-param deps) into a Dependant tree at
    registration time.  ``dependant.dependencies`` is a list of ``Dependant``
    nodes — each node has a ``.call`` attribute (the callable) and its own
    ``.dependencies`` list (its sub-deps).  This function walks the tree
    without revisiting nodes, yielding every callable in the chain.

    Args:
        dependant: FastAPI Dependant object (``route.dependant`` or any node
                   in the dependency tree).
        _seen:     Internal set to prevent revisiting shared dep nodes.

    Yields:
        Callable objects from the dependency chain.
    """
    if _seen is None:
        _seen = set()
    for dep in dependant.dependencies:
        # Each item in .dependencies is itself a Dependant object.
        if dep.call is None:
            continue
        node_id = id(dep.call)
        if node_id in _seen:
            continue
        _seen.add(node_id)
        yield dep.call
        # Recurse into this node's own sub-dependencies.
        yield from _iter_dep_callables(dep, _seen)


def _route_has_auth(route: APIRoute) -> bool:
    """
    Return True iff the route's dependency tree contains an auth sentinel.

    Traverses the full dependency graph and checks whether any callable
    is one of the two root authentication functions.
    """
    return any(c in _AUTH_SENTINELS for c in _iter_dep_callables(route.dependant))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app_http_routes() -> list[APIRoute]:
    """
    Return all HTTP APIRoute instances registered in the live app.

    WebSocketRoute instances are excluded — they use in-band first-frame
    JWT auth and are not subject to the FastAPI dependency check.
    """
    from app.main import app

    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
    ]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestAuthCoverage:
    """
    Enforce that every HTTP endpoint requires authentication.

    Two complementary tests:
      1. Every non-allowlisted route has an auth dep.
      2. Every allowlisted route actually exists (prevents stale entries).
    """

    @pytest.mark.unit
    def test_every_http_route_requires_auth(self, app_http_routes: list[APIRoute]) -> None:
        """
        Assert that every registered HTTP route not in the allowlist enforces
        at least one auth dependency.

        Failure means either:
          (a) A new endpoint was added without an auth dep — add the dep, OR
          (b) A genuinely public endpoint was added — add it to _PUBLIC_ROUTES
              with a rationale comment.
        """
        violations: list[str] = []

        for route in app_http_routes:
            methods = sorted(route.methods or set())
            path = route.path

            # Skip routes whose (method, path) pair is in the public allowlist
            if any((m, path) in _PUBLIC_ROUTES for m in methods):
                continue

            # Skip routes that use alternative in-band auth
            if path in _IN_BAND_AUTH_PATHS:
                continue

            # Assert auth dep is present
            if not _route_has_auth(route):
                violations.append(
                    f"  {', '.join(methods):8s}  {path}"
                )

        assert not violations, (
            "Unauthenticated routes detected.\n\n"
            "Every route below lacks an authentication dependency and is not\n"
            "listed in the public allowlist (_PUBLIC_ROUTES).\n\n"
            "Remediation options:\n"
            "  • Add Depends(get_current_user_id) or equivalent to the route.\n"
            "  • Add the route to _PUBLIC_ROUTES (with a rationale comment)\n"
            "    if it is intentionally public.\n"
            "  • Add the path to _IN_BAND_AUTH_PATHS if auth is handled\n"
            "    outside FastAPI's dependency system.\n\n"
            "Affected routes:\n"
            + "\n".join(violations)
        )

    @pytest.mark.unit
    def test_public_allowlist_routes_all_exist(self, app_http_routes: list[APIRoute]) -> None:
        """
        Assert that every entry in _PUBLIC_ROUTES corresponds to a real route.

        Prevents the allowlist from silently accumulating stale entries when
        routes are renamed or removed — a stale allowlist entry is a false
        security guarantee.
        """
        registered: set[tuple[str, str]] = {
            (method, route.path)
            for route in app_http_routes
            for method in (route.methods or set())
        }

        stale: list[str] = [
            f"  {method:8s}  {path}"
            for method, path in sorted(_PUBLIC_ROUTES)
            if (method, path) not in registered
        ]

        assert not stale, (
            "Public allowlist contains routes that do not exist in the app.\n\n"
            "Remove these stale entries from _PUBLIC_ROUTES in\n"
            "tests/unit/test_auth_coverage.py:\n\n"
            + "\n".join(stale)
        )

    @pytest.mark.unit
    def test_in_band_auth_paths_all_exist(self, app_http_routes: list[APIRoute]) -> None:
        """
        Assert that every path in _IN_BAND_AUTH_PATHS corresponds to a real route.

        Ensures in-band auth documentation stays accurate when routes change.
        """
        registered_paths = {route.path for route in app_http_routes}

        stale = [
            f"  {path}"
            for path in sorted(_IN_BAND_AUTH_PATHS)
            if path not in registered_paths
        ]

        assert not stale, (
            "In-band auth path list contains paths that do not exist in the app.\n\n"
            "Remove these stale entries from _IN_BAND_AUTH_PATHS in\n"
            "tests/unit/test_auth_coverage.py:\n\n"
            + "\n".join(stale)
        )

    @pytest.mark.unit
    def test_admin_routes_require_admin_role(self, app_http_routes: list[APIRoute]) -> None:
        """
        Assert that every route under /admin/* requires the admin role specifically.

        The admin subsystem (user management, training console, strategy governance)
        must never be accessible to viewer or trader roles.  This test enforces the
        tighter guarantee: admin paths need require_admin_role, not just any auth dep.
        """
        violations: list[str] = []

        for route in app_http_routes:
            path = route.path
            if not path.startswith("/api/v1/admin/"):
                continue

            # Check that require_admin_role is in the dependency tree
            has_admin = any(
                c is require_admin_role
                for c in _iter_dep_callables(route.dependant)
            )
            if not has_admin:
                methods = sorted(route.methods or set())
                violations.append(f"  {', '.join(methods):8s}  {path}")

        assert not violations, (
            "Admin routes found without require_admin_role dependency.\n\n"
            "All /admin/* routes must enforce require_admin_role (or AdminUserID)\n"
            "to prevent privilege escalation by trader/viewer roles.\n\n"
            "Affected routes:\n" + "\n".join(violations)
        )

    @pytest.mark.unit
    def test_auth_sentinels_are_callable(self) -> None:
        """
        Sanity check: confirm the auth sentinels are importable callables.

        If either import path changes, this test fails fast with a clear
        ImportError rather than silently passing all coverage checks.
        """
        import inspect

        for sentinel in _AUTH_SENTINELS:
            assert callable(sentinel), (
                f"Auth sentinel {sentinel!r} is not callable — "
                "update _AUTH_SENTINELS in test_auth_coverage.py"
            )
            # Must be async (FastAPI deps run in async context)
            assert inspect.iscoroutinefunction(sentinel), (
                f"Auth sentinel {sentinel!r} must be an async function"
            )
