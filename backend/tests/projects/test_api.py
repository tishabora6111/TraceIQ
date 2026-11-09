import logging
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.core.database import get_session_factory
from app.core.security import generate_opaque_token, hash_opaque_token
from app.modules.projects.models import ApiKey, Project, ProjectMember, ProjectRole
from tests.projects.conftest import ORIGIN, Caller

pytestmark = pytest.mark.db

URL = "/api/v1/projects"
NOT_FOUND = {"error": {"code": "project_not_found", "message": "Project not found."}}
FORBIDDEN = {
    "error": {"code": "insufficient_project_role", "message": "Your project role does not allow this action."}
}
ORIGIN_REJECTED = {"error": {"code": "origin_not_allowed", "message": "Request origin is not allowed."}}

MakeCaller = Callable[[str], Caller]
AddMember = Callable[..., None]


def _create(client: TestClient, caller: Caller, name: str = "Search assistant", **fields: Any) -> dict[str, Any]:
    response = client.post(URL, json={"name": name, **fields}, headers=caller.headers)
    assert response.status_code == 201, response.text
    return response.json()


def _delete(client: TestClient, url: str, confirm_name: str, headers: dict[str, str]):
    return client.request("DELETE", url, json={"confirm_name": confirm_name}, headers=headers)


def _count(model: type) -> int:
    with get_session_factory()() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def _stored(project_id: str) -> Project | None:
    with get_session_factory()() as session:
        return session.get(Project, uuid.UUID(project_id))


@pytest.fixture
def team(client: TestClient, make_caller: MakeCaller, add_member: AddMember) -> dict[str, Any]:
    """One project with an Owner, an Admin and a Member, plus an outsider with their own project."""
    owner, admin, member, outsider = (make_caller(f"{n}@example.com") for n in ("owner", "admin", "member", "outsider"))
    project = _create(client, owner, "Team project", description="Shared")
    add_member(project["id"], admin.user, ProjectRole.ADMIN)
    add_member(project["id"], member.user, ProjectRole.MEMBER)
    other = _create(client, outsider, "Outsider project")
    return {"owner": owner, "admin": admin, "member": member, "outsider": outsider, "project": project, "other": other}


# --- Create -----------------------------------------------------------------------------------


def test_create_project_makes_the_caller_its_owner(client: TestClient, make_caller: MakeCaller) -> None:
    caller = make_caller("owner@example.com")

    body = _create(client, caller, "Search assistant", description="RAG over docs")

    assert set(body) == {"id", "name", "description", "role", "created_at", "updated_at"}
    assert (body["name"], body["description"], body["role"]) == ("Search assistant", "RAG over docs", "owner")
    with get_session_factory()() as session:
        [member] = session.scalars(select(ProjectMember)).all()
        assert (str(member.project_id), member.user_id, member.role) == (body["id"], caller.user.id, "owner")


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("  Search  ", "Search"),
        ("\tTabbed\n", "Tabbed"),
        ("\r\nWindows\r\n", "Windows"),
        (" Non-breaking ", "Non-breaking"),
        (" Em space　", "Em space"),
        ("Inner   spaces", "Inner   spaces"),
    ],
)
def test_project_names_are_normalized(client: TestClient, make_caller: MakeCaller, raw: str, stored: str) -> None:
    body = _create(client, make_caller("owner@example.com"), raw)

    assert body["name"] == stored
    assert _stored(body["id"]).name == stored  # type: ignore[union-attr]


@pytest.mark.parametrize("name", ["", " ", "\t\n", " ", "  　", "\r\n"])
def test_blank_project_names_are_rejected(client: TestClient, make_caller: MakeCaller, name: str) -> None:
    caller = make_caller("owner@example.com")

    response = client.post(URL, json={"name": name}, headers=caller.headers)

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "name"]
    assert _count(Project) == 0


def test_name_length_is_checked_after_normalization(client: TestClient, make_caller: MakeCaller) -> None:
    caller = make_caller("owner@example.com")

    assert _create(client, caller, "  " + "x" * 100 + " ")["name"] == "x" * 100
    response = client.post(URL, json={"name": "x" * 101}, headers=caller.headers)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "name"]


def test_description_rules(client: TestClient, make_caller: MakeCaller) -> None:
    caller = make_caller("owner@example.com")

    assert _create(client, caller, "a", description="  trimmed  ")["description"] == "trimmed"
    assert _create(client, caller, "b", description="   ")["description"] is None
    assert _create(client, caller, "c", description="d" * 1000)["description"] == "d" * 1000
    assert _create(client, caller, "d")["description"] is None
    response = client.post(URL, json={"name": "e", "description": "d" * 1001}, headers=caller.headers)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body", [{}, {"description": "no name"}, {"name": 5}, {"name": "ok", "owner_id": str(uuid.uuid4())}]
)
def test_create_rejects_invalid_bodies(client: TestClient, make_caller: MakeCaller, body: dict[str, Any]) -> None:
    caller = make_caller("owner@example.com")

    assert client.post(URL, json=body, headers=caller.headers).status_code == 422
    assert _count(Project) == 0


def test_create_requires_authentication(client: TestClient) -> None:
    for headers in ({}, {"Authorization": "Bearer not-a-token"}):
        response = client.post(URL, json={"name": "x"}, headers=headers)
        assert response.status_code == 401
    assert _count(Project) == 0


# --- List -------------------------------------------------------------------------------------


def test_list_returns_only_the_callers_projects_with_their_roles(client: TestClient, team: dict[str, Any]) -> None:
    for role in ("owner", "admin", "member"):
        response = client.get(URL, headers=team[role].headers)
        assert response.status_code == 200
        assert response.json() == [
            {
                "id": team["project"]["id"],
                "name": "Team project",
                "role": role,
                "created_at": team["project"]["created_at"],
            }
        ]
    assert [p["id"] for p in client.get(URL, headers=team["outsider"].headers).json()] == [team["other"]["id"]]


def test_list_is_empty_for_a_user_without_projects(client: TestClient, make_caller: MakeCaller) -> None:
    assert client.get(URL, headers=make_caller("new@example.com").headers).json() == []


def test_list_pagination(client: TestClient, make_caller: MakeCaller) -> None:
    caller = make_caller("owner@example.com")
    names = [f"Project {i:02d}" for i in range(7)]
    for name in reversed(names):
        _create(client, caller, name)

    def page(**params: int) -> list[str]:
        response = client.get(URL, params=params, headers=caller.headers)
        assert response.status_code == 200
        return [p["name"] for p in response.json()]

    assert page() == names  # ordered by name; default limit 50
    assert page(limit=3) == names[:3]
    assert page(limit=3, offset=3) == names[3:6]
    assert page(limit=3, offset=6) == names[6:]
    assert page(offset=7) == []
    assert page(limit=100) == names


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "x"}])
def test_list_rejects_invalid_pagination(client: TestClient, make_caller: MakeCaller, params: dict[str, Any]) -> None:
    assert client.get(URL, params=params, headers=make_caller("owner@example.com").headers).status_code == 422


def test_list_requires_authentication(client: TestClient) -> None:
    assert client.get(URL).status_code == 401


# --- Detail -----------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["owner", "admin", "member"])
def test_every_member_sees_the_project_with_their_own_role(client: TestClient, team: dict[str, Any], role: str) -> None:
    response = client.get(f"{URL}/{team['project']['id']}", headers=team[role].headers)

    assert response.status_code == 200
    assert response.json() == {**team["project"], "role": role}


def test_non_members_and_unknown_projects_get_the_same_404(client: TestClient, team: dict[str, Any]) -> None:
    """A non-member cannot tell an existing project from a missing one."""
    existing = client.get(f"{URL}/{team['project']['id']}", headers=team["outsider"].headers)
    missing = client.get(f"{URL}/{uuid.uuid4()}", headers=team["outsider"].headers)

    assert existing.status_code == missing.status_code == 404
    assert existing.json() == missing.json() == NOT_FOUND


def test_invalid_project_id_is_a_validation_error(client: TestClient, team: dict[str, Any]) -> None:
    assert client.get(f"{URL}/not-a-uuid", headers=team["owner"].headers).status_code == 422


def test_detail_requires_authentication(client: TestClient, team: dict[str, Any]) -> None:
    assert client.get(f"{URL}/{team['project']['id']}").status_code == 401


# --- Update -----------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_can_update(client: TestClient, team: dict[str, Any], role: str) -> None:
    project_id = team["project"]["id"]

    response = client.patch(
        f"{URL}/{project_id}", json={"name": "  Renamed ", "description": "New"}, headers=team[role].headers
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["description"], body["role"]) == ("Renamed", "New", role)
    assert body["updated_at"] >= team["project"]["updated_at"]
    assert _stored(project_id).name == "Renamed"  # type: ignore[union-attr]


def test_partial_update_changes_only_given_fields(client: TestClient, team: dict[str, Any]) -> None:
    project_id = team["project"]["id"]
    headers = team["owner"].headers

    assert client.patch(f"{URL}/{project_id}", json={"name": "Only name"}, headers=headers).json()["description"] == (
        "Shared"
    )
    cleared = client.patch(f"{URL}/{project_id}", json={"description": None}, headers=headers).json()
    assert (cleared["name"], cleared["description"]) == ("Only name", None)


@pytest.mark.parametrize(
    "body", [{}, {"name": None}, {"name": "  "}, {"name": "x" * 101}, {"retention_overrides": {}}]
)
def test_update_rejects_invalid_bodies(client: TestClient, team: dict[str, Any], body: dict[str, Any]) -> None:
    project_id = team["project"]["id"]

    assert client.patch(f"{URL}/{project_id}", json=body, headers=team["owner"].headers).status_code == 422
    assert _stored(project_id).name == "Team project"  # type: ignore[union-attr]


def test_member_cannot_update(client: TestClient, team: dict[str, Any]) -> None:
    project_id = team["project"]["id"]

    response = client.patch(f"{URL}/{project_id}", json={"name": "Hijacked"}, headers=team["member"].headers)

    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert _stored(project_id).name == "Team project"  # type: ignore[union-attr]


def test_non_member_cannot_update(client: TestClient, team: dict[str, Any]) -> None:
    project_id = team["project"]["id"]

    response = client.patch(f"{URL}/{project_id}", json={"name": "Hijacked"}, headers=team["outsider"].headers)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert _stored(project_id).name == "Team project"  # type: ignore[union-attr]


# --- Delete -----------------------------------------------------------------------------------


def _add_api_key(project_id: str) -> None:
    with get_session_factory()() as session:
        session.add(
            ApiKey(
                project_id=uuid.UUID(project_id),
                name="Ingestion",
                prefix="tiq_test",
                key_hash=hash_opaque_token(generate_opaque_token()),
            )
        )
        session.commit()


def test_owner_deletes_the_project_with_exact_confirmation(client: TestClient, team: dict[str, Any]) -> None:
    project_id = team["project"]["id"]
    _add_api_key(project_id)
    _add_api_key(team["other"]["id"])

    response = _delete(client, f"{URL}/{project_id}", "Team project", team["owner"].headers)

    assert response.status_code == 204
    assert _stored(project_id) is None
    with get_session_factory()() as session:
        remaining_members = session.scalars(select(ProjectMember.project_id)).all()
        remaining_keys = session.scalars(select(ApiKey.project_id)).all()
    assert {str(p) for p in remaining_members} == {team["other"]["id"]}
    assert {str(p) for p in remaining_keys} == {team["other"]["id"]}
    # Nobody can reach it any more; users are unaffected.
    assert client.get(f"{URL}/{project_id}", headers=team["owner"].headers).status_code == 404
    assert client.get(URL, headers=team["admin"].headers).json() == []


def test_confirmation_tolerates_surrounding_whitespace(client: TestClient, team: dict[str, Any]) -> None:
    response = _delete(client, f"{URL}/{team['project']['id']}", "  Team project ", team["owner"].headers)

    assert response.status_code == 204


@pytest.mark.parametrize("confirm", ["team project", "Team", "Team project!", "Outsider project"])
def test_wrong_confirmation_deletes_nothing(client: TestClient, team: dict[str, Any], confirm: str) -> None:
    project_id = team["project"]["id"]

    response = _delete(client, f"{URL}/{project_id}", confirm, team["owner"].headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "confirmation_mismatch"
    assert _stored(project_id) is not None
    assert _count(ProjectMember) == 4


def test_missing_confirmation_is_a_validation_error(client: TestClient, team: dict[str, Any]) -> None:
    project_id = team["project"]["id"]

    for body in ({}, {"confirm_name": ""}, {"confirm": "Team project"}):
        response = client.request("DELETE", f"{URL}/{project_id}", json=body, headers=team["owner"].headers)
        assert response.status_code == 422
    assert client.request("DELETE", f"{URL}/{project_id}", headers=team["owner"].headers).status_code == 422
    assert _stored(project_id) is not None


@pytest.mark.parametrize("role", ["admin", "member"])
def test_only_the_owner_can_delete(client: TestClient, team: dict[str, Any], role: str) -> None:
    project_id = team["project"]["id"]

    response = _delete(client, f"{URL}/{project_id}", "Team project", team[role].headers)

    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert _stored(project_id) is not None


def test_non_member_cannot_delete(client: TestClient, team: dict[str, Any]) -> None:
    project_id = team["project"]["id"]

    response = _delete(client, f"{URL}/{project_id}", "Team project", team["outsider"].headers)

    assert response.status_code == 404
    assert _stored(project_id) is not None


# --- Cross-project isolation ------------------------------------------------------------------


def test_roles_are_per_project(client: TestClient, team: dict[str, Any], add_member: AddMember) -> None:
    """Admin of one project is only a Member of another: no rights carry over."""
    add_member(team["other"]["id"], team["admin"].user, ProjectRole.MEMBER)

    assert client.patch(
        f"{URL}/{team['project']['id']}", json={"name": "Fine"}, headers=team["admin"].headers
    ).status_code == 200
    assert client.patch(
        f"{URL}/{team['other']['id']}", json={"name": "Nope"}, headers=team["admin"].headers
    ).status_code == 403


def test_owner_of_one_project_cannot_touch_another(client: TestClient, team: dict[str, Any]) -> None:
    other_id = team["other"]["id"]
    headers = team["owner"].headers

    assert client.get(f"{URL}/{other_id}", headers=headers).status_code == 404
    assert client.patch(f"{URL}/{other_id}", json={"name": "x"}, headers=headers).status_code == 404
    assert _delete(client, f"{URL}/{other_id}", "Outsider project", headers).status_code == 404
    assert _stored(other_id).name == "Outsider project"  # type: ignore[union-attr]
    assert {p["id"] for p in client.get(URL, headers=headers).json()} == {team["project"]["id"]}


# --- Origin on every write endpoint -----------------------------------------------------------


def _writes(team: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    project_id = team["project"]["id"]
    return [
        ("POST", URL, {"json": {"name": "Cross-site"}}),
        ("PATCH", f"{URL}/{project_id}", {"json": {"name": "Cross-site"}}),
        ("DELETE", f"{URL}/{project_id}", {"json": {"confirm_name": "Team project"}}),
    ]


@pytest.mark.parametrize("origin_headers", [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}])
def test_every_write_endpoint_requires_an_allowed_origin(
    client: TestClient, team: dict[str, Any], origin_headers: dict[str, str]
) -> None:
    client.headers.pop("Origin")
    try:
        for method, url, kwargs in _writes(team):
            response = client.request(method, url, headers={**team["owner"].headers, **origin_headers}, **kwargs)
            assert response.status_code == 403, (method, url)
            assert response.json() == ORIGIN_REJECTED
    finally:
        client.headers["Origin"] = ORIGIN

    assert _count(Project) == 2
    assert _stored(team["project"]["id"]).name == "Team project"  # type: ignore[union-attr]


def test_reads_do_not_require_an_origin(client: TestClient, team: dict[str, Any]) -> None:
    client.headers.pop("Origin")
    try:
        assert client.get(URL, headers=team["owner"].headers).status_code == 200
        assert client.get(f"{URL}/{team['project']['id']}", headers=team["owner"].headers).status_code == 200
    finally:
        client.headers["Origin"] = ORIGIN


def test_writes_with_a_referer_fallback(client: TestClient, team: dict[str, Any]) -> None:
    client.headers.pop("Origin")
    try:
        response = client.patch(
            f"{URL}/{team['project']['id']}",
            json={"name": "Via referer"},
            headers={**team["owner"].headers, "Referer": f"{ORIGIN}/projects"},
        )
    finally:
        client.headers["Origin"] = ORIGIN

    assert response.status_code == 200


def test_project_names_are_not_logged(client: TestClient, make_caller: MakeCaller, caplog: pytest.LogCaptureFixture) -> None:
    caller = make_caller("owner@example.com")

    with caplog.at_level(logging.INFO):
        project = _create(client, caller, "Confidential codename")
        client.patch(f"{URL}/{project['id']}", json={"name": "Renamed codename"}, headers=caller.headers)
        _delete(client, f"{URL}/{project['id']}", "Renamed codename", caller.headers)

    messages = [r.getMessage() for r in caplog.records]
    assert {"Project created", "Project updated", "Project deleted"} <= set(messages)
    assert "codename" not in caplog.text


def test_deleted_users_projects_query_uses_no_raw_join_leaks(client: TestClient, team: dict[str, Any]) -> None:
    """A membership row pointing at a project is the only path to it: removing the row removes access."""
    with get_session_factory()() as session:
        session.execute(
            text("DELETE FROM project_members WHERE user_id = :u"), {"u": team["member"].user.id}
        )
        session.commit()

    assert client.get(f"{URL}/{team['project']['id']}", headers=team["member"].headers).status_code == 404
    assert client.get(URL, headers=team["member"].headers).json() == []
