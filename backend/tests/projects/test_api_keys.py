import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.database import get_session_factory
from app.core.security import generate_api_key
from app.modules.projects import repository, service
from app.modules.projects.errors import ApiKeyLimitError, InsufficientProjectRoleError, ProjectNotFoundError
from app.modules.projects.models import ApiKey, ProjectMember, ProjectRole
from app.modules.projects.schemas import ApiKeyCreated, ApiKeyPrincipal, ProjectAccess
from tests.projects.conftest import ORIGIN, Caller

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

PROJECTS = "/api/v1/projects"
NOT_FOUND = {"error": {"code": "project_not_found", "message": "Project not found."}}
KEY_NOT_FOUND = {"error": {"code": "api_key_not_found", "message": "API key not found."}}
FORBIDDEN_CODE = "insufficient_project_role"
LIMIT = service.MAX_ACTIVE_API_KEYS
READ_FIELDS = {
    "id",
    "name",
    "prefix",
    "is_active",
    "created_by",
    "created_at",
    "last_used_at",
    "revoked_at",
    "revoked_by",
}

MakeCaller = Callable[[str], Caller]
AddMember = Callable[..., None]


@pytest.fixture
def team(client: TestClient, make_caller: MakeCaller, add_member: AddMember) -> dict[str, Any]:
    names = ("owner", "admin", "member", "outsider")
    callers = {name: make_caller(f"{name}@example.com") for name in names}
    project = client.post(PROJECTS, json={"name": "Team"}, headers=callers["owner"].headers).json()["id"]
    add_member(project, callers["admin"].user, ProjectRole.ADMIN)
    add_member(project, callers["member"].user, ProjectRole.MEMBER)
    other = client.post(PROJECTS, json={"name": "Other"}, headers=callers["outsider"].headers).json()["id"]
    return {**callers, "project": project, "other": other}


def _url(project_id: Any) -> str:
    return f"{PROJECTS}/{project_id}/api-keys"


def _revoke_url(project_id: Any, api_key_id: Any) -> str:
    return f"{_url(project_id)}/{api_key_id}/revoke"


def _create(client: TestClient, team: dict[str, Any], name: str = "CI", actor: str = "owner", project: str = "project"):
    return client.post(_url(team[project]), json={"name": name}, headers=team[actor].headers)


def _list(client: TestClient, team: dict[str, Any], actor: str = "owner", project: str = "project"):
    return client.get(_url(team[project]), headers=team[actor].headers)


def _revoke(client: TestClient, team: dict[str, Any], api_key_id: Any, actor: str = "owner", project: str = "project"):
    return client.post(_revoke_url(team[project], api_key_id), headers=team[actor].headers)


def _rows(project_id: Any) -> list[ApiKey]:
    with get_session_factory()() as session:
        return list(session.scalars(select(ApiKey).where(ApiKey.project_id == uuid.UUID(str(project_id)))))


def _active_count(project_id: Any) -> int:
    with get_session_factory()() as session:
        return repository.count_active_api_keys(session, uuid.UUID(str(project_id)))


def _authenticate(key: object) -> ApiKeyPrincipal | None:
    with get_session_factory()() as session:
        return service.authenticate_api_key(session, key)


def _access(team: dict[str, Any], name: str, role: ProjectRole = ProjectRole.ADMIN) -> ProjectAccess:
    return ProjectAccess(project_id=team["project"], user_id=team[name].user.id, role=role)


def _fill(team: dict[str, Any], count: int, project: str = "project") -> None:
    """Create ``count`` keys directly (fast), as the project's first Admin-or-better member."""
    actor = "owner" if project == "project" else "outsider"
    access = ProjectAccess(project_id=team[project], user_id=team[actor].user.id, role=ProjectRole.OWNER)
    for index in range(count):
        with get_session_factory()() as session:
            service.create_api_key(session, access=access, name=f"key {index}")


# --- Creation -----------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["owner", "admin"])
def test_owner_and_admin_create_a_key(client: TestClient, team: dict[str, Any], actor: str) -> None:
    response = _create(client, team, "Production ingestion", actor)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == READ_FIELDS | {"key"}
    key = body["key"]
    assert key.startswith("tiq_") and len(key) == 47
    assert body["prefix"] == key[:12]
    assert body["name"] == "Production ingestion"
    assert body["is_active"] is True
    assert body["created_by"] == str(team[actor].user.id)
    assert (body["last_used_at"], body["revoked_at"], body["revoked_by"]) == (None, None, None)


def test_creation_response_is_never_cached(client: TestClient, team: dict[str, Any]) -> None:
    response = _create(client, team)

    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


def test_only_the_sha256_digest_is_stored(client: TestClient, team: dict[str, Any]) -> None:
    body = _create(client, team).json()

    [row] = _rows(team["project"])
    assert row.id == uuid.UUID(body["id"])
    assert row.key_hash == hashlib.sha256(body["key"].encode()).hexdigest()
    stored = [str(getattr(row, column.key)) for column in ApiKey.__table__.columns]
    assert all(body["key"] not in value for value in stored)
    assert row.prefix == body["key"][:12]


def test_every_key_is_different(client: TestClient, team: dict[str, Any]) -> None:
    keys = {_create(client, team, f"key {index}").json()["key"] for index in range(5)}

    assert len(keys) == 5
    assert len({row.key_hash for row in _rows(team["project"])}) == 5


def test_the_raw_key_is_disclosed_only_once(client: TestClient, team: dict[str, Any]) -> None:
    created = _create(client, team).json()
    key, key_hash = created["key"], _rows(team["project"])[0].key_hash

    later = [
        _list(client, team),
        _list(client, team, "admin"),
        _revoke(client, team, created["id"]),
        _revoke(client, team, created["id"]),
        _list(client, team),
    ]

    for response in later:
        assert response.status_code == 200
        assert key not in response.text
        assert key_hash not in response.text
        assert '"key"' not in response.text
        assert "key_hash" not in response.text


@pytest.mark.parametrize(
    ("name", "stored"),
    [("  CI  ", "CI"), (" CI ", "CI"), ("x" * 100, "x" * 100), ("Clé de prod", "Clé de prod")],
)
def test_names_are_trimmed_and_may_use_the_full_length(
    client: TestClient, team: dict[str, Any], name: str, stored: str
) -> None:
    response = _create(client, team, name)

    assert response.status_code == 201
    assert response.json()["name"] == stored
    assert _rows(team["project"])[0].name == stored


def test_duplicate_names_are_allowed(client: TestClient, team: dict[str, Any]) -> None:
    assert _create(client, team, "CI").status_code == 201
    assert _create(client, team, "CI").status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": " \t\n"},
        {"name": "x" * 101},
        {"name": None},
        {"name": 42},
        {"name": True},
        {"name": ["CI"]},
        {"name": "CI", "key": "tiq_" + "a" * 43},
        {"name": "CI", "key_hash": "0" * 64},
        {"name": "CI", "prefix": "tiq_mine"},
    ],
)
def test_invalid_bodies_are_rejected(client: TestClient, team: dict[str, Any], body: dict[str, Any]) -> None:
    response = client.post(_url(team["project"]), json=body, headers=team["owner"].headers)

    assert response.status_code == 422
    assert _rows(team["project"]) == []


# --- Listing ------------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["owner", "admin"])
def test_owner_and_admin_list_keys_including_revoked_ones(client: TestClient, team: dict[str, Any], actor: str) -> None:
    first = _create(client, team, "first").json()
    second = _create(client, team, "second", "admin").json()
    _revoke(client, team, first["id"])

    response = _list(client, team, actor)

    assert response.status_code == 200
    keys = {item["id"]: item for item in response.json()}
    assert set(keys) == {first["id"], second["id"]}
    assert all(set(item) == READ_FIELDS for item in keys.values())
    assert keys[first["id"]]["is_active"] is False
    assert keys[first["id"]]["revoked_by"] == str(team["owner"].user.id)
    assert keys[second["id"]]["is_active"] is True
    assert keys[second["id"]]["prefix"] == second["key"][:12]


def test_listing_is_paginated(client: TestClient, team: dict[str, Any]) -> None:
    _fill(team, 3)

    page = client.get(_url(team["project"]), params={"limit": 2}, headers=team["owner"].headers).json()
    rest = client.get(_url(team["project"]), params={"limit": 2, "offset": 2}, headers=team["owner"].headers).json()

    assert len(page) == 2 and len(rest) == 1
    assert {item["id"] for item in page}.isdisjoint(item["id"] for item in rest)
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert client.get(_url(team["project"]), params=params, headers=team["owner"].headers).status_code == 422


def test_empty_list(client: TestClient, team: dict[str, Any]) -> None:
    assert _list(client, team).json() == []


# --- Revocation ---------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["owner", "admin"])
def test_owner_and_admin_revoke(client: TestClient, team: dict[str, Any], actor: str) -> None:
    created = _create(client, team).json()
    assert _authenticate(created["key"]) == ApiKeyPrincipal(
        project_id=uuid.UUID(team["project"]), api_key_id=uuid.UUID(created["id"])
    )

    response = _revoke(client, team, created["id"], actor)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == READ_FIELDS
    assert body["is_active"] is False
    assert body["revoked_at"] is not None
    assert body["revoked_by"] == str(team[actor].user.id)
    assert _authenticate(created["key"]) is None
    [row] = _rows(team["project"])
    assert row.revoked_at is not None  # kept for audit, never deleted


def test_revoking_twice_changes_nothing(client: TestClient, team: dict[str, Any]) -> None:
    created = _create(client, team).json()
    first = _revoke(client, team, created["id"]).json()

    second = _revoke(client, team, created["id"], "admin")

    assert second.status_code == 200
    assert second.json() == first  # same revoked_at and revoked_by
    assert _authenticate(created["key"]) is None


def test_revoking_one_key_leaves_the_others_working(client: TestClient, team: dict[str, Any]) -> None:
    revoked, kept = _create(client, team, "a").json(), _create(client, team, "b").json()

    _revoke(client, team, revoked["id"])

    assert _authenticate(revoked["key"]) is None
    assert _authenticate(kept["key"]) is not None


def test_unknown_key_id_gets_404(client: TestClient, team: dict[str, Any]) -> None:
    response = _revoke(client, team, uuid.uuid4())

    assert response.status_code == 404
    assert response.json() == KEY_NOT_FOUND


def test_invalid_key_id_gets_422(client: TestClient, team: dict[str, Any]) -> None:
    assert client.post(_revoke_url(team["project"], "not-a-uuid"), headers=team["owner"].headers).status_code == 422


# --- Authentication -----------------------------------------------------------------------------


def test_a_valid_key_authenticates_its_own_project_only(client: TestClient, team: dict[str, Any]) -> None:
    mine = _create(client, team).json()
    theirs = _create(client, team, actor="outsider", project="other").json()

    assert _authenticate(mine["key"]) == ApiKeyPrincipal(
        project_id=uuid.UUID(team["project"]), api_key_id=uuid.UUID(mine["id"])
    )
    assert _authenticate(theirs["key"]) == ApiKeyPrincipal(
        project_id=uuid.UUID(team["other"]), api_key_id=uuid.UUID(theirs["id"])
    )


@pytest.mark.parametrize("transform", ["unknown", "prefix", "hash", "case", "whitespace", "truncated", "none", "bytes"])
def test_wrong_keys_never_authenticate(client: TestClient, team: dict[str, Any], transform: str) -> None:
    key = _create(client, team).json()["key"]
    key_hash = _rows(team["project"])[0].key_hash
    presented: object = {
        "unknown": generate_api_key(),
        "prefix": key[:12],
        "hash": key_hash,
        "case": key.upper() if key != key.upper() else key.lower(),
        "whitespace": f" {key} ",
        "truncated": key[:-1],
        "none": None,
        "bytes": key.encode(),
    }[transform]

    assert _authenticate(presented) is None


def test_keys_of_a_deleted_project_never_authenticate(client: TestClient, team: dict[str, Any]) -> None:
    key = _create(client, team).json()["key"]

    deleted = client.request(
        "DELETE", f"{PROJECTS}/{team['project']}", json={"confirm_name": "Team"}, headers=team["owner"].headers
    )

    assert deleted.status_code == 204
    assert _authenticate(key) is None


def test_keys_survive_their_creator_leaving(client: TestClient, team: dict[str, Any]) -> None:
    """A key belongs to the project, not to the person who created it."""
    key = _create(client, team, actor="admin").json()["key"]

    left = client.delete(f"{PROJECTS}/{team['project']}/members/{team['admin'].user.id}", headers=team["admin"].headers)

    assert left.status_code == 204
    assert _authenticate(key) is not None


# --- Permissions and isolation ------------------------------------------------------------------


def test_member_gets_403_for_every_key_route(client: TestClient, team: dict[str, Any]) -> None:
    created = _create(client, team).json()

    responses = [_create(client, team, actor="member"), _list(client, team, "member"), _revoke(client, team, created["id"], "member")]

    for response in responses:
        assert response.status_code == 403
        assert response.json()["error"]["code"] == FORBIDDEN_CODE
        assert created["key"] not in response.text
    assert len(_rows(team["project"])) == 1
    assert _authenticate(created["key"]) is not None


def test_non_member_gets_404_for_every_key_route(client: TestClient, team: dict[str, Any]) -> None:
    created = _create(client, team).json()

    responses = [
        _create(client, team, actor="outsider"),
        _list(client, team, "outsider"),
        _revoke(client, team, created["id"], "outsider"),
    ]

    for response in responses:
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
    assert len(_rows(team["project"])) == 1
    assert _authenticate(created["key"]) is not None


def test_key_routes_require_authentication(client: TestClient, team: dict[str, Any]) -> None:
    created = _create(client, team).json()

    assert client.post(_url(team["project"]), json={"name": "CI"}).status_code == 401
    assert client.get(_url(team["project"])).status_code == 401
    assert client.post(_revoke_url(team["project"], created["id"])).status_code == 401
    # An API key is not a user credential: it cannot manage keys.
    as_api_key = {"Authorization": f"Bearer {created['key']}"}
    assert client.get(_url(team["project"]), headers=as_api_key).status_code == 401
    assert _authenticate(created["key"]) is not None


def test_another_projects_key_cannot_be_revoked_through_this_project(client: TestClient, team: dict[str, Any]) -> None:
    theirs = _create(client, team, actor="outsider", project="other").json()

    response = _revoke(client, team, theirs["id"])

    assert response.status_code == 404
    assert response.json() == KEY_NOT_FOUND
    assert _authenticate(theirs["key"]) is not None


def test_listings_are_scoped_to_the_project(client: TestClient, team: dict[str, Any]) -> None:
    mine = _create(client, team).json()
    theirs = _create(client, team, actor="outsider", project="other").json()

    assert [item["id"] for item in _list(client, team).json()] == [mine["id"]]
    assert [item["id"] for item in _list(client, team, "outsider", "other").json()] == [theirs["id"]]


@pytest.mark.parametrize("actor", ["owner", "admin", "member"])
def test_roles_in_one_project_give_nothing_in_another(client: TestClient, team: dict[str, Any], actor: str) -> None:
    theirs = _create(client, team, actor="outsider", project="other").json()

    for response in (
        _create(client, team, actor=actor, project="other"),
        _list(client, team, actor, "other"),
        _revoke(client, team, theirs["id"], actor, "other"),
    ):
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
    assert len(_rows(team["other"])) == 1


@pytest.mark.parametrize("origin_headers", [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}])
def test_writes_require_an_allowed_origin(client: TestClient, team: dict[str, Any], origin_headers: dict[str, str]) -> None:
    created = _create(client, team).json()
    client.headers.pop("Origin")
    try:
        headers = {**team["owner"].headers, **origin_headers}
        responses = [
            client.post(_url(team["project"]), json={"name": "CI"}, headers=headers),
            client.post(_revoke_url(team["project"], created["id"]), headers=headers),
        ]
        listing = client.get(_url(team["project"]), headers=headers)
    finally:
        client.headers["Origin"] = ORIGIN

    for response in responses:
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "origin_not_allowed"
    assert listing.status_code == 200  # reads are not Origin-checked
    assert len(_rows(team["project"])) == 1
    assert _authenticate(created["key"]) is not None


# --- Active-key limit ---------------------------------------------------------------------------


def test_limit_is_25_active_keys(client: TestClient, team: dict[str, Any]) -> None:
    assert LIMIT == 25
    _fill(team, LIMIT)

    response = _create(client, team)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "api_key_limit_reached"
    assert "key" not in response.json()
    assert _active_count(team["project"]) == LIMIT
    assert len(_rows(team["project"])) == LIMIT


def test_revoked_keys_do_not_count(client: TestClient, team: dict[str, Any]) -> None:
    _fill(team, LIMIT)
    victim = _list(client, team).json()[0]["id"]

    _revoke(client, team, victim)
    assert _create(client, team).status_code == 201
    assert _create(client, team).status_code == 409

    assert _active_count(team["project"]) == LIMIT
    assert len(_rows(team["project"])) == LIMIT + 1


def test_the_limit_is_per_project(client: TestClient, team: dict[str, Any]) -> None:
    _fill(team, LIMIT)

    assert _create(client, team, actor="outsider", project="other").status_code == 201


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


def test_concurrent_creation_at_the_limit(team: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """With one slot left, several creations authorized at once (all past the dependency, all
    reaching the lock together): exactly one gets the slot."""
    _fill(team, LIMIT - 1)
    contenders = 4
    barrier = threading.Barrier(contenders, timeout=10)
    original = repository.lock_project

    def lock_after_all_arrived(session, project_id):  # type: ignore[no-untyped-def]
        barrier.wait()
        return original(session, project_id)

    monkeypatch.setattr(repository, "lock_project", lock_after_all_arrived)

    def create(actor: str) -> Callable[[], object]:
        def call() -> object:
            with get_session_factory()() as session:
                return service.create_api_key(session, access=_access(team, actor), name="race")

        return call

    started = [_in_thread(create("owner" if index % 2 else "admin")) for index in range(contenders)]
    for thread, _ in started:
        thread.join(timeout=30)
    results = [outcome[0] for _, outcome in started]

    assert sum(isinstance(r, ApiKeyCreated) for r in results) == 1
    assert sum(isinstance(r, ApiKeyLimitError) for r in results) == contenders - 1
    assert _active_count(team["project"]) == LIMIT


# --- Service: transactions, stale authorization, logging ----------------------------------------


def test_create_commit_failure_rolls_back(team: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    with get_session_factory()() as session:

        def fail() -> None:
            raise RuntimeError("injected failure")

        monkeypatch.setattr(session, "commit", fail)
        with pytest.raises(RuntimeError):
            service.create_api_key(session, access=_access(team, "owner"), name="CI")
        assert not session.in_transaction()
        monkeypatch.undo()

    assert _rows(team["project"]) == []


def test_revoke_commit_failure_rolls_back(client: TestClient, team: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    created = _create(client, team).json()

    with get_session_factory()() as session:

        def fail() -> None:
            raise RuntimeError("injected failure")

        monkeypatch.setattr(session, "commit", fail)
        with pytest.raises(RuntimeError):
            service.revoke_api_key(session, access=_access(team, "owner"), api_key_id=uuid.UUID(created["id"]))
        assert not session.in_transaction()
        monkeypatch.undo()

    [row] = _rows(team["project"])
    assert (row.revoked_at, row.revoked_by) == (None, None)
    assert _authenticate(created["key"]) is not None


def _demote_admin(team: dict[str, Any]) -> None:
    with get_session_factory()() as session:
        session.get_one(ProjectMember, (uuid.UUID(team["project"]), team["admin"].user.id)).role = "member"
        session.commit()


def test_demoted_admin_cannot_create_or_revoke(client: TestClient, team: dict[str, Any]) -> None:
    """The dependency saw Admin; the role has since dropped to Member."""
    created = _create(client, team).json()
    stale = _access(team, "admin")
    _demote_admin(team)

    with get_session_factory()() as session, pytest.raises(InsufficientProjectRoleError):
        service.create_api_key(session, access=stale, name="CI")
    with get_session_factory()() as session, pytest.raises(InsufficientProjectRoleError):
        service.revoke_api_key(session, access=stale, api_key_id=uuid.UUID(created["id"]))

    assert len(_rows(team["project"])) == 1
    assert _authenticate(created["key"]) is not None


def test_removed_admin_cannot_create(team: dict[str, Any]) -> None:
    stale = _access(team, "admin")
    with get_session_factory()() as session:
        session.delete(session.get_one(ProjectMember, (uuid.UUID(team["project"]), team["admin"].user.id)))
        session.commit()

    with get_session_factory()() as session, pytest.raises(ProjectNotFoundError):
        service.create_api_key(session, access=stale, name="CI")
    assert _rows(team["project"]) == []


def test_list_service_refuses_members(team: dict[str, Any]) -> None:
    """Defence in depth behind the route's Admin dependency."""
    with get_session_factory()() as session, pytest.raises(InsufficientProjectRoleError):
        service.list_api_keys(session, access=_access(team, "member", ProjectRole.MEMBER), limit=10, offset=0)


def test_creation_waits_for_a_concurrent_demotion(team: dict[str, Any]) -> None:
    holder = get_session_factory()()
    try:
        assert repository.lock_project(holder, uuid.UUID(team["project"])) is not None
        repository.set_member_role(
            holder, project_id=uuid.UUID(team["project"]), user_id=team["admin"].user.id, role=ProjectRole.MEMBER
        )

        def create() -> object:
            with get_session_factory()() as session:
                return service.create_api_key(session, access=_access(team, "admin"), name="CI")

        thread, outcome = _in_thread(create)
        time.sleep(0.5)
        assert thread.is_alive(), "the creation must wait for the project lock"
        holder.commit()
    finally:
        holder.close()
    thread.join(timeout=10)

    assert isinstance(outcome[0], InsufficientProjectRoleError)
    assert _rows(team["project"]) == []


def test_logs_carry_ids_only(client: TestClient, team: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        created = _create(client, team, "Secret name").json()
        _revoke(client, team, created["id"])
        _authenticate(created["key"])

    key_hash = _rows(team["project"])[0].key_hash
    [created_log] = [r for r in caplog.records if r.getMessage() == "API key created"]
    [revoked_log] = [r for r in caplog.records if r.getMessage() == "API key revoked"]
    assert created_log.api_key_id == revoked_log.api_key_id == created["id"]
    for secret in (created["key"], key_hash, created["key"][:12], "Secret name", team["owner"].headers["Authorization"]):
        assert secret not in caplog.text


def test_database_counts_match_the_service(team: dict[str, Any]) -> None:
    _fill(team, 3)
    with get_session_factory()() as session:
        total = session.scalar(select(func.count()).select_from(ApiKey))

    assert total == 3 == _active_count(team["project"])
