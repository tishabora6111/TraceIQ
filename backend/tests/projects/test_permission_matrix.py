"""The consolidated permission matrix (ARCHITECTURE §7.3) for every projects route.

``ROUTES`` lists every route under ``/api/v1/projects``. ``test_every_project_route_is_in_the_matrix``
fails when a route is added without a row here, so new routes cannot skip this suite. Every route
is exercised by every caller state, and a refused request must leave the project unchanged.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.database import get_session_factory
from app.modules.auth.dependencies import get_current_user, require_allowed_origin_for_writes
from app.modules.auth.models import User
from app.modules.projects import repository, service
from app.modules.projects.models import ApiKey, Project, ProjectMember, ProjectRole
from app.modules.projects.schemas import ProjectAccess
from tests.projects.conftest import ORIGIN, Caller

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

PROJECTS = "/api/v1/projects"
PROJECT_NAME = "Team"
# "user": any signed-in, active user; otherwise the minimum project role.
ANY_USER = "user"
RANK = {ProjectRole.MEMBER: 0, ProjectRole.ADMIN: 1, ProjectRole.OWNER: 2}

MakeCaller = Callable[[str], Caller]
AddMember = Callable[..., None]


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    # Who may call it successfully, in this suite's setup.
    minimum: str
    # The project-role dependency the route declares (None for routes without ``{project_id}``).
    dependency: ProjectRole | None
    success: int
    body: Callable[[dict[str, Any]], Any] | None = None

    @property
    def id(self) -> str:
        return f"{self.method} {self.path.removeprefix(PROJECTS) or '/'}"


P = f"{PROJECTS}/{{project_id}}"
ROUTES = [
    Route("POST", PROJECTS, ANY_USER, None, 201, lambda w: {"name": "Fresh"}),
    Route("GET", PROJECTS, ANY_USER, None, 200),
    Route("GET", P, ProjectRole.MEMBER, ProjectRole.MEMBER, 200),
    Route("PATCH", P, ProjectRole.ADMIN, ProjectRole.ADMIN, 200, lambda w: {"name": "Renamed"}),
    Route("DELETE", P, ProjectRole.OWNER, ProjectRole.OWNER, 204, lambda w: {"confirm_name": PROJECT_NAME}),
    Route("GET", f"{P}/members", ProjectRole.MEMBER, ProjectRole.MEMBER, 200),
    Route(
        "POST", f"{P}/members", ProjectRole.ADMIN, ProjectRole.ADMIN, 201,
        lambda w: {"email": "candidate@example.com", "role": "member"},
    ),
    Route("PATCH", f"{P}/members/{{user_id}}", ProjectRole.ADMIN, ProjectRole.ADMIN, 200, lambda w: {"role": "admin"}),
    # Any member may call it (to leave); removing someone else, as here, needs Admin.
    Route("DELETE", f"{P}/members/{{user_id}}", ProjectRole.ADMIN, ProjectRole.MEMBER, 204),
    Route(
        "POST", f"{P}/ownership-transfer", ProjectRole.OWNER, ProjectRole.OWNER, 200,
        lambda w: {"user_id": str(w["subject"].user.id)},
    ),
    Route("GET", f"{P}/retention", ProjectRole.MEMBER, ProjectRole.MEMBER, 200),
    Route("PATCH", f"{P}/retention", ProjectRole.ADMIN, ProjectRole.ADMIN, 200, lambda w: {"traces_days": 7}),
    Route("POST", f"{P}/api-keys", ProjectRole.ADMIN, ProjectRole.ADMIN, 201, lambda w: {"name": "CI"}),
    Route("GET", f"{P}/api-keys", ProjectRole.ADMIN, ProjectRole.ADMIN, 200),
    Route("POST", f"{P}/api-keys/{{api_key_id}}/revoke", ProjectRole.ADMIN, ProjectRole.ADMIN, 200),
]

# Caller states. "inactive" is an Admin whose account was deactivated after signing in;
# "stranger" belongs to no project; "outsider" owns a different project.
CALLERS = ["owner", "admin", "member", "stranger", "outsider", "inactive", "no_token"]


@pytest.fixture
def world(client: TestClient, make_caller: MakeCaller, add_member: AddMember) -> dict[str, Any]:
    names = ("owner", "admin", "member", "subject", "stranger", "outsider", "inactive")
    callers = {name: make_caller(f"{name}@example.com") for name in names}
    make_caller("candidate@example.com")
    project = client.post(PROJECTS, json={"name": PROJECT_NAME}, headers=callers["owner"].headers).json()["id"]
    add_member(project, callers["admin"].user, ProjectRole.ADMIN)
    add_member(project, callers["member"].user, ProjectRole.MEMBER)
    add_member(project, callers["subject"].user, ProjectRole.MEMBER)
    add_member(project, callers["inactive"].user, ProjectRole.ADMIN)
    client.post(PROJECTS, json={"name": "Other"}, headers=callers["outsider"].headers)
    with get_session_factory()() as session:
        access = ProjectAccess(project_id=project, user_id=callers["owner"].user.id, role=ProjectRole.OWNER)
        api_key = service.create_api_key(session, access=access, name="Existing")
    with get_session_factory()() as session:
        session.get_one(User, callers["inactive"].user.id).is_active = False
        session.commit()
    return {**callers, "project": project, "api_key_id": str(api_key.id)}


def _url(route: Route, world: dict[str, Any]) -> str:
    return route.path.format(
        project_id=world["project"], user_id=world["subject"].user.id, api_key_id=world["api_key_id"]
    )


def _headers(world: dict[str, Any], caller: str) -> dict[str, str]:
    return {} if caller == "no_token" else world[caller].headers


def _call(client: TestClient, route: Route, world: dict[str, Any], caller: str):
    body = route.body(world) if route.body else None
    return client.request(route.method, _url(route, world), json=body, headers=_headers(world, caller))


def _snapshot(project_id: str) -> Any:
    """Everything a projects route can change about the project."""
    pid = uuid.UUID(project_id)
    with get_session_factory()() as session:
        project = session.get(Project, pid)
        members = session.execute(
            select(ProjectMember.user_id, ProjectMember.role).where(ProjectMember.project_id == pid)
        ).all()
        keys = session.execute(
            select(ApiKey.id, ApiKey.revoked_at.is_(None)).where(ApiKey.project_id == pid)
        ).all()
        return (
            project and (project.name, project.description, project.retention_overrides),
            sorted(members),
            sorted(keys),
        )


def _expected(route: Route, caller: str) -> tuple[int, str | None]:
    if caller in ("no_token", "inactive"):
        return 401, "unauthorized"
    if route.minimum == ANY_USER:
        return route.success, None
    if caller in ("stranger", "outsider"):
        return 404, "project_not_found"
    if RANK[ProjectRole(caller)] >= RANK[ProjectRole(route.minimum)]:
        return route.success, None
    return 403, "insufficient_project_role"


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.id)
def test_permission_matrix(client: TestClient, world: dict[str, Any], route: Route, caller: str) -> None:
    before = _snapshot(world["project"])

    response = _call(client, route, world, caller)

    status, code = _expected(route, caller)
    assert response.status_code == status, response.text
    if code is not None:
        assert response.json()["error"]["code"] == code
        assert _snapshot(world["project"]) == before
    elif route.method == "GET" or route.minimum == ANY_USER:
        assert _snapshot(world["project"]) == before


@pytest.mark.parametrize("origin_headers", [{}, {"Origin": "https://evil.example"}])
@pytest.mark.parametrize("route", [r for r in ROUTES if r.method != "GET"], ids=lambda r: r.id)
def test_every_write_requires_an_allowed_origin(
    client: TestClient, world: dict[str, Any], route: Route, origin_headers: dict[str, str]
) -> None:
    """Even the Owner, with a valid token, is refused without an allowed Origin."""
    before = _snapshot(world["project"])
    client.headers.pop("Origin")
    try:
        response = client.request(
            route.method,
            _url(route, world),
            json=route.body(world) if route.body else None,
            headers={**world["owner"].headers, **origin_headers},
        )
    finally:
        client.headers["Origin"] = ORIGIN

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert _snapshot(world["project"]) == before


@pytest.mark.parametrize("route", [r for r in ROUTES if r.method == "GET"], ids=lambda r: r.id)
def test_reads_need_no_origin(client: TestClient, world: dict[str, Any], route: Route) -> None:
    client.headers.pop("Origin")
    try:
        response = client.get(_url(route, world), headers=world["owner"].headers)
    finally:
        client.headers["Origin"] = ORIGIN

    assert response.status_code == 200


# --- Route inventory -----------------------------------------------------------------------------


def _calls(dependant: Any) -> list[Callable[..., Any]]:
    """Every dependency callable a route resolves, recursively (router-level ones included)."""
    found = []
    for dependency in dependant.dependencies:
        found.append(dependency.call)
        found.extend(_calls(dependency))
    return found


def _module_routes() -> list[tuple[str, str, APIRoute]]:
    """(method, mounted path, route) for every route of every router the app mounts.

    Read from the module routers rather than ``app.routes``, whose included-router entries are
    internal to FastAPI. ``test_the_router_list_covers_the_app`` keeps this list complete.
    """
    from app.main import API_PREFIX
    from app.modules.auth.api import router as auth_router
    from app.modules.health.api import router as health_router
    from app.modules.projects.api import router as projects_router

    mounted = [("", health_router), (API_PREFIX, auth_router), (API_PREFIX, projects_router)]
    return [
        (method, prefix + route.path, route)
        for prefix, router in mounted
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    ]


def test_the_router_list_covers_the_app(settings: Settings) -> None:
    """Every operation the app serves (from its OpenAPI schema) comes from a router listed above."""
    from app.main import create_app

    served = {
        (method.upper(), path)
        for path, operations in create_app(settings).openapi()["paths"].items()
        for method in operations
    }

    assert served == {(method, path) for method, path, _ in _module_routes()}


def test_every_project_route_is_in_the_matrix() -> None:
    mounted = {
        (method, path)
        for method, path, route in _module_routes()
        if path == PROJECTS or path.startswith(f"{PROJECTS}/")
    }

    assert mounted == {(route.method, route.path) for route in ROUTES}


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.id)
def test_route_dependencies(route: Route) -> None:
    [api_route] = [r for method, path, r in _module_routes() if (method, path) == (route.method, route.path)]
    calls = _calls(api_route.dependant)
    role_checks = [call.__name__ for call in calls if call.__name__.startswith("require_project_role_")]

    assert api_route.include_in_schema
    assert require_allowed_origin_for_writes in calls
    assert get_current_user in calls
    if route.dependency is None:
        assert role_checks == []
    else:
        assert role_checks == [f"require_project_role_{route.dependency.value}"]


def test_every_project_scoped_route_in_the_app_checks_the_project_role() -> None:
    """Not only the projects router: any route anywhere with a ``{project_id}`` must check it."""
    scoped = [(path, route) for _, path, route in _module_routes() if "{project_id}" in path]

    assert scoped
    for path, route in scoped:
        names = [call.__name__ for call in _calls(route.dependant)]
        assert any(name.startswith("require_project_role_") for name in names), path


# --- Stale authorization over HTTP ----------------------------------------------------------------

# Admin routes whose service must refuse an Admin demoted after the dependency check.
_ADMIN_WRITES = [r for r in ROUTES if r.minimum == ProjectRole.ADMIN and r.method != "GET"]


@pytest.mark.parametrize("route", _ADMIN_WRITES, ids=lambda r: r.id)
def test_admin_demoted_between_dependency_and_lock_is_refused(
    client: TestClient, world: dict[str, Any], route: Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request's own session already read the Admin role (the dependency); another session
    then demotes the Admin and commits, before the service takes the project lock. Under
    READ COMMITTED the re-check under the lock sees the demotion and refuses the request."""
    original = repository.lock_project
    demoted: list[bool] = []

    def demote_then_lock(session, project_id):  # type: ignore[no-untyped-def]
        assert session.in_transaction(), "the dependency's read must already be in this transaction"
        with get_session_factory()() as other:
            repository.set_member_role(
                other, project_id=project_id, user_id=world["admin"].user.id, role=ProjectRole.MEMBER
            )
            other.commit()
        demoted.append(True)
        return original(session, project_id)

    monkeypatch.setattr(repository, "lock_project", demote_then_lock)
    before = _snapshot(world["project"])

    response = _call(client, route, world, "admin")

    assert demoted == [True]
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_project_role"
    after = _snapshot(world["project"])
    expected_members = [
        (user_id, "member" if user_id == world["admin"].user.id else role) for user_id, role in before[1]
    ]
    assert after == (before[0], expected_members, before[2])
