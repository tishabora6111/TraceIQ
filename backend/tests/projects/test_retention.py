import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import MAX_RETENTION_DAYS, MIN_RETENTION_DAYS, Settings
from app.core.database import get_session_factory
from app.modules.projects import repository, service
from app.modules.projects.errors import InsufficientProjectRoleError, ProjectNotFoundError
from app.modules.projects.models import Project, ProjectMember, ProjectRole
from app.modules.projects.schemas import RETENTION_KEYS, ProjectAccess, RetentionRead
from tests.projects.conftest import ORIGIN, Caller

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

PROJECTS = "/api/v1/projects"
NOT_FOUND = {"error": {"code": "project_not_found", "message": "Project not found."}}
FORBIDDEN_CODE = "insufficient_project_role"

# The documented defaults (D7, ARCHITECTURE §5.7).
DEFAULTS = {
    "traces_days": 30,
    "evaluation_results_days": 90,
    "prediction_logs_days": 30,
    "incidents_days_after_resolution": 180,
    "investigation_reports_days_after_update": 180,
}
NO_OVERRIDES = dict.fromkeys(DEFAULTS)

MakeCaller = Callable[[str], Caller]
AddMember = Callable[..., None]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The code defaults, whatever TRACEIQ_RETENTION_* the environment running the tests sets."""
    return settings.model_copy(
        update={f"retention_{key}": Settings.model_fields[f"retention_{key}"].default for key in RETENTION_KEYS}
    )


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
    return f"{PROJECTS}/{project_id}/retention"


def _get(client: TestClient, team: dict[str, Any], actor: str = "owner", project: str = "project"):
    return client.get(_url(team[project]), headers=team[actor].headers)


def _patch(client: TestClient, team: dict[str, Any], body: Any, actor: str = "owner", project: str = "project"):
    return client.patch(_url(team[project]), json=body, headers=team[actor].headers)


def _stored(project_id: Any) -> Any:
    """The raw column: None for SQL NULL, otherwise the JSON object."""
    with get_session_factory()() as session:
        return session.get_one(Project, uuid.UUID(str(project_id))).retention_overrides


def _store_raw(project_id: Any, raw_json: str) -> None:
    with get_session_factory()() as session:
        session.execute(
            text("UPDATE projects SET retention_overrides = CAST(:value AS jsonb) WHERE id = :id"),
            {"value": raw_json, "id": uuid.UUID(str(project_id))},
        )
        session.commit()


def _access(team: dict[str, Any], name: str, role: ProjectRole = ProjectRole.ADMIN) -> ProjectAccess:
    return ProjectAccess(project_id=team["project"], user_id=team[name].user.id, role=role)


# --- Reading ------------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["owner", "admin", "member"])
def test_every_member_reads_the_defaults(client: TestClient, team: dict[str, Any], actor: str) -> None:
    response = _get(client, team, actor)

    assert response.status_code == 200
    assert response.json() == {
        "project_id": team["project"],
        "effective": DEFAULTS,
        "overrides": NO_OVERRIDES,
        "defaults": DEFAULTS,
        "model_artifacts": "until_manually_deleted",
    }
    assert _stored(team["project"]) is None


def test_defaults_come_from_configuration(team: dict[str, Any], settings: Settings) -> None:
    configured = settings.model_copy(update={"retention_traces_days": 7, "retention_prediction_logs_days": 14})
    with get_session_factory()() as session:
        result = service.get_retention(session, access=_access(team, "member", ProjectRole.MEMBER), settings=configured)

    assert result.defaults.traces_days == result.effective.traces_days == 7
    assert result.defaults.prediction_logs_days == result.effective.prediction_logs_days == 14
    assert result.effective.evaluation_results_days == 90


def test_overrides_survive_a_change_of_defaults(team: dict[str, Any], settings: Settings) -> None:
    with get_session_factory()() as session:
        service.update_retention(session, access=_access(team, "owner"), changes={"traces_days": 10}, settings=settings)
    configured = settings.model_copy(update={"retention_traces_days": 60, "retention_evaluation_results_days": 120})
    with get_session_factory()() as session:
        result = service.get_retention(session, access=_access(team, "member"), settings=configured)

    assert result.effective.traces_days == 10
    assert result.effective.evaluation_results_days == 120


def test_non_member_gets_404(client: TestClient, team: dict[str, Any]) -> None:
    for response in (_get(client, team, "outsider"), _patch(client, team, {"traces_days": 5}, "outsider")):
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
    assert _stored(team["project"]) is None


def test_unknown_project_gets_404(client: TestClient, team: dict[str, Any]) -> None:
    response = client.get(_url(uuid.uuid4()), headers=team["owner"].headers)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_retention_requires_authentication(client: TestClient, team: dict[str, Any]) -> None:
    assert client.get(_url(team["project"])).status_code == 401
    assert client.patch(_url(team["project"]), json={"traces_days": 5}).status_code == 401
    assert _stored(team["project"]) is None


# --- Updating -----------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["owner", "admin"])
def test_owner_and_admin_update(client: TestClient, team: dict[str, Any], actor: str) -> None:
    response = _patch(client, team, {"traces_days": 7}, actor)

    assert response.status_code == 200
    body = response.json()
    assert body["effective"] == {**DEFAULTS, "traces_days": 7}
    assert body["overrides"] == {**NO_OVERRIDES, "traces_days": 7}
    assert body["defaults"] == DEFAULTS
    assert _stored(team["project"]) == {"traces_days": 7}
    assert _get(client, team, "member").json() == body


def test_member_cannot_update(client: TestClient, team: dict[str, Any]) -> None:
    response = _patch(client, team, {"traces_days": 7}, "member")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == FORBIDDEN_CODE
    assert _stored(team["project"]) is None


def test_multiple_overrides_in_one_request(client: TestClient, team: dict[str, Any]) -> None:
    body = {key: 100 + index for index, key in enumerate(RETENTION_KEYS)}

    response = _patch(client, team, body)

    assert response.json()["effective"] == body
    assert response.json()["overrides"] == body
    assert _stored(team["project"]) == body


def test_partial_updates_keep_other_overrides(client: TestClient, team: dict[str, Any]) -> None:
    _patch(client, team, {"traces_days": 7, "prediction_logs_days": 8})

    response = _patch(client, team, {"evaluation_results_days": 9, "traces_days": 10})

    expected = {"traces_days": 10, "prediction_logs_days": 8, "evaluation_results_days": 9}
    assert response.json()["overrides"] == {**NO_OVERRIDES, **expected}
    assert response.json()["effective"] == {**DEFAULTS, **expected}
    assert _stored(team["project"]) == expected


def test_null_resets_one_setting(client: TestClient, team: dict[str, Any]) -> None:
    _patch(client, team, {"traces_days": 7, "prediction_logs_days": 8})

    response = _patch(client, team, {"traces_days": None})

    assert response.json()["effective"] == {**DEFAULTS, "prediction_logs_days": 8}
    assert response.json()["overrides"] == {**NO_OVERRIDES, "prediction_logs_days": 8}
    assert _stored(team["project"]) == {"prediction_logs_days": 8}


def test_null_for_a_setting_without_override_is_a_no_op(client: TestClient, team: dict[str, Any]) -> None:
    response = _patch(client, team, {"incidents_days_after_resolution": None})

    assert response.status_code == 200
    assert response.json()["effective"] == DEFAULTS
    assert _stored(team["project"]) is None


def test_all_null_resets_everything_to_sql_null(client: TestClient, team: dict[str, Any]) -> None:
    _patch(client, team, {key: 50 for key in RETENTION_KEYS})

    response = _patch(client, team, NO_OVERRIDES)

    assert response.json()["effective"] == DEFAULTS
    assert response.json()["overrides"] == NO_OVERRIDES
    assert _stored(team["project"]) is None
    with get_session_factory()() as session:
        is_null = session.scalar(
            text("SELECT retention_overrides IS NULL FROM projects WHERE id = :id"), {"id": uuid.UUID(team["project"])}
        )
    assert is_null is True


def test_stored_empty_object_means_defaults(client: TestClient, team: dict[str, Any]) -> None:
    _store_raw(team["project"], "{}")

    assert _get(client, team).json()["effective"] == DEFAULTS
    response = _patch(client, team, {"traces_days": None})
    assert response.json()["effective"] == DEFAULTS
    assert _stored(team["project"]) is None


@pytest.mark.parametrize("days", [MIN_RETENTION_DAYS, MAX_RETENTION_DAYS])
@pytest.mark.parametrize("key", RETENTION_KEYS)
def test_bounds_are_accepted(client: TestClient, team: dict[str, Any], key: str, days: int) -> None:
    response = _patch(client, team, {key: days})

    assert response.status_code == 200
    assert response.json()["effective"][key] == days


@pytest.mark.parametrize("days", [MIN_RETENTION_DAYS - 1, -1, MAX_RETENTION_DAYS + 1, 10**12])
@pytest.mark.parametrize("key", RETENTION_KEYS)
def test_out_of_bounds_values_are_rejected(client: TestClient, team: dict[str, Any], key: str, days: int) -> None:
    _patch(client, team, {"traces_days": 7})

    assert _patch(client, team, {key: days}).status_code == 422
    assert _stored(team["project"]) == {"traces_days": 7}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"traces_days": True},
        {"traces_days": False},
        {"traces_days": "30"},
        {"traces_days": 30.0},
        {"traces_days": 30.5},
        {"traces_days": [30]},
        {"traces_days": {"days": 30}},
        {"unknown_days": 30},
        {"traces_days": 30, "unknown_days": 30},
        {"retention_traces_days": 30},
        {"TRACES_DAYS": 30},
        {"model_artifacts": 30},
        {"model_artifacts": "until_manually_deleted"},
        {"model_artifacts": None},
        {"effective": {"traces_days": 30}},
        {"overrides": {"traces_days": 30}},
    ],
)
def test_invalid_bodies_are_rejected(client: TestClient, team: dict[str, Any], body: dict[str, Any]) -> None:
    _patch(client, team, {"prediction_logs_days": 8})

    assert _patch(client, team, body).status_code == 422
    assert _stored(team["project"]) == {"prediction_logs_days": 8}


@pytest.mark.parametrize("raw", ["null", "[]", "[1, 2]", '"traces"', "30", "true"])
def test_non_object_bodies_are_rejected(client: TestClient, team: dict[str, Any], raw: str) -> None:
    response = client.patch(
        _url(team["project"]),
        content=raw,
        headers={**team["owner"].headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert _stored(team["project"]) is None


def test_malformed_json_is_rejected(client: TestClient, team: dict[str, Any]) -> None:
    response = client.patch(
        _url(team["project"]),
        content='{"traces_days": ',
        headers={**team["owner"].headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert _stored(team["project"]) is None


def test_model_artifacts_are_never_overridable(client: TestClient, team: dict[str, Any]) -> None:
    rejected = _patch(client, team, {"model_artifacts": 30})
    _store_raw(team["project"], '{"model_artifacts": 30, "model_artifacts_days": 30}')

    assert rejected.status_code == 422
    body = _get(client, team).json()
    assert body["model_artifacts"] == "until_manually_deleted"
    assert "model_artifacts" not in body["effective"]
    assert "model_artifacts" not in body["overrides"]
    # The next write drops the stray keys.
    _patch(client, team, {"traces_days": 7})
    assert _stored(team["project"]) == {"traces_days": 7}


# --- Invalid stored values ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        {"traces_days": True},
        {"traces_days": "30"},
        {"traces_days": 30.5},
        {"traces_days": None},
        {"traces_days": 0},
        {"traces_days": MAX_RETENTION_DAYS + 1},
        {"traces_days": [7]},
        {"traces_days": {"days": 7}},
        {"unknown_days": 7},
    ],
)
def test_invalid_stored_values_fall_back_to_defaults(
    client: TestClient, team: dict[str, Any], stored: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Values the API could never have written (e.g. a manual database edit) are ignored, never
    applied: retention cleanup must not act on them. Valid entries next to them still apply."""
    _store_raw(team["project"], json.dumps({**stored, "prediction_logs_days": 8}))

    with caplog.at_level(logging.WARNING):
        body = _get(client, team).json()

    assert body["effective"] == {**DEFAULTS, "prediction_logs_days": 8}
    assert body["overrides"] == {**NO_OVERRIDES, "prediction_logs_days": 8}
    [record] = [r for r in caplog.records if r.getMessage() == "Ignoring invalid retention overrides"]
    assert record.invalid_count == 1
    assert record.project_id == team["project"]

    # An update rewrites the column with valid entries only.
    _patch(client, team, {"evaluation_results_days": 9})
    assert _stored(team["project"]) == {"prediction_logs_days": 8, "evaluation_results_days": 9}


def test_database_rejects_non_object_overrides(team: dict[str, Any]) -> None:
    with pytest.raises(Exception, match="ck_projects_retention_overrides_object"):
        _store_raw(team["project"], "[30]")
    assert _stored(team["project"]) is None


# --- Isolation, Origin, logging -----------------------------------------------------------------


def test_projects_are_isolated(client: TestClient, team: dict[str, Any]) -> None:
    _patch(client, team, {"traces_days": 7})
    _patch(client, team, {"traces_days": 11, "prediction_logs_days": 12}, "outsider", "other")

    assert _get(client, team).json()["effective"] == {**DEFAULTS, "traces_days": 7}
    assert _get(client, team, "outsider", "other").json()["effective"] == {
        **DEFAULTS,
        "traces_days": 11,
        "prediction_logs_days": 12,
    }
    assert _stored(team["project"]) == {"traces_days": 7}


def test_admin_of_one_project_cannot_update_another(client: TestClient, team: dict[str, Any]) -> None:
    for actor in ("owner", "admin", "member"):
        response = _patch(client, team, {"traces_days": 1}, actor, "other")
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
    assert _stored(team["other"]) is None


@pytest.mark.parametrize("origin_headers", [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}])
def test_update_requires_an_allowed_origin(
    client: TestClient, team: dict[str, Any], origin_headers: dict[str, str]
) -> None:
    client.headers.pop("Origin")
    try:
        response = client.patch(
            _url(team["project"]), json={"traces_days": 7}, headers={**team["owner"].headers, **origin_headers}
        )
        read = client.get(_url(team["project"]), headers={**team["member"].headers, **origin_headers})
    finally:
        client.headers["Origin"] = ORIGIN

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert _stored(team["project"]) is None
    assert read.status_code == 200  # reads are not Origin-checked


def test_update_logs_ids_and_field_names_only(
    client: TestClient, team: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        _patch(client, team, {"traces_days": 7, "prediction_logs_days": None})

    [record] = [r for r in caplog.records if r.getMessage() == "Project retention updated"]
    assert record.project_id == team["project"]
    assert record.fields == ["prediction_logs_days", "traces_days"]


# --- Service: transactions, stale authorization, concurrency -------------------------------------


def test_commit_failure_rolls_back(team: dict[str, Any], settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    with get_session_factory()() as session:
        service.update_retention(session, access=_access(team, "owner"), changes={"traces_days": 7}, settings=settings)

    with get_session_factory()() as session:

        def fail() -> None:
            raise RuntimeError("injected failure")

        monkeypatch.setattr(session, "commit", fail)
        with pytest.raises(RuntimeError):
            service.update_retention(
                session, access=_access(team, "owner"), changes={"traces_days": 8, "prediction_logs_days": 9}, settings=settings
            )
        assert not session.in_transaction()
        monkeypatch.undo()

    assert _stored(team["project"]) == {"traces_days": 7}


@pytest.mark.parametrize(
    "changes", [{"traces_days": 0}, {"traces_days": True}, {"model_artifacts": 30}, {"traces_days": 8, "unknown": 1}]
)
def test_service_rejects_invalid_changes(team: dict[str, Any], settings: Settings, changes: dict[str, Any]) -> None:
    """Defence in depth behind the request schema: nothing is written."""
    with get_session_factory()() as session:
        with pytest.raises(ValueError):
            service.update_retention(session, access=_access(team, "owner"), changes=changes, settings=settings)
        assert not session.in_transaction()

    assert _stored(team["project"]) is None


def test_demoted_admin_is_refused(team: dict[str, Any], settings: Settings) -> None:
    """The dependency saw Admin; the role has since dropped to Member."""
    stale = _access(team, "admin")
    with get_session_factory()() as session:
        session.get_one(ProjectMember, (uuid.UUID(team["project"]), team["admin"].user.id)).role = "member"
        session.commit()

    with get_session_factory()() as session, pytest.raises(InsufficientProjectRoleError):
        service.update_retention(session, access=stale, changes={"traces_days": 7}, settings=settings)
    assert _stored(team["project"]) is None


def test_removed_admin_is_refused(team: dict[str, Any], settings: Settings) -> None:
    stale = _access(team, "admin")
    with get_session_factory()() as session:
        session.delete(session.get_one(ProjectMember, (uuid.UUID(team["project"]), team["admin"].user.id)))
        session.commit()

    with get_session_factory()() as session, pytest.raises(ProjectNotFoundError):
        service.update_retention(session, access=stale, changes={"traces_days": 7}, settings=settings)
    assert _stored(team["project"]) is None


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


def test_concurrent_updates_of_different_settings_are_both_kept(
    team: dict[str, Any], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both requests are authorized before either takes the lock; the merge happens under the
    lock, so the second sees the first's committed override instead of overwriting it."""
    barrier = threading.Barrier(2, timeout=10)
    original = repository.lock_project

    def lock_after_both_arrived(session, project_id):  # type: ignore[no-untyped-def]
        barrier.wait()
        return original(session, project_id)

    monkeypatch.setattr(repository, "lock_project", lock_after_both_arrived)

    def update(actor: str, changes: dict[str, int]) -> Callable[[], object]:
        def call() -> object:
            with get_session_factory()() as session:
                return service.update_retention(session, access=_access(team, actor), changes=changes, settings=settings)

        return call

    started = [
        _in_thread(update("owner", {"traces_days": 7})),
        _in_thread(update("admin", {"evaluation_results_days": 9})),
    ]
    for thread, _ in started:
        thread.join(timeout=30)

    assert all(isinstance(outcome[0], RetentionRead) for _, outcome in started)
    assert _stored(team["project"]) == {"evaluation_results_days": 9, "traces_days": 7}


def test_update_waits_for_a_concurrent_demotion(team: dict[str, Any], settings: Settings) -> None:
    """While the Admin is demoted under the project lock, their update waits, then is refused."""
    holder = get_session_factory()()
    try:
        assert repository.lock_project(holder, uuid.UUID(team["project"])) is not None
        repository.set_member_role(
            holder, project_id=uuid.UUID(team["project"]), user_id=team["admin"].user.id, role=ProjectRole.MEMBER
        )

        def update() -> object:
            with get_session_factory()() as session:
                return service.update_retention(
                    session, access=_access(team, "admin"), changes={"traces_days": 7}, settings=settings
                )

        thread, outcome = _in_thread(update)
        time.sleep(0.5)
        assert thread.is_alive(), "the update must wait for the project lock"
        holder.commit()
    finally:
        holder.close()
    thread.join(timeout=10)

    assert isinstance(outcome[0], InsufficientProjectRoleError)
    assert _stored(team["project"]) is None
