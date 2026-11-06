import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.core.database import get_engine, get_session_factory
from app.modules.auth.models import User
from app.modules.projects import repository, service
from app.modules.projects.errors import (
    AlreadyMemberError,
    InsufficientProjectRoleError,
    MemberNotFoundError,
    OwnerRoleChangeForbiddenError,
)
from app.modules.projects.models import ProjectMember, ProjectRole
from app.modules.projects.schemas import ProjectAccess
from tests.projects.conftest import ORIGIN, Caller

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

PROJECTS = "/api/v1/projects"
NOT_FOUND = {"error": {"code": "project_not_found", "message": "Project not found."}}
FORBIDDEN_CODE = "insufficient_project_role"
OWNER_FORBIDDEN = {
    "error": {
        "code": "owner_role_change_forbidden",
        "message": "The Owner cannot be changed or removed here. Transfer ownership first.",
    }
}

MakeCaller = Callable[[str], Caller]
AddMember = Callable[..., None]


def _members_url(project_id: Any, user_id: Any | None = None) -> str:
    base = f"{PROJECTS}/{project_id}/members"
    return base if user_id is None else f"{base}/{user_id}"


def _roles(project_id: Any) -> dict[uuid.UUID, str]:
    with get_session_factory()() as session:
        rows = session.execute(
            select(ProjectMember.user_id, ProjectMember.role).where(ProjectMember.project_id == uuid.UUID(str(project_id)))
        ).tuples()
        return dict(rows.all())


def _owner_count(project_id: Any) -> int:
    return list(_roles(project_id).values()).count("owner")


@pytest.fixture
def team(client: TestClient, make_caller: MakeCaller, add_member: AddMember) -> dict[str, Any]:
    """A project with Owner, two Admins and two Members; an outsider with their own project; and
    a registered user (`candidate`) who belongs to nothing yet."""
    names = ("owner", "admin", "admin2", "member", "member2", "outsider", "candidate")
    callers = {name: make_caller(f"{name}@example.com") for name in names}
    project = client.post(PROJECTS, json={"name": "Team"}, headers=callers["owner"].headers).json()
    for name, role in (("admin", "admin"), ("admin2", "admin"), ("member", "member"), ("member2", "member")):
        add_member(project["id"], callers[name].user, ProjectRole(role))
    other = client.post(PROJECTS, json={"name": "Other"}, headers=callers["outsider"].headers).json()
    return {**callers, "project": project["id"], "other": other["id"]}


def _id(team: dict[str, Any], name: str) -> uuid.UUID:
    return team[name].user.id


# --- List -------------------------------------------------------------------------------------


@pytest.mark.parametrize("viewer", ["owner", "admin", "member"])
def test_every_member_can_list_members(client: TestClient, team: dict[str, Any], viewer: str) -> None:
    response = client.get(_members_url(team["project"]), headers=team[viewer].headers)

    assert response.status_code == 200
    body = response.json()
    assert [(m["email"], m["role"]) for m in body] == [
        ("owner@example.com", "owner"),
        ("admin2@example.com", "admin"),
        ("admin@example.com", "admin"),
        ("member2@example.com", "member"),
        ("member@example.com", "member"),
    ]
    assert all(set(m) == {"user_id", "email", "role", "is_active", "created_at"} for m in body)
    assert all(m["is_active"] for m in body)


def test_member_list_exposes_no_credentials(client: TestClient, team: dict[str, Any]) -> None:
    text = client.get(_members_url(team["project"]), headers=team["owner"].headers).text

    for leaked in ("password", "$argon2", "hash", "token", "Bearer"):
        assert leaked not in text


def test_inactive_members_stay_visible(client: TestClient, team: dict[str, Any]) -> None:
    with get_session_factory()() as session:
        session.get_one(User, _id(team, "member")).is_active = False
        session.commit()

    body = client.get(_members_url(team["project"]), headers=team["owner"].headers).json()

    [member] = [m for m in body if m["email"] == "member@example.com"]
    assert member["is_active"] is False and member["role"] == "member"


def test_non_member_cannot_list_members(client: TestClient, team: dict[str, Any]) -> None:
    response = client.get(_members_url(team["project"]), headers=team["outsider"].headers)

    assert (response.status_code, response.json()) == (404, NOT_FOUND)


@contextmanager
def _recorded_selects() -> Iterator[list[str]]:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_listing_members_uses_two_queries_regardless_of_size(team: dict[str, Any], make_caller: MakeCaller) -> None:
    """One query for memberships, one batched user lookup: no N+1."""
    access = ProjectAccess(project_id=team["project"], user_id=_id(team, "owner"), role=ProjectRole.OWNER)
    with get_session_factory()() as session:
        for i in range(10):
            session.add(
                ProjectMember(project_id=uuid.UUID(team["project"]), user_id=make_caller(f"x{i}@example.com").user.id, role="member")
            )
        session.commit()

    with get_session_factory()() as session, _recorded_selects() as selects:
        members = service.list_members(session, access=access)

    assert len(members) == 15
    assert len(selects) == 2


# --- Add ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["owner", "admin"])
@pytest.mark.parametrize("role", ["admin", "member"])
def test_owner_and_admin_can_add_members(client: TestClient, team: dict[str, Any], actor: str, role: str) -> None:
    response = client.post(
        _members_url(team["project"]), json={"email": "candidate@example.com", "role": role}, headers=team[actor].headers
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["user_id"], body["email"], body["role"], body["is_active"]) == (
        str(_id(team, "candidate")),
        "candidate@example.com",
        role,
        True,
    )
    assert _roles(team["project"])[_id(team, "candidate")] == role
    # The new member now sees the project.
    assert client.get(f"{PROJECTS}/{team['project']}", headers=team["candidate"].headers).json()["role"] == role


@pytest.mark.parametrize("email", ["CANDIDATE@example.com", "  Candidate@Example.COM  "])
def test_add_normalizes_the_email_like_authentication(client: TestClient, team: dict[str, Any], email: str) -> None:
    response = client.post(
        _members_url(team["project"]), json={"email": email, "role": "member"}, headers=team["owner"].headers
    )

    assert response.status_code == 201
    assert response.json()["email"] == "candidate@example.com"


def test_add_unknown_user(client: TestClient, team: dict[str, Any]) -> None:
    response = client.post(
        _members_url(team["project"]), json={"email": "nobody@example.com", "role": "member"}, headers=team["owner"].headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"
    assert len(_roles(team["project"])) == 5


def test_add_inactive_user(client: TestClient, team: dict[str, Any]) -> None:
    with get_session_factory()() as session:
        session.get_one(User, _id(team, "candidate")).is_active = False
        session.commit()

    response = client.post(
        _members_url(team["project"]), json={"email": "candidate@example.com", "role": "member"}, headers=team["owner"].headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "user_inactive"
    assert _id(team, "candidate") not in _roles(team["project"])


@pytest.mark.parametrize("existing", ["owner", "admin", "member"])
def test_add_existing_member_is_rejected(client: TestClient, team: dict[str, Any], existing: str) -> None:
    before = _roles(team["project"])

    response = client.post(
        _members_url(team["project"]), json={"email": f"{existing}@example.com", "role": "admin"}, headers=team["owner"].headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_member"
    assert _roles(team["project"]) == before


@pytest.mark.parametrize("role", ["owner", "Owner", "viewer", ""])
def test_owner_role_cannot_be_assigned(client: TestClient, team: dict[str, Any], role: str) -> None:
    response = client.post(
        _members_url(team["project"]), json={"email": "candidate@example.com", "role": role}, headers=team["owner"].headers
    )

    assert response.status_code == 422
    assert _id(team, "candidate") not in _roles(team["project"])
    assert _owner_count(team["project"]) == 1


@pytest.mark.parametrize("body", [{}, {"email": "candidate@example.com"}, {"email": "not-an-email", "role": "member"}])
def test_add_rejects_invalid_bodies(client: TestClient, team: dict[str, Any], body: dict[str, str]) -> None:
    assert client.post(_members_url(team["project"]), json=body, headers=team["owner"].headers).status_code == 422


def test_member_cannot_add(client: TestClient, team: dict[str, Any]) -> None:
    response = client.post(
        _members_url(team["project"]), json={"email": "candidate@example.com", "role": "member"}, headers=team["member"].headers
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == FORBIDDEN_CODE
    assert _id(team, "candidate") not in _roles(team["project"])


def test_non_member_cannot_add(client: TestClient, team: dict[str, Any]) -> None:
    response = client.post(
        _members_url(team["project"]), json={"email": "candidate@example.com", "role": "member"}, headers=team["outsider"].headers
    )

    assert (response.status_code, response.json()) == (404, NOT_FOUND)


# --- Change role --------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["owner", "admin"])
@pytest.mark.parametrize(("target", "new_role"), [("member", "admin"), ("admin2", "member"), ("admin2", "admin")])
def test_owner_and_admin_change_roles(
    client: TestClient, team: dict[str, Any], actor: str, target: str, new_role: str
) -> None:
    response = client.patch(
        _members_url(team["project"], _id(team, target)), json={"role": new_role}, headers=team[actor].headers
    )

    assert response.status_code == 200
    assert (response.json()["user_id"], response.json()["role"]) == (str(_id(team, target)), new_role)
    assert _roles(team["project"])[_id(team, target)] == new_role
    assert _owner_count(team["project"]) == 1


def test_admin_can_demote_themselves(client: TestClient, team: dict[str, Any]) -> None:
    url = _members_url(team["project"], _id(team, "admin"))

    assert client.patch(url, json={"role": "member"}, headers=team["admin"].headers).status_code == 200
    # Now a Member: no more management rights.
    assert client.patch(url, json={"role": "admin"}, headers=team["admin"].headers).status_code == 403


@pytest.mark.parametrize("actor", ["owner", "admin"])
@pytest.mark.parametrize("new_role", ["admin", "member"])
def test_the_owners_role_never_changes_here(client: TestClient, team: dict[str, Any], actor: str, new_role: str) -> None:
    response = client.patch(
        _members_url(team["project"], _id(team, "owner")), json={"role": new_role}, headers=team[actor].headers
    )

    assert (response.status_code, response.json()) == (409, OWNER_FORBIDDEN)
    assert _roles(team["project"])[_id(team, "owner")] == "owner"


def test_nobody_is_promoted_to_owner(client: TestClient, team: dict[str, Any]) -> None:
    response = client.patch(
        _members_url(team["project"], _id(team, "admin")), json={"role": "owner"}, headers=team["owner"].headers
    )

    assert response.status_code == 422
    assert _owner_count(team["project"]) == 1


@pytest.mark.parametrize("target", ["member", "member2", "admin", "owner"])
def test_member_cannot_change_any_role_including_their_own(client: TestClient, team: dict[str, Any], target: str) -> None:
    before = _roles(team["project"])

    response = client.patch(
        _members_url(team["project"], _id(team, target)), json={"role": "admin"}, headers=team["member"].headers
    )

    assert response.status_code == 403
    assert _roles(team["project"]) == before


def test_change_role_of_a_missing_member(client: TestClient, team: dict[str, Any]) -> None:
    for user_id in (uuid.uuid4(), _id(team, "candidate"), _id(team, "outsider")):
        response = client.patch(
            _members_url(team["project"], user_id), json={"role": "admin"}, headers=team["owner"].headers
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "member_not_found"


@pytest.mark.parametrize("body", [{}, {"role": "Admin"}, {"role": "member", "email": "x@example.com"}])
def test_change_role_rejects_invalid_bodies(client: TestClient, team: dict[str, Any], body: dict[str, str]) -> None:
    url = _members_url(team["project"], _id(team, "member"))

    assert client.patch(url, json=body, headers=team["owner"].headers).status_code == 422


def test_non_member_cannot_change_roles(client: TestClient, team: dict[str, Any]) -> None:
    response = client.patch(
        _members_url(team["project"], _id(team, "member")), json={"role": "admin"}, headers=team["outsider"].headers
    )

    assert (response.status_code, response.json()) == (404, NOT_FOUND)


# --- Remove and leave ---------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["owner", "admin"])
@pytest.mark.parametrize("target", ["member", "admin2"])
def test_owner_and_admin_remove_others(client: TestClient, team: dict[str, Any], actor: str, target: str) -> None:
    response = client.delete(_members_url(team["project"], _id(team, target)), headers=team[actor].headers)

    assert response.status_code == 204
    assert _id(team, target) not in _roles(team["project"])
    assert client.get(f"{PROJECTS}/{team['project']}", headers=team[target].headers).status_code == 404


@pytest.mark.parametrize("leaver", ["admin", "member"])
def test_admins_and_members_can_leave(client: TestClient, team: dict[str, Any], leaver: str) -> None:
    response = client.delete(_members_url(team["project"], _id(team, leaver)), headers=team[leaver].headers)

    assert response.status_code == 204
    assert _id(team, leaver) not in _roles(team["project"])
    assert client.get(PROJECTS, headers=team[leaver].headers).json() == []


def test_owner_cannot_leave(client: TestClient, team: dict[str, Any]) -> None:
    response = client.delete(_members_url(team["project"], _id(team, "owner")), headers=team["owner"].headers)

    assert (response.status_code, response.json()) == (409, OWNER_FORBIDDEN)
    assert _owner_count(team["project"]) == 1


def test_admin_cannot_remove_the_owner(client: TestClient, team: dict[str, Any]) -> None:
    response = client.delete(_members_url(team["project"], _id(team, "owner")), headers=team["admin"].headers)

    assert (response.status_code, response.json()) == (409, OWNER_FORBIDDEN)
    assert _owner_count(team["project"]) == 1


@pytest.mark.parametrize("target", ["member2", "admin", "owner"])
def test_member_cannot_remove_others(client: TestClient, team: dict[str, Any], target: str) -> None:
    response = client.delete(_members_url(team["project"], _id(team, target)), headers=team["member"].headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == FORBIDDEN_CODE
    assert _id(team, target) in _roles(team["project"])


def test_repeated_removal_returns_404(client: TestClient, team: dict[str, Any]) -> None:
    url = _members_url(team["project"], _id(team, "member"))

    assert client.delete(url, headers=team["owner"].headers).status_code == 204
    second = client.delete(url, headers=team["owner"].headers)
    assert second.status_code == 404
    assert second.json()["error"]["code"] == "member_not_found"


def test_removing_a_non_member(client: TestClient, team: dict[str, Any]) -> None:
    for user_id in (uuid.uuid4(), _id(team, "candidate")):
        response = client.delete(_members_url(team["project"], user_id), headers=team["admin"].headers)
        assert response.json()["error"]["code"] == "member_not_found"


def test_leaving_twice_returns_404_project(client: TestClient, team: dict[str, Any]) -> None:
    """After leaving, the user is no longer a member: the project itself is now 404 for them."""
    url = _members_url(team["project"], _id(team, "member"))
    assert client.delete(url, headers=team["member"].headers).status_code == 204

    assert client.delete(url, headers=team["member"].headers).json() == NOT_FOUND


def test_non_member_cannot_remove(client: TestClient, team: dict[str, Any]) -> None:
    response = client.delete(_members_url(team["project"], _id(team, "member")), headers=team["outsider"].headers)

    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert _id(team, "member") in _roles(team["project"])


# --- Cross-project isolation --------------------------------------------------------------------


def test_admin_of_one_project_cannot_manage_another(client: TestClient, team: dict[str, Any], add_member: AddMember) -> None:
    add_member(team["other"], team["admin"].user, ProjectRole.MEMBER)
    headers = team["admin"].headers

    assert client.post(
        _members_url(team["other"]), json={"email": "candidate@example.com", "role": "member"}, headers=headers
    ).status_code == 403
    assert client.delete(_members_url(team["other"], _id(team, "outsider")), headers=headers).status_code == 403
    # Not a member of the other project at all: 404, and nothing about its members leaks.
    assert client.get(_members_url(team["other"]), headers=team["member"].headers).json() == NOT_FOUND


def test_membership_lookups_are_scoped_by_project(client: TestClient, team: dict[str, Any]) -> None:
    """The outsider is the Owner of the other project, not of this one: managing them here finds nothing."""
    url = _members_url(team["project"], _id(team, "outsider"))

    assert client.patch(url, json={"role": "member"}, headers=team["owner"].headers).status_code == 404
    assert client.delete(url, headers=team["owner"].headers).status_code == 404
    assert _roles(team["other"])[_id(team, "outsider")] == "owner"


# --- Origin ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("origin_headers", [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}])
def test_every_member_write_requires_an_allowed_origin(
    client: TestClient, team: dict[str, Any], origin_headers: dict[str, str]
) -> None:
    before = _roles(team["project"])
    writes = [
        ("POST", _members_url(team["project"]), {"json": {"email": "candidate@example.com", "role": "member"}}),
        ("PATCH", _members_url(team["project"], _id(team, "member")), {"json": {"role": "admin"}}),
        ("DELETE", _members_url(team["project"], _id(team, "member")), {}),
        ("DELETE", _members_url(team["project"], _id(team, "member2")), {}),  # leaving
    ]
    client.headers.pop("Origin")
    try:
        for (method, url, kwargs), caller in zip(writes, ("owner", "owner", "owner", "member2"), strict=True):
            response = client.request(method, url, headers={**team[caller].headers, **origin_headers}, **kwargs)
            assert response.status_code == 403, (method, url)
            assert response.json()["error"]["code"] == "origin_not_allowed"
        # Reads need no Origin.
        assert client.get(_members_url(team["project"]), headers=team["member"].headers).status_code == 200
    finally:
        client.headers["Origin"] = ORIGIN

    assert _roles(team["project"]) == before


def test_membership_changes_log_ids_only(client: TestClient, team: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        client.post(
            _members_url(team["project"]), json={"email": "candidate@example.com", "role": "member"}, headers=team["owner"].headers
        )
        client.patch(_members_url(team["project"], _id(team, "candidate")), json={"role": "admin"}, headers=team["owner"].headers)
        client.delete(_members_url(team["project"], _id(team, "candidate")), headers=team["owner"].headers)

    messages = {r.getMessage() for r in caplog.records}
    assert {"Project member added", "Project member role changed", "Project member removed"} <= messages
    assert "candidate@example.com" not in caplog.text


# --- Rollback and concurrency (service level) -----------------------------------------------------


def _access(team: dict[str, Any], name: str, role: ProjectRole) -> ProjectAccess:
    return ProjectAccess(project_id=team["project"], user_id=_id(team, name), role=role)


def _boom(*args: object, **kwargs: object) -> None:
    raise RuntimeError("injected failure")


@pytest.mark.parametrize("operation", ["add", "change", "remove"])
def test_failures_roll_back(team: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    before = _roles(team["project"])
    owner = _access(team, "owner", ProjectRole.OWNER)

    with get_session_factory()() as session:
        if operation == "add":
            monkeypatch.setattr(repository, "insert_member", _boom)
            call = lambda: service.add_member(session, access=owner, email="candidate@example.com", role=ProjectRole.MEMBER)  # noqa: E731
        elif operation == "change":
            monkeypatch.setattr(session, "commit", _boom)
            call = lambda: service.change_member_role(session, access=owner, user_id=_id(team, "member"), role=ProjectRole.ADMIN)  # noqa: E731
        else:
            monkeypatch.setattr(repository, "delete_member", _boom)
            call = lambda: service.remove_member(session, access=owner, user_id=_id(team, "member"))  # noqa: E731
        with pytest.raises(RuntimeError, match="injected failure"):
            call()
        assert not session.in_transaction()
        monkeypatch.undo()

    assert _roles(team["project"]) == before


def test_service_refuses_the_owner_role_even_if_called_directly(team: dict[str, Any]) -> None:
    owner = _access(team, "owner", ProjectRole.OWNER)

    with get_session_factory()() as session:
        with pytest.raises(OwnerRoleChangeForbiddenError):
            service.add_member(session, access=owner, email="candidate@example.com", role=ProjectRole.OWNER)
        with pytest.raises(OwnerRoleChangeForbiddenError):
            service.change_member_role(session, access=owner, user_id=_id(team, "admin"), role=ProjectRole.OWNER)
    assert _owner_count(team["project"]) == 1


def _in_thread(target: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(target())
        except BaseException as exc:  # noqa: BLE001 - asserted by the caller
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


@pytest.mark.parametrize("operation", ["remove", "change", "add"])
def test_a_concurrent_demotion_is_seen_under_the_lock(team: dict[str, Any], operation: str) -> None:
    """The admin's request passed the dependency as Admin. While it waits for the project lock,
    the Owner demotes them. Once the lock is released the request runs against committed state:
    refused, with nothing changed."""
    stale_admin = _access(team, "admin", ProjectRole.ADMIN)
    before = _roles(team["project"])

    owner_session = get_session_factory()()
    try:
        assert repository.lock_project(owner_session, uuid.UUID(team["project"])) is not None
        owner_session.get_one(ProjectMember, (uuid.UUID(team["project"]), _id(team, "admin"))).role = "member"
        owner_session.flush()

        def admin_request() -> object:
            with get_session_factory()() as session:
                if operation == "remove":
                    return service.remove_member(session, access=stale_admin, user_id=_id(team, "member"))
                if operation == "change":
                    return service.change_member_role(
                        session, access=stale_admin, user_id=_id(team, "member"), role=ProjectRole.ADMIN
                    )
                return service.add_member(
                    session, access=stale_admin, email="candidate@example.com", role=ProjectRole.MEMBER
                )

        thread, outcome = _in_thread(admin_request)
        time.sleep(0.5)
        assert thread.is_alive(), "the admin's request must wait for the project lock"
        owner_session.commit()
    finally:
        owner_session.close()
    thread.join(timeout=10)

    assert isinstance(outcome[0], InsufficientProjectRoleError), outcome
    assert _roles(team["project"]) == {**before, _id(team, "admin"): "member"}


def _concurrently(monkeypatch: pytest.MonkeyPatch, calls: list[Callable[[], object]]) -> list[object]:
    """Run the calls in threads that all pass their pre-lock checks before any takes the lock."""
    barrier = threading.Barrier(len(calls), timeout=10)
    original = repository.lock_project

    def lock_after_everyone_arrived(session, project_id):  # type: ignore[no-untyped-def]
        barrier.wait()
        return original(session, project_id)

    monkeypatch.setattr(repository, "lock_project", lock_after_everyone_arrived)
    started = [_in_thread(call) for call in calls]
    for thread, _ in started:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread, _ in started)
    return [outcome[0] for _, outcome in started]


def test_concurrent_adds_of_the_same_user(team: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def add(name: str) -> Callable[[], object]:
        def call() -> object:
            with get_session_factory()() as session:
                return service.add_member(
                    session, access=_access(team, name, ProjectRole.ADMIN), email="candidate@example.com", role=ProjectRole.MEMBER
                )

        return call

    results = _concurrently(monkeypatch, [add("admin"), add("admin2")])

    assert sorted(type(r).__name__ for r in results) == ["AlreadyMemberError", "MemberRead"]
    assert _roles(team["project"])[_id(team, "candidate")] == "member"


def test_concurrent_removals_of_the_same_member(team: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def remove(name: str) -> Callable[[], object]:
        def call() -> object:
            with get_session_factory()() as session:
                service.remove_member(session, access=_access(team, name, ProjectRole.ADMIN), user_id=_id(team, "member"))
                return "removed"

        return call

    results = _concurrently(monkeypatch, [remove("admin"), remove("admin2")])

    assert sorted(r if isinstance(r, str) else type(r).__name__ for r in results) == ["MemberNotFoundError", "removed"]
    assert _id(team, "member") not in _roles(team["project"])


def test_admins_removing_each_other_concurrently(team: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Each passed the dependency as Admin. Serialized by the lock, the second runs after the first
    removed it from the project: it is no longer a member, so it is refused."""

    def remove(actor: str, target: str) -> Callable[[], object]:
        def call() -> object:
            with get_session_factory()() as session:
                service.remove_member(session, access=_access(team, actor, ProjectRole.ADMIN), user_id=_id(team, target))
                return "removed"

        return call

    results = _concurrently(monkeypatch, [remove("admin", "admin2"), remove("admin2", "admin")])

    assert sorted(r if isinstance(r, str) else type(r).__name__ for r in results) == ["ProjectNotFoundError", "removed"]
    remaining = _roles(team["project"])
    assert (_id(team, "admin") in remaining) != (_id(team, "admin2") in remaining)
    assert _owner_count(team["project"]) == 1


def test_member_errors_are_typed(team: dict[str, Any]) -> None:
    owner = _access(team, "owner", ProjectRole.OWNER)
    with get_session_factory()() as session:
        with pytest.raises(AlreadyMemberError):
            service.add_member(session, access=owner, email="member@example.com", role=ProjectRole.MEMBER)
        with pytest.raises(MemberNotFoundError):
            service.remove_member(session, access=owner, user_id=uuid.uuid4())
