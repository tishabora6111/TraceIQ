import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import get_session_factory
from app.modules.auth.models import User
from app.modules.projects import repository, service
from app.modules.projects.errors import (
    InsufficientProjectRoleError,
    OwnershipTransferToSelfError,
    ProjectNotFoundError,
)
from app.modules.projects.models import ProjectMember, ProjectRole
from app.modules.projects.schemas import OwnershipTransferResult, ProjectAccess
from tests.projects.conftest import ORIGIN, Caller

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

PROJECTS = "/api/v1/projects"
NOT_FOUND = {"error": {"code": "project_not_found", "message": "Project not found."}}

MakeCaller = Callable[[str], Caller]
AddMember = Callable[..., None]


def _url(project_id: Any) -> str:
    return f"{PROJECTS}/{project_id}/ownership-transfer"


def _roles(project_id: Any) -> dict[uuid.UUID, str]:
    with get_session_factory()() as session:
        rows = session.execute(
            select(ProjectMember.user_id, ProjectMember.role).where(ProjectMember.project_id == uuid.UUID(str(project_id)))
        ).tuples()
        return dict(rows.all())


def _owners(project_id: Any) -> list[uuid.UUID]:
    return [user_id for user_id, role in _roles(project_id).items() if role == "owner"]


@pytest.fixture
def team(client: TestClient, make_caller: MakeCaller, add_member: AddMember) -> dict[str, Any]:
    names = ("owner", "admin", "member", "outsider", "stranger")
    callers = {name: make_caller(f"{name}@example.com") for name in names}
    project = client.post(PROJECTS, json={"name": "Team"}, headers=callers["owner"].headers).json()["id"]
    add_member(project, callers["admin"].user, ProjectRole.ADMIN)
    add_member(project, callers["member"].user, ProjectRole.MEMBER)
    other = client.post(PROJECTS, json={"name": "Other"}, headers=callers["outsider"].headers).json()["id"]
    return {**callers, "project": project, "other": other}


def _id(team: dict[str, Any], name: str) -> uuid.UUID:
    return team[name].user.id


def _transfer(client: TestClient, team: dict[str, Any], actor: str, target: uuid.UUID, project: str = "project"):
    return client.post(_url(team[project]), json={"user_id": str(target)}, headers=team[actor].headers)


# --- Success ------------------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["admin", "member"])
def test_owner_transfers_ownership(client: TestClient, team: dict[str, Any], target: str) -> None:
    response = _transfer(client, team, "owner", _id(team, target))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"new_owner", "previous_owner"}
    assert (body["new_owner"]["user_id"], body["new_owner"]["role"], body["new_owner"]["email"]) == (
        str(_id(team, target)),
        "owner",
        f"{target}@example.com",
    )
    assert (body["previous_owner"]["user_id"], body["previous_owner"]["role"]) == (str(_id(team, "owner")), "admin")
    roles = _roles(team["project"])
    assert roles[_id(team, target)] == "owner"
    assert roles[_id(team, "owner")] == "admin"
    assert _owners(team["project"]) == [_id(team, target)]
    assert len(roles) == 3  # no membership created or deleted


def test_the_new_owner_has_owner_rights_and_the_old_one_has_admin_rights(client: TestClient, team: dict[str, Any]) -> None:
    _transfer(client, team, "owner", _id(team, "admin"))

    # The previous Owner (now Admin) can no longer transfer back or delete.
    assert _transfer(client, team, "owner", _id(team, "owner")).status_code == 403
    assert client.request(
        "DELETE", f"{PROJECTS}/{team['project']}", json={"confirm_name": "Team"}, headers=team["owner"].headers
    ).status_code == 403
    # ...but can still manage the project as an Admin.
    assert client.patch(f"{PROJECTS}/{team['project']}", json={"name": "Renamed"}, headers=team["owner"].headers).status_code == 200
    # The new Owner can transfer it back.
    assert _transfer(client, team, "admin", _id(team, "owner")).status_code == 200
    assert _owners(team["project"]) == [_id(team, "owner")]
    # ...and the previous Owner may now leave (it was the Owner that could not).
    assert client.delete(f"{PROJECTS}/{team['project']}/members/{_id(team, 'admin')}", headers=team["admin"].headers).status_code == 204


def test_membership_timestamps_are_kept_and_updated(client: TestClient, team: dict[str, Any]) -> None:
    with get_session_factory()() as session:
        before = {
            m.user_id: (m.created_at, m.updated_at)
            for m in session.scalars(select(ProjectMember).where(ProjectMember.project_id == uuid.UUID(team["project"])))
        }

    _transfer(client, team, "owner", _id(team, "member"))

    with get_session_factory()() as session:
        for user_id in (_id(team, "owner"), _id(team, "member")):
            member = session.get_one(ProjectMember, (uuid.UUID(team["project"]), user_id))
            assert member.created_at == before[user_id][0]  # same row, not recreated


# --- Rejections ---------------------------------------------------------------------------------


def test_self_transfer_is_rejected(client: TestClient, team: dict[str, Any]) -> None:
    response = _transfer(client, team, "owner", _id(team, "owner"))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ownership_transfer_to_self"
    assert _owners(team["project"]) == [_id(team, "owner")]


@pytest.mark.parametrize("target", ["stranger", "outsider", "unknown"])
def test_non_member_target_is_rejected_without_leaking_other_memberships(
    client: TestClient, team: dict[str, Any], target: str
) -> None:
    """The outsider owns another project; the stranger belongs to none; neither is distinguishable."""
    user_id = uuid.uuid4() if target == "unknown" else _id(team, target)

    response = _transfer(client, team, "owner", user_id)

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "member_not_found", "message": "Member not found."}}
    assert _owners(team["project"]) == [_id(team, "owner")]
    assert _owners(team["other"]) == [_id(team, "outsider")]


def test_inactive_target_is_rejected(client: TestClient, team: dict[str, Any]) -> None:
    with get_session_factory()() as session:
        session.get_one(User, _id(team, "admin")).is_active = False
        session.commit()

    response = _transfer(client, team, "owner", _id(team, "admin"))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "user_inactive"
    assert _roles(team["project"])[_id(team, "admin")] == "admin"
    assert _owners(team["project"]) == [_id(team, "owner")]


@pytest.mark.parametrize("actor", ["admin", "member"])
def test_admin_and_member_cannot_transfer(client: TestClient, team: dict[str, Any], actor: str) -> None:
    for target in ("admin", "member", "owner"):
        response = _transfer(client, team, actor, _id(team, target))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "insufficient_project_role"
    assert _owners(team["project"]) == [_id(team, "owner")]


def test_non_member_actor_gets_404(client: TestClient, team: dict[str, Any]) -> None:
    for actor in ("outsider", "stranger"):
        response = _transfer(client, team, actor, _id(team, actor))
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert _owners(team["project"]) == [_id(team, "owner")]


@pytest.mark.parametrize("body", [{}, {"user_id": "not-a-uuid"}, {"user_id": str(uuid.uuid4()), "role": "owner"}])
def test_invalid_bodies(client: TestClient, team: dict[str, Any], body: dict[str, str]) -> None:
    response = client.post(_url(team["project"]), json=body, headers=team["owner"].headers)

    assert response.status_code == 422
    assert _owners(team["project"]) == [_id(team, "owner")]


def test_transfer_requires_authentication(client: TestClient, team: dict[str, Any]) -> None:
    assert client.post(_url(team["project"]), json={"user_id": str(_id(team, "admin"))}).status_code == 401


# --- Cross-project isolation and Origin -----------------------------------------------------------


def test_owner_of_one_project_cannot_transfer_another(client: TestClient, team: dict[str, Any], add_member: AddMember) -> None:
    add_member(team["other"], team["owner"].user, ProjectRole.ADMIN)

    # An Admin there: forbidden. And its members are not reachable through this project's URL.
    assert _transfer(client, team, "owner", _id(team, "owner"), project="other").status_code == 403
    assert _transfer(client, team, "owner", _id(team, "outsider")).status_code == 404
    assert _owners(team["other"]) == [_id(team, "outsider")]


@pytest.mark.parametrize("origin_headers", [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}])
def test_transfer_requires_an_allowed_origin(client: TestClient, team: dict[str, Any], origin_headers: dict[str, str]) -> None:
    client.headers.pop("Origin")
    try:
        response = client.post(
            _url(team["project"]),
            json={"user_id": str(_id(team, "admin"))},
            headers={**team["owner"].headers, **origin_headers},
        )
    finally:
        client.headers["Origin"] = ORIGIN

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert _owners(team["project"]) == [_id(team, "owner")]


def test_transfer_logs_ids_only(client: TestClient, team: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        _transfer(client, team, "owner", _id(team, "admin"))

    [record] = [r for r in caplog.records if r.getMessage() == "Project ownership transferred"]
    assert record.new_owner_user_id == str(_id(team, "admin"))
    assert "admin@example.com" not in caplog.text


# --- Service: atomicity, stale authorization, concurrency ----------------------------------------


def _access(team: dict[str, Any], name: str, role: ProjectRole = ProjectRole.OWNER) -> ProjectAccess:
    return ProjectAccess(project_id=team["project"], user_id=_id(team, name), role=role)


def test_failed_promotion_leaves_both_memberships_unchanged(team: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The demotion has already run when the promotion fails: the rollback must undo it."""
    before = _roles(team["project"])
    original = repository.set_member_role
    calls: list[ProjectRole] = []

    def fail_on_promotion(session, *, project_id, user_id, role):  # type: ignore[no-untyped-def]
        calls.append(role)
        if role == ProjectRole.OWNER:
            raise RuntimeError("injected failure")
        return original(session, project_id=project_id, user_id=user_id, role=role)

    monkeypatch.setattr(repository, "set_member_role", fail_on_promotion)
    with get_session_factory()() as session:
        with pytest.raises(RuntimeError, match="injected failure"):
            service.transfer_ownership(session, access=_access(team, "owner"), user_id=_id(team, "admin"))
        assert not session.in_transaction()

    assert calls == [ProjectRole.ADMIN, ProjectRole.OWNER]  # demote first, then promote
    assert _roles(team["project"]) == before


def test_commit_failure_rolls_back(team: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    before = _roles(team["project"])

    with get_session_factory()() as session:

        def fail() -> None:
            raise RuntimeError("injected failure")

        monkeypatch.setattr(session, "commit", fail)
        with pytest.raises(RuntimeError):
            service.transfer_ownership(session, access=_access(team, "owner"), user_id=_id(team, "admin"))
        monkeypatch.undo()

    assert _roles(team["project"]) == before


def test_promoting_first_would_violate_the_owner_index(team: dict[str, Any]) -> None:
    """Why the order matters: the partial unique index rejects a second Owner immediately."""
    with get_session_factory()() as session, pytest.raises(Exception, match="uq_project_members_owner"):
        repository.set_member_role(
            session, project_id=uuid.UUID(team["project"]), user_id=_id(team, "admin"), role=ProjectRole.OWNER
        )


def test_stale_owner_is_refused(team: dict[str, Any]) -> None:
    """The dependency saw Owner; ownership has since moved: the transfer is refused."""
    stale = _access(team, "owner")
    with get_session_factory()() as session:
        service.transfer_ownership(session, access=stale, user_id=_id(team, "admin"))

    with get_session_factory()() as session, pytest.raises(InsufficientProjectRoleError):
        service.transfer_ownership(session, access=stale, user_id=_id(team, "member"))

    assert _owners(team["project"]) == [_id(team, "admin")]


def test_removed_owner_is_refused(team: dict[str, Any]) -> None:
    stale = _access(team, "owner")
    with get_session_factory()() as session:
        session.delete(session.get_one(ProjectMember, (uuid.UUID(team["project"]), _id(team, "owner"))))
        session.commit()

    with get_session_factory()() as session, pytest.raises(ProjectNotFoundError):
        service.transfer_ownership(session, access=stale, user_id=_id(team, "admin"))


def test_self_transfer_is_rejected_by_the_service(team: dict[str, Any]) -> None:
    with get_session_factory()() as session, pytest.raises(OwnershipTransferToSelfError):
        service.transfer_ownership(session, access=_access(team, "owner"), user_id=_id(team, "owner"))


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


def test_concurrent_transfers_by_the_owner(team: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Two transfers to different members, both authorized as Owner before either took the lock:
    exactly one succeeds; the other sees the caller is now only an Admin."""
    barrier = threading.Barrier(2, timeout=10)
    original = repository.lock_project

    def lock_after_both_arrived(session, project_id):  # type: ignore[no-untyped-def]
        barrier.wait()
        return original(session, project_id)

    monkeypatch.setattr(repository, "lock_project", lock_after_both_arrived)

    def transfer_to(name: str) -> Callable[[], object]:
        def call() -> object:
            with get_session_factory()() as session:
                return service.transfer_ownership(session, access=_access(team, "owner"), user_id=_id(team, name))

        return call

    started = [_in_thread(transfer_to("admin")), _in_thread(transfer_to("member"))]
    for thread, _ in started:
        thread.join(timeout=30)
    results = [outcome[0] for _, outcome in started]

    assert sorted(type(r).__name__ for r in results) == ["InsufficientProjectRoleError", "OwnershipTransferResult"]
    [success] = [r for r in results if isinstance(r, OwnershipTransferResult)]
    assert _owners(team["project"]) == [success.new_owner.user_id]
    assert _roles(team["project"])[_id(team, "owner")] == "admin"


def test_transfer_waits_for_a_concurrent_removal_of_the_target(team: dict[str, Any]) -> None:
    """While an Admin removes the intended target under the lock, the transfer waits, then finds
    no such member and changes nothing."""
    holder = get_session_factory()()
    try:
        assert repository.lock_project(holder, uuid.UUID(team["project"])) is not None
        repository.delete_member(holder, project_id=uuid.UUID(team["project"]), user_id=_id(team, "member"))

        def transfer() -> object:
            with get_session_factory()() as session:
                return service.transfer_ownership(session, access=_access(team, "owner"), user_id=_id(team, "member"))

        thread, outcome = _in_thread(transfer)
        time.sleep(0.5)
        assert thread.is_alive(), "the transfer must wait for the project lock"
        holder.commit()
    finally:
        holder.close()
    thread.join(timeout=10)

    assert type(outcome[0]).__name__ == "MemberNotFoundError"
    assert _owners(team["project"]) == [_id(team, "owner")]
