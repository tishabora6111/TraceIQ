import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.core.database import get_session_factory
from app.core.security import create_csrf_token, decode_access_token, hash_opaque_token
from app.modules.auth import repository, service
from app.modules.auth.cookies import CSRF_COOKIE_NAME, REFRESH_COOKIE_NAME
from app.modules.auth.errors import CsrfValidationError, NotAuthenticatedError
from app.modules.auth.models import RefreshToken, User
from tests.auth.conftest import ORIGIN, PASSWORD
from tests.auth.helpers import (
    active_tokens,
    login,
    me,
    refresh,
    session_from,
    set_cookies,
    tokens,
)

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

CreateUser = Callable[..., User]
NOT_AUTHENTICATED = {"error": {"code": "unauthorized", "message": "Not authenticated."}}
CSRF_FAILED = {"error": {"code": "csrf_failed", "message": "CSRF validation failed."}}


@pytest.fixture
def user(create_user: CreateUser) -> User:
    return create_user()


def _assert_unauthenticated(response) -> None:
    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert response.headers["www-authenticate"] == "Bearer"
    assert "set-cookie" not in response.headers


def _assert_csrf_rejected(response) -> None:
    assert response.status_code == 403
    assert response.json() == CSRF_FAILED
    assert "set-cookie" not in response.headers


def _set_column(column: str, value: str) -> None:
    with get_session_factory()() as session:
        session.execute(text(f"UPDATE refresh_tokens SET {column} = {value}"))
        session.commit()


# --- Rotation ---------------------------------------------------------------------------------


def test_refresh_rotates_the_token(client: TestClient, settings: Settings, user: User) -> None:
    first = login(client)

    response = refresh(client, first)

    second = session_from(response)
    assert response.json()["token_type"] == "bearer"
    assert response.json()["expires_in"] == 15 * 60
    assert response.json()["user"]["id"] == str(user.id)
    assert response.headers["cache-control"] == "no-store"
    assert second.refresh_token != first.refresh_token
    assert second.csrf_token != first.csrf_token
    assert second.access_token != first.access_token

    old, new = tokens()
    assert old.token_hash == hash_opaque_token(first.refresh_token)
    assert new.token_hash == hash_opaque_token(second.refresh_token)
    assert (old.revocation_reason, old.replaced_by_id) == ("rotated", new.id)
    assert old.revoked_at is not None
    assert new.revoked_at is None and new.replaced_by_id is None
    # Same family, same user, same absolute deadline.
    assert new.family_id == old.family_id
    assert new.user_id == old.user_id == user.id
    assert new.absolute_expires_at == old.absolute_expires_at
    assert decode_access_token(second.access_token, settings=settings).session_id == old.family_id


def test_rotated_session_still_authenticates(client: TestClient, user: User) -> None:
    first = login(client)
    second = session_from(refresh(client, first))

    assert me(client, second.access_token).status_code == 200
    # Access tokens are bound to the session, not to one refresh token: the earlier one stays valid.
    assert me(client, first.access_token).status_code == 200


def test_refresh_sets_secure_cookies(client: TestClient, user: User) -> None:
    cookies = set_cookies(refresh(client, login(client)))

    refresh_cookie, csrf_cookie = cookies[REFRESH_COOKIE_NAME], cookies[CSRF_COOKIE_NAME]
    assert (refresh_cookie["httponly"], refresh_cookie["secure"], refresh_cookie["samesite"]) == (True, True, "lax")
    assert refresh_cookie["path"] == "/api/v1/auth"
    assert "httponly" not in csrf_cookie
    assert (csrf_cookie["secure"], csrf_cookie["samesite"], csrf_cookie["path"]) == (True, "lax", "/")
    assert int(str(refresh_cookie["max-age"])) in range(7 * 24 * 3600 - 5, 7 * 24 * 3600 + 1)


def test_chain_of_rotations(client: TestClient, user: User) -> None:
    session = login(client)
    for _ in range(4):
        session = session_from(refresh(client, session))

    all_tokens = tokens()
    assert len(all_tokens) == 5
    assert len({token.family_id for token in all_tokens}) == 1
    assert [token.revocation_reason for token in all_tokens] == ["rotated"] * 4 + [None]
    for previous, following in zip(all_tokens, all_tokens[1:], strict=False):
        assert previous.replaced_by_id == following.id
    assert me(client, session.access_token).status_code == 200


def test_rolling_expiry_is_capped_by_the_absolute_lifetime(client: TestClient, user: User) -> None:
    session = login(client)
    _set_column("absolute_expires_at", "expires_at")  # the family ends when this token does

    refresh(client, session)

    old, new = tokens()
    assert new.expires_at == new.absolute_expires_at == old.absolute_expires_at


def test_refresh_updates_nothing_but_tokens(client: TestClient, user: User) -> None:
    login(client)
    with get_session_factory()() as session:
        before = session.get_one(User, user.id).last_login_at

    refresh(client, login(client))

    with get_session_factory()() as session:
        assert session.get_one(User, user.id).last_login_at >= before  # set by the second login only


# --- Reuse detection --------------------------------------------------------------------------


def test_reusing_a_rotated_token_revokes_the_whole_family(client: TestClient, user: User) -> None:
    first = login(client)
    second = session_from(refresh(client, first))

    _assert_unauthenticated(refresh(client, first, csrf_cookie=second.csrf_token, csrf_header=second.csrf_token))

    assert active_tokens() == []
    old, new = tokens()
    assert old.revocation_reason == "rotated"
    assert new.revocation_reason == "reuse_detected"
    # The legitimate holder's tokens stop working at once.
    _assert_unauthenticated(refresh(client, second))
    assert me(client, second.access_token).status_code == 401
    assert me(client, first.access_token).status_code == 401


def test_reuse_with_the_original_csrf_token_is_still_detected(client: TestClient, user: User) -> None:
    """The CSRF token is bound to the family, so the stale pair is CSRF-valid: reuse detection runs."""
    first = login(client)
    refresh(client, first)

    _assert_unauthenticated(refresh(client, first))

    assert active_tokens() == []


def test_reuse_does_not_affect_other_sessions(client: TestClient, settings: Settings, user: User) -> None:
    other = login(client)
    first = login(client)
    refresh(client, first)

    refresh(client, first)  # reuse

    other_family = decode_access_token(other.access_token, settings=settings).session_id
    assert [token.family_id for token in active_tokens()] == [other_family]
    assert me(client, other.access_token).status_code == 200


def test_reuse_is_logged_without_tokens(client: TestClient, user: User, caplog: pytest.LogCaptureFixture) -> None:
    first = login(client)
    second = session_from(refresh(client, first))

    with caplog.at_level(logging.INFO):
        refresh(client, first)

    [warning] = [r for r in caplog.records if r.getMessage().startswith("Refresh token reuse detected")]
    assert warning.levelno == logging.WARNING
    assert (warning.previous_reason, warning.tokens_revoked) == ("rotated", 1)
    for secret in (first.refresh_token, second.refresh_token, first.csrf_token, second.access_token):
        assert secret not in caplog.text


def test_presenting_a_logged_out_token_is_rejected(client: TestClient, user: User) -> None:
    session = login(client)
    with get_session_factory()() as db:
        db.execute(text("UPDATE refresh_tokens SET revoked_at = now(), revocation_reason = 'logout'"))
        db.commit()

    _assert_unauthenticated(refresh(client, session))
    assert [token.revocation_reason for token in tokens()] == ["logout"]


# --- Rejections without state change ----------------------------------------------------------


def test_missing_refresh_cookie(client: TestClient, user: User) -> None:
    session = login(client)

    _assert_unauthenticated(refresh(client, session, refresh_token=None))
    assert len(active_tokens()) == 1


@pytest.mark.parametrize("value", ["unknown-token", "x" * 600, "%00"])
def test_unknown_refresh_token(client: TestClient, user: User, value: str) -> None:
    session = login(client)

    _assert_unauthenticated(refresh(client, session, refresh_token=value))
    assert len(active_tokens()) == 1


def test_expired_refresh_token(client: TestClient, user: User) -> None:
    session = login(client)
    _set_column("expires_at", "now() - interval '1 second'")

    _assert_unauthenticated(refresh(client, session))
    [token] = tokens()
    assert token.revoked_at is None  # expiry is not reuse: nothing is revoked


def test_absolute_expiry_ends_the_family(client: TestClient, user: User) -> None:
    session = login(client)
    _set_column("expires_at", "now() - interval '2 seconds'")
    _set_column("absolute_expires_at", "now() - interval '1 second'")

    _assert_unauthenticated(refresh(client, session))


def test_inactive_user_cannot_refresh(client: TestClient, user: User) -> None:
    session = login(client)
    with get_session_factory()() as db:
        db.get_one(User, user.id).is_active = False
        db.commit()

    _assert_unauthenticated(refresh(client, session))
    [token] = tokens()
    assert token.revoked_at is None


def test_deleted_user_cannot_refresh(client: TestClient, user: User) -> None:
    session = login(client)
    with get_session_factory()() as db:
        db.execute(text("DELETE FROM users"))
        db.commit()

    _assert_unauthenticated(refresh(client, session))


def test_refresh_token_in_header_or_body_is_ignored(client: TestClient, user: User) -> None:
    session = login(client)
    client.cookies.clear()

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"X-CSRF-Token": session.csrf_token, "Authorization": f"Bearer {session.refresh_token}"},
        json={"refresh_token": session.refresh_token},
    )

    _assert_unauthenticated(response)


# --- CSRF ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"csrf_header": None},
        {"csrf_cookie": None},
        {"csrf_header": None, "csrf_cookie": None},
        {"csrf_header": "different.value"},
        {"csrf_header": ""},
    ],
    ids=["no-header", "no-cookie", "neither", "mismatch", "empty-header"],
)
def test_refresh_requires_matching_csrf_header_and_cookie(
    client: TestClient, user: User, overrides: dict[str, str | None]
) -> None:
    session = login(client)

    _assert_csrf_rejected(refresh(client, session, **overrides))
    [token] = tokens()
    assert token.revoked_at is None  # nothing changed


def test_forged_csrf_signature(client: TestClient, user: User) -> None:
    session = login(client)
    nonce, _, signature = session.csrf_token.partition(".")
    forged = f"{nonce}.{'A' * len(signature)}"

    _assert_csrf_rejected(refresh(client, session, csrf_header=forged, csrf_cookie=forged))


def test_csrf_token_of_another_session(client: TestClient, user: User) -> None:
    first, second = login(client), login(client)

    _assert_csrf_rejected(refresh(client, first, csrf_header=second.csrf_token, csrf_cookie=second.csrf_token))


def test_csrf_token_signed_with_another_secret(client: TestClient, settings: Settings, user: User) -> None:
    session = login(client)
    family_id = tokens()[0].family_id
    other = settings.model_copy(update={"csrf_secret": settings.jwt_secret})
    foreign = create_csrf_token(family_id, settings=other)

    _assert_csrf_rejected(refresh(client, session, csrf_header=foreign, csrf_cookie=foreign))


def test_old_csrf_header_after_rotation(client: TestClient, user: User) -> None:
    """After rotation the cookie holds the new CSRF token; replaying the old header fails double-submit."""
    first = login(client)
    second = session_from(refresh(client, first))

    _assert_csrf_rejected(refresh(client, second, csrf_header=first.csrf_token))
    assert len(active_tokens()) == 1


def test_csrf_failure_never_triggers_reuse_revocation(client: TestClient, user: User) -> None:
    """Without the CSRF token, presenting a stolen rotated token must not let an attacker end the session."""
    first = login(client)
    second = session_from(refresh(client, first))

    _assert_csrf_rejected(refresh(client, first, csrf_header="forged.value", csrf_cookie="forged.value"))

    assert len(active_tokens()) == 1
    assert me(client, second.access_token).status_code == 200


def test_csrf_failures_are_logged_without_tokens(
    client: TestClient, user: User, caplog: pytest.LogCaptureFixture
) -> None:
    session = login(client)

    with caplog.at_level(logging.INFO):
        refresh(client, session, csrf_header="forged.value")

    assert any(r.getMessage() == "CSRF validation failed" for r in caplog.records)
    assert session.refresh_token not in caplog.text and session.csrf_token not in caplog.text


# --- Origin -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{"Origin": "https://evil.example"}, {"Origin": "null"}, {"Referer": "https://evil.example/app"}, {}],
    ids=["foreign", "null", "foreign-referer", "missing"],
)
def test_refresh_requires_an_allowed_origin(client: TestClient, user: User, headers: dict[str, str]) -> None:
    session = login(client)
    client.headers.pop("Origin")
    try:
        response = refresh(client, session, headers=headers)
    finally:
        client.headers["Origin"] = ORIGIN

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    [token] = tokens()
    assert token.revoked_at is None


def test_refresh_accepts_referer_when_origin_is_absent(client: TestClient, user: User) -> None:
    session = login(client)
    client.headers.pop("Origin")
    try:
        response = refresh(client, session, headers={"Referer": f"{ORIGIN}/dashboard"})
    finally:
        client.headers["Origin"] = ORIGIN

    assert response.status_code == 200


def test_origin_is_checked_before_csrf(client: TestClient, user: User) -> None:
    session = login(client)

    response = refresh(client, session, csrf_header=None, headers={"Origin": "https://evil.example"})

    assert response.json()["error"]["code"] == "origin_not_allowed"


# --- Service: boundaries, rollback and concurrency -----------------------------------------------


def _service_refresh(settings: Settings, token: str, csrf: str, now: datetime | None = None) -> service.LoginResult:
    with get_session_factory()() as session:
        return service.refresh_session(
            session, refresh_token=token, csrf_header=csrf, csrf_cookie=csrf, settings=settings, now=now
        )


def _service_login(settings: Settings, now: datetime | None = None) -> service.LoginResult:
    with get_session_factory()() as session:
        return service.login(session, email="user@example.com", password=PASSWORD, settings=settings, now=now)


@pytest.mark.parametrize(
    ("offset", "succeeds"),
    [(timedelta(days=7) - timedelta(seconds=1), True), (timedelta(days=7), False)],
    ids=["just-before-expiry", "at-expiry"],
)
def test_rolling_expiry_boundary(settings: Settings, user: User, offset: timedelta, succeeds: bool) -> None:
    start = datetime.now(UTC).replace(microsecond=0)
    session = _service_login(settings, now=start)

    if succeeds:
        assert _service_refresh(settings, session.refresh_token, session.csrf_token, now=start + offset)
    else:
        with pytest.raises(NotAuthenticatedError):
            _service_refresh(settings, session.refresh_token, session.csrf_token, now=start + offset)


def test_absolute_expiry_boundary(settings: Settings, user: User) -> None:
    start = datetime.now(UTC).replace(microsecond=0)
    session = _service_login(settings, now=start)
    # Keep rotating just inside each rolling window until the absolute deadline is reached.
    current, now = session, start
    for _ in range(4):
        now += timedelta(days=7) - timedelta(seconds=1)
        current = _service_refresh(settings, current.refresh_token, current.csrf_token, now=now)
    assert current.refresh_token_expires_at == start + timedelta(days=30)  # capped by the absolute lifetime

    with pytest.raises(NotAuthenticatedError):
        _service_refresh(settings, current.refresh_token, current.csrf_token, now=start + timedelta(days=30))


def test_refresh_failure_rolls_back(settings: Settings, user: User, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _service_login(settings)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(repository, "insert_refresh_token", fail)
    with get_session_factory()() as db:
        with pytest.raises(RuntimeError, match="injected failure"):
            service.refresh_session(
                db,
                refresh_token=session.refresh_token,
                csrf_header=session.csrf_token,
                csrf_cookie=session.csrf_token,
                settings=settings,
            )
        assert not db.in_transaction()

    [token] = tokens()
    assert token.revoked_at is None  # the old token is still valid: nothing was half-rotated


def test_rejections_roll_back(settings: Settings, user: User) -> None:
    session = _service_login(settings)

    with get_session_factory()() as db:
        with pytest.raises(CsrfValidationError):
            service.refresh_session(
                db, refresh_token=session.refresh_token, csrf_header="x.y", csrf_cookie="x.y", settings=settings
            )
        assert not db.in_transaction()
        with pytest.raises(NotAuthenticatedError):
            service.refresh_session(db, refresh_token="unknown", csrf_header=None, csrf_cookie=None, settings=settings)
        assert not db.in_transaction()


def _run_concurrently(monkeypatch: pytest.MonkeyPatch, calls: list[Callable[[], object]]) -> list[object]:
    """Run the calls in threads that all finish their unlocked lookup before any takes the user lock."""
    barrier = threading.Barrier(len(calls), timeout=10)
    original_lock_user = repository.lock_user

    def lock_user_after_everyone_looked_up(session, user_id):  # type: ignore[no-untyped-def]
        barrier.wait()
        return original_lock_user(session, user_id)

    monkeypatch.setattr(repository, "lock_user", lock_user_after_everyone_looked_up)
    results: list[object] = [None] * len(calls)

    def run(index: int) -> None:
        try:
            results[index] = calls[index]()
        except BaseException as exc:  # noqa: BLE001 - collected for assertions
            results[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    return results


def test_concurrent_refreshes_with_the_same_token(
    settings: Settings, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly one rotates. The other then sees a rotated token: strict reuse detection revokes the
    family, so no replacement survives (the frontend must serialize refreshes)."""
    session = _service_login(settings)
    call = lambda: _service_refresh(settings, session.refresh_token, session.csrf_token)  # noqa: E731

    results = _run_concurrently(monkeypatch, [call, call])

    successes = [r for r in results if isinstance(r, service.LoginResult)]
    failures = [r for r in results if isinstance(r, NotAuthenticatedError)]
    assert (len(successes), len(failures)) == (1, 1), results
    assert active_tokens() == []
    reasons = sorted(token.revocation_reason or "" for token in tokens())
    assert reasons == ["reuse_detected", "rotated"]
    assert len(tokens()) == 2  # one replacement only


def test_concurrent_rotation_and_reuse_leave_no_active_token(
    settings: Settings, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale token replayed while the current one is being rotated: whichever runs first, the
    family ends fully revoked (no replacement escapes reuse detection)."""
    first = _service_login(settings)
    current = _service_refresh(settings, first.refresh_token, first.csrf_token)

    results = _run_concurrently(
        monkeypatch,
        [
            lambda: _service_refresh(settings, current.refresh_token, current.csrf_token),
            lambda: _service_refresh(settings, first.refresh_token, first.csrf_token),
        ],
    )

    assert isinstance(results[1], NotAuthenticatedError)
    assert active_tokens() == []


def test_concurrent_refreshes_of_different_sessions_both_succeed(
    settings: Settings, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    one, two = _service_login(settings), _service_login(settings)

    results = _run_concurrently(
        monkeypatch,
        [
            lambda: _service_refresh(settings, one.refresh_token, one.csrf_token),
            lambda: _service_refresh(settings, two.refresh_token, two.csrf_token),
        ],
    )

    assert all(isinstance(r, service.LoginResult) for r in results), results
    assert len(active_tokens()) == 2


def test_refresh_result_repr_hides_tokens(settings: Settings, user: User) -> None:
    session = _service_login(settings)
    result = _service_refresh(settings, session.refresh_token, session.csrf_token)

    for secret in (result.refresh_token, result.access_token, result.csrf_token):
        assert secret not in repr(result)


def test_refresh_with_random_family_csrf_is_rejected(settings: Settings, user: User) -> None:
    session = _service_login(settings)
    foreign = create_csrf_token(uuid.uuid4(), settings=settings)

    with pytest.raises(CsrfValidationError):
        _service_refresh(settings, session.refresh_token, foreign)


def test_refresh_token_row_lookup_uses_the_hash(settings: Settings, user: User) -> None:
    session = _service_login(settings)

    with get_session_factory()() as db:
        assert repository.get_refresh_token_by_hash(db, session.refresh_token) is None  # never the plaintext
        found = repository.get_refresh_token_by_hash(db, hash_opaque_token(session.refresh_token))
        assert isinstance(found, RefreshToken)
