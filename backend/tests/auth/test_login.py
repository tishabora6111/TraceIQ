import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.database import get_session_factory
from app.core.security import decode_access_token, hash_opaque_token, verify_csrf_token
from app.modules.auth.cookies import CSRF_COOKIE_NAME, REFRESH_COOKIE_NAME, REFRESH_COOKIE_PATH
from app.modules.auth.models import RefreshToken, User
from tests.auth.conftest import ORIGIN, PASSWORD

pytestmark = pytest.mark.db

URL = "/api/v1/auth/login"
INVALID_CREDENTIALS = {"error": {"code": "unauthorized", "message": "Invalid email or password."}}

CreateUser = Callable[..., User]


def _login(client: TestClient, email: str = "user@example.com", password: str = PASSWORD, **kwargs: object) -> Response:
    return client.post(URL, json={"email": email, "password": password}, **kwargs)


def _set_cookies(response: Response) -> dict[str, dict[str, str | bool]]:
    """Parse every Set-Cookie header into {name: {attribute: value}}."""
    cookies: dict[str, dict[str, str | bool]] = {}
    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(header)
        for name, morsel in parsed.items():
            attributes: dict[str, str | bool] = {"value": morsel.value}
            attributes.update({key: value for key, value in morsel.items() if value})
            cookies[name] = attributes
    return cookies


def _tokens() -> list[RefreshToken]:
    with get_session_factory()() as session:
        return list(session.scalars(select(RefreshToken)))


def _user(email: str = "user@example.com") -> User:
    with get_session_factory()() as session:
        return session.scalars(select(User).where(User.email == email)).one()


# --- Success ----------------------------------------------------------------------------------


def test_login_returns_an_access_token_and_user(client: TestClient, settings: Settings, create_user: CreateUser) -> None:
    user = create_user()

    response = _login(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "csrf_token", "user"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    assert body["user"]["id"] == str(user.id)
    assert body["user"]["email"] == "user@example.com"
    assert set(body["user"]) == {"id", "email", "is_active", "created_at", "last_login_at"}

    claims = decode_access_token(body["access_token"], settings=settings)
    assert claims.user_id == user.id
    assert claims.expires_at - claims.issued_at == timedelta(minutes=15)


def test_login_response_is_not_cached(client: TestClient, create_user: CreateUser) -> None:
    create_user()

    assert _login(client).headers["cache-control"] == "no-store"


def test_login_creates_a_refresh_token_family(client: TestClient, settings: Settings, create_user: CreateUser) -> None:
    user = create_user()
    before = datetime.now(UTC)

    response = _login(client)

    refresh_cookie = _set_cookies(response)[REFRESH_COOKIE_NAME]["value"]
    [token] = _tokens()
    claims = decode_access_token(response.json()["access_token"], settings=settings)
    assert token.user_id == user.id
    assert token.family_id == claims.session_id
    # Only the SHA-256 digest is stored, never the token itself.
    assert token.token_hash == hash_opaque_token(str(refresh_cookie))
    assert token.token_hash != refresh_cookie
    assert token.revoked_at is None and token.revocation_reason is None and token.replaced_by_id is None
    assert before + timedelta(days=7) - timedelta(seconds=5) <= token.expires_at <= datetime.now(UTC) + timedelta(days=7)
    assert token.absolute_expires_at - token.expires_at == timedelta(days=23)


def test_login_sets_secure_session_cookies(client: TestClient, create_user: CreateUser) -> None:
    create_user()

    cookies = _set_cookies(_login(client))

    refresh = cookies[REFRESH_COOKIE_NAME]
    assert refresh["httponly"] is True
    assert refresh["secure"] is True
    assert refresh["samesite"] == "lax"
    assert refresh["path"] == "/api/v1/auth"
    assert int(str(refresh["max-age"])) in range(7 * 24 * 3600 - 5, 7 * 24 * 3600 + 1)
    assert "domain" not in refresh

    csrf = cookies[CSRF_COOKIE_NAME]
    assert "httponly" not in csrf  # the frontend reads it
    assert csrf["secure"] is True
    assert csrf["samesite"] == "lax"
    assert csrf["path"] == "/"
    assert csrf["max-age"] == refresh["max-age"]


def test_refresh_cookie_path_is_the_auth_router() -> None:
    from app.main import API_PREFIX
    from app.modules.auth.api import router

    assert REFRESH_COOKIE_PATH == API_PREFIX + router.prefix


def test_csrf_token_is_bound_to_the_session(client: TestClient, settings: Settings, create_user: CreateUser) -> None:
    create_user()

    response = _login(client)

    body = response.json()
    cookie = _set_cookies(response)[CSRF_COOKIE_NAME]["value"]
    assert cookie == body["csrf_token"]
    session_id = decode_access_token(body["access_token"], settings=settings).session_id
    assert verify_csrf_token(header_token=body["csrf_token"], cookie_token=str(cookie), session_id=session_id, settings=settings)
    assert not verify_csrf_token(
        header_token=body["csrf_token"], cookie_token=str(cookie), session_id=uuid.uuid4(), settings=settings
    )


def test_cookie_flags_follow_settings(make_client: Callable[..., TestClient], create_user: CreateUser) -> None:
    create_user()
    client = make_client(auth_cookie_secure=False, auth_cookie_samesite="strict", auth_cookie_domain="traceiq.test")

    cookies = _set_cookies(_login(client))

    for name in (REFRESH_COOKIE_NAME, CSRF_COOKIE_NAME):
        assert "secure" not in cookies[name]
        assert cookies[name]["samesite"] == "strict"
        assert cookies[name]["domain"] == "traceiq.test"
    assert cookies[REFRESH_COOKIE_NAME]["httponly"] is True


def test_login_updates_last_login_at(client: TestClient, create_user: CreateUser) -> None:
    create_user()
    assert _user().last_login_at is None

    first = _login(client)
    first_login = _user().last_login_at
    second = _login(client)
    second_login = _user().last_login_at

    assert first_login is not None and second_login is not None
    assert first_login.tzinfo is not None
    assert second_login >= first_login
    assert first.json()["user"]["last_login_at"] is not None
    assert datetime.fromisoformat(second.json()["user"]["last_login_at"]) == second_login


def test_each_login_starts_a_new_session(client: TestClient, settings: Settings, create_user: CreateUser) -> None:
    create_user()

    first, second = _login(client), _login(client)

    sessions = {decode_access_token(r.json()["access_token"], settings=settings).session_id for r in (first, second)}
    assert len(sessions) == 2
    assert {token.family_id for token in _tokens()} == sessions
    assert _set_cookies(first)[REFRESH_COOKIE_NAME] != _set_cookies(second)[REFRESH_COOKIE_NAME]


@pytest.mark.parametrize("email", ["USER@example.com", "  user@EXAMPLE.com  "])
def test_login_email_is_case_and_whitespace_insensitive(client: TestClient, create_user: CreateUser, email: str) -> None:
    create_user()

    assert _login(client, email=email).status_code == 200


def test_login_upgrades_an_outdated_password_hash(client: TestClient, create_user: CreateUser) -> None:
    weak = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8192, parallelism=1),))
    old_hash = weak.hash(PASSWORD)
    create_user(password_hash=old_hash)

    assert _login(client).status_code == 200

    new_hash = _user().password_hash
    assert new_hash != old_hash
    assert "m=65536,t=3,p=4" in new_hash
    # The upgraded hash still verifies, and is not upgraded again.
    assert _login(client).status_code == 200
    assert _user().password_hash == new_hash


# --- Failure ----------------------------------------------------------------------------------


def _assert_rejected(response: Response) -> None:
    assert response.status_code == 401
    assert response.json() == INVALID_CREDENTIALS
    assert response.headers["www-authenticate"] == "Bearer"
    assert "set-cookie" not in response.headers


def test_wrong_password_is_rejected(client: TestClient, create_user: CreateUser) -> None:
    create_user()

    _assert_rejected(_login(client, password="wrong password 123"))
    assert _tokens() == []
    assert _user().last_login_at is None


def test_unknown_email_is_rejected_identically(client: TestClient, create_user: CreateUser) -> None:
    create_user()

    unknown = _login(client, email="nobody@example.com")
    wrong_password = _login(client, password="wrong password 123")

    _assert_rejected(unknown)
    assert unknown.json() == wrong_password.json()
    assert unknown.headers.get("www-authenticate") == wrong_password.headers.get("www-authenticate")


def test_inactive_user_is_rejected_identically(client: TestClient, create_user: CreateUser) -> None:
    create_user(is_active=False)

    _assert_rejected(_login(client))
    assert _tokens() == []
    assert _user().last_login_at is None


def test_inactive_user_with_wrong_password_is_rejected(client: TestClient, create_user: CreateUser) -> None:
    create_user(is_active=False)

    _assert_rejected(_login(client, password="wrong password 123"))


def test_unknown_email_still_verifies_a_password(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.auth import service

    calls: list[str] = []
    monkeypatch.setattr(service, "verify_dummy_password", lambda password: calls.append(password))

    _assert_rejected(_login(client, email="nobody@example.com"))

    assert calls == [PASSWORD]


def test_login_accepts_passwords_shorter_than_the_registration_minimum(
    client: TestClient, create_user: CreateUser
) -> None:
    # Users created under an older, shorter policy can still log in.
    legacy = PasswordHash((Argon2Hasher(),)).hash("short")
    create_user(password_hash=legacy)

    assert _login(client, password="short").status_code == 200


@pytest.mark.parametrize(("password", "error_type"), [("", "too_short"), ("x" * 257, "too_long")])
def test_login_password_length_is_validated(client: TestClient, password: str, error_type: str) -> None:
    response = _login(client, password=password)

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["type"] == error_type
    if password:
        assert password not in response.text


@pytest.mark.parametrize("body", [{}, {"email": "user@example.com"}, {"email": "not-an-email", "password": PASSWORD}])
def test_login_rejects_invalid_bodies(client: TestClient, body: dict[str, object]) -> None:
    response = client.post(URL, json=body)

    assert response.status_code == 422
    assert PASSWORD not in response.text


@pytest.mark.parametrize(
    "headers",
    [{"Origin": "https://evil.example"}, {"Origin": "null"}, {"Referer": "https://evil.example/login"}, {}],
)
def test_login_requires_an_allowed_origin(client: TestClient, create_user: CreateUser, headers: dict[str, str]) -> None:
    create_user()
    client.headers.pop("Origin")

    response = _login(client, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert "set-cookie" not in response.headers
    assert _tokens() == []
    assert _user().last_login_at is None


def test_login_accepts_referer_when_origin_is_absent(client: TestClient, create_user: CreateUser) -> None:
    create_user()
    client.headers.pop("Origin")

    assert _login(client, headers={"Referer": f"{ORIGIN}/login"}).status_code == 200


def test_login_rejects_all_origins_when_none_are_configured(
    make_client: Callable[..., TestClient], create_user: CreateUser
) -> None:
    create_user()

    assert _login(make_client(cors_origins=[])).status_code == 403


def test_login_logs_contain_no_credentials_or_tokens(
    client: TestClient, create_user: CreateUser, caplog: pytest.LogCaptureFixture
) -> None:
    create_user()

    with caplog.at_level(logging.DEBUG):
        success = _login(client)
        _login(client, password="wrong password 123")
        _login(client, email="nobody@example.com")

    body = success.json()
    refresh_cookie = str(_set_cookies(success)[REFRESH_COOKIE_NAME]["value"])
    for secret in (PASSWORD, "wrong password 123", body["access_token"], body["csrf_token"], refresh_cookie):
        assert secret not in caplog.text
    assert "user@example.com" not in caplog.text
    assert "nobody@example.com" not in caplog.text
    messages = [record.getMessage() for record in caplog.records]
    assert "Login succeeded" in messages and messages.count("Login failed") == 2


def test_failed_logins_create_no_sessions(client: TestClient, create_user: CreateUser) -> None:
    create_user()

    for _ in range(3):
        _login(client, password="wrong password 123")

    with get_session_factory()() as session:
        assert session.scalar(select(func.count()).select_from(RefreshToken)) == 0
