import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.core.database import get_session_factory
from app.core.security import decode_access_token
from app.modules.auth import service
from app.modules.auth.cookies import CSRF_COOKIE_NAME, REFRESH_COOKIE_NAME
from app.modules.auth.models import RevocationReason, User
from tests.auth.conftest import ORIGIN
from tests.auth.helpers import (
    LOGOUT_URL,
    active_tokens,
    login,
    logout,
    me,
    post_with_cookies,
    refresh,
    session_from,
    set_cookies,
    tokens,
)

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

CreateUser = Callable[..., User]


@pytest.fixture
def user(create_user: CreateUser) -> User:
    return create_user()


def _assert_cookies_cleared(response, *, secure: bool = True, samesite: str = "lax", domain: str | None = None) -> None:
    cookies = set_cookies(response)
    assert set(cookies) == {REFRESH_COOKIE_NAME, CSRF_COOKIE_NAME}
    for name, path in ((REFRESH_COOKIE_NAME, "/api/v1/auth"), (CSRF_COOKIE_NAME, "/")):
        cookie = cookies[name]
        assert cookie["value"] == ""
        assert cookie["max-age"] == "0"
        # Starlette sets Expires to "now"; with Max-Age=0 the browser drops the cookie at once.
        assert parsedate_to_datetime(str(cookie["expires"])) <= datetime.now(UTC) + timedelta(seconds=1)
        # Same path and domain as when set, or the browser would keep the original cookie.
        assert cookie["path"] == path
        assert cookie.get("domain") == domain
        assert ("secure" in cookie) is secure
        assert cookie["samesite"] == samesite
    assert cookies[REFRESH_COOKIE_NAME]["httponly"] is True
    assert "httponly" not in cookies[CSRF_COOKIE_NAME]


# --- Logout -------------------------------------------------------------------------------------


def test_logout_revokes_the_session_and_clears_cookies(client: TestClient, user: User) -> None:
    session = login(client)

    response = logout(client, session)

    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    _assert_cookies_cleared(response)
    [token] = tokens()
    assert token.revocation_reason == "logout" and token.revoked_at is not None


def test_access_token_stops_working_immediately(client: TestClient, user: User) -> None:
    session = login(client)
    assert me(client, session.access_token).status_code == 200

    logout(client, session)

    assert me(client, session.access_token).status_code == 401


def test_refresh_token_is_dead_after_logout(client: TestClient, user: User) -> None:
    session = login(client)
    logout(client, session)

    assert refresh(client, session).status_code == 401
    assert [token.revocation_reason for token in tokens()] == ["logout"]


def test_logout_revokes_every_token_of_a_rotated_family(client: TestClient, user: User) -> None:
    first = login(client)
    second = session_from(refresh(client, first))

    assert logout(client, second).status_code == 204

    assert active_tokens() == []
    assert [token.revocation_reason for token in tokens()] == ["rotated", "logout"]
    assert me(client, first.access_token).status_code == 401
    assert me(client, second.access_token).status_code == 401


def test_logout_with_a_stale_rotated_token_ends_the_session(client: TestClient, user: User) -> None:
    first = login(client)
    second = session_from(refresh(client, first))

    assert logout(client, first).status_code == 204

    assert active_tokens() == []
    assert me(client, second.access_token).status_code == 401


def test_logout_leaves_other_sessions_alone(client: TestClient, settings: Settings, user: User) -> None:
    other, current = login(client), login(client)

    logout(client, current)

    other_family = decode_access_token(other.access_token, settings=settings).session_id
    assert [token.family_id for token in active_tokens()] == [other_family]
    assert me(client, other.access_token).status_code == 200


def test_repeated_logout_is_safe(client: TestClient, user: User) -> None:
    session = login(client)

    first, second = logout(client, session), logout(client, session)

    assert (first.status_code, second.status_code) == (204, 204)
    _assert_cookies_cleared(second)
    assert [token.revocation_reason for token in tokens()] == ["logout"]  # revoked once, unchanged since


@pytest.mark.parametrize(
    "overrides",
    [
        {"refresh_token": None, "csrf_cookie": None, "csrf_header": None},
        {"refresh_token": None},
        {"refresh_token": "unknown-token"},
        {"refresh_token": "x" * 600},
    ],
    ids=["no-cookies", "no-refresh-cookie", "unknown-token", "oversized-token"],
)
def test_logout_without_a_known_session_succeeds(
    client: TestClient, user: User, overrides: dict[str, str | None]
) -> None:
    session = login(client)

    response = logout(client, session, **overrides)

    assert response.status_code == 204
    _assert_cookies_cleared(response)
    assert len(active_tokens()) == 1  # nothing was revoked


def test_logout_of_an_expired_session_succeeds(client: TestClient, user: User) -> None:
    session = login(client)
    with get_session_factory()() as db:
        db.execute(text("UPDATE refresh_tokens SET expires_at = now() - interval '1 second'"))
        db.commit()

    response = logout(client, session)

    assert response.status_code == 204
    [token] = tokens()
    assert token.revocation_reason == "logout"


def test_logout_after_reuse_detection_succeeds(client: TestClient, user: User) -> None:
    first = login(client)
    refresh(client, first)
    refresh(client, first)  # reuse: family revoked

    assert logout(client, first).status_code == 204
    assert sorted(token.revocation_reason or "" for token in tokens()) == ["reuse_detected", "rotated"]


def test_logout_of_a_deleted_user_succeeds(client: TestClient, user: User) -> None:
    session = login(client)
    with get_session_factory()() as db:
        db.execute(text("DELETE FROM users"))
        db.commit()

    assert logout(client, session).status_code == 204


# --- CSRF and Origin ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"csrf_header": None}, {"csrf_cookie": None}, {"csrf_header": "forged.value"}],
    ids=["no-header", "no-cookie", "mismatch"],
)
def test_logout_of_a_known_session_requires_csrf(
    client: TestClient, user: User, overrides: dict[str, str | None]
) -> None:
    session = login(client)

    response = logout(client, session, **overrides)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert "set-cookie" not in response.headers  # cookies are kept: the session is still valid
    assert len(active_tokens()) == 1
    assert me(client, session.access_token).status_code == 200


def test_logout_with_another_sessions_csrf_token_is_rejected(client: TestClient, user: User) -> None:
    first, second = login(client), login(client)

    response = logout(client, first, csrf_header=second.csrf_token, csrf_cookie=second.csrf_token)

    assert response.status_code == 403
    assert len(active_tokens()) == 2


@pytest.mark.parametrize(
    "headers", [{"Origin": "https://evil.example"}, {"Origin": "null"}, {}], ids=["foreign", "null", "missing"]
)
def test_logout_requires_an_allowed_origin(client: TestClient, user: User, headers: dict[str, str]) -> None:
    session = login(client)
    client.headers.pop("Origin")
    try:
        response = logout(client, session, headers=headers)
    finally:
        client.headers["Origin"] = ORIGIN

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert len(active_tokens()) == 1


def test_logout_does_not_accept_a_bearer_token_instead_of_the_cookie(client: TestClient, user: User) -> None:
    session = login(client)

    response = post_with_cookies(client, LOGOUT_URL, headers={"Authorization": f"Bearer {session.access_token}"})

    assert response.status_code == 204
    assert len(active_tokens()) == 1  # only the refresh cookie identifies the session to end


# --- Cookie attributes follow settings ------------------------------------------------------------


def test_cleared_cookies_match_configured_attributes(make_client: Callable[..., TestClient], user: User) -> None:
    client = make_client(auth_cookie_secure=False, auth_cookie_samesite="strict", auth_cookie_domain="traceiq.test")
    session = login(client)

    response = logout(client, session)

    _assert_cookies_cleared(response, secure=False, samesite="strict", domain="traceiq.test")


def test_logout_logs_no_tokens(client: TestClient, user: User, caplog: pytest.LogCaptureFixture) -> None:
    session = login(client)

    with caplog.at_level(logging.INFO):
        logout(client, session)
        logout(client, session, refresh_token="unknown-token")

    messages = [r.getMessage() for r in caplog.records]
    assert "Logged out" in messages and "Logout without a known session" in messages
    for secret in (session.refresh_token, session.csrf_token, session.access_token, "unknown-token"):
        assert secret not in caplog.text


# --- Account-wide revocation -------------------------------------------------------------------


def test_revoke_all_sessions(client: TestClient, user: User, create_user: CreateUser) -> None:
    first = login(client)
    second = session_from(refresh(client, login(client)))
    other_user = create_user("other@example.com")
    other = login(client, email="other@example.com")

    with get_session_factory()() as db:
        revoked = service.revoke_all_sessions(db, user_id=user.id)

    assert revoked == 2  # one active token per family
    for access_token in (first.access_token, second.access_token):
        assert me(client, access_token).status_code == 401
    assert refresh(client, second).status_code == 401
    assert me(client, other.access_token).status_code == 200
    reasons = {(token.user_id, token.revocation_reason) for token in tokens()}
    assert (user.id, "revoked") in reasons
    assert (other_user.id, None) in reasons


def test_revoke_all_sessions_is_idempotent(user: User) -> None:
    with get_session_factory()() as db:
        assert service.revoke_all_sessions(db, user_id=user.id) == 0
        assert service.revoke_all_sessions(db, user_id=user.id, reason=RevocationReason.REVOKED) == 0


def test_revoke_all_sessions_rolls_back_on_failure(
    client: TestClient, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.auth import repository

    login(client)

    def fail(*args: object, **kwargs: object) -> int:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(repository, "revoke_user_tokens", fail)
    with get_session_factory()() as db:
        with pytest.raises(RuntimeError):
            service.revoke_all_sessions(db, user_id=user.id)
        assert not db.in_transaction()
    assert len(active_tokens()) == 1


def test_logout_rolls_back_on_failure(client: TestClient, user: User, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.auth import repository

    session = login(client)

    def fail(*args: object, **kwargs: object) -> int:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(repository, "revoke_family", fail)
    with get_session_factory()() as db:
        with pytest.raises(RuntimeError):
            service.logout(
                db, refresh_token=session.refresh_token, csrf_header=session.csrf_token, csrf_cookie=session.csrf_token
            )
        assert not db.in_transaction()
    assert len(active_tokens()) == 1
