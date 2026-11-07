import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.core.config import Settings
from app.core.database import get_session_factory
from app.core.security import create_access_token, generate_opaque_token, hash_opaque_token
from app.modules.auth.cookies import CSRF_COOKIE_NAME, REFRESH_COOKIE_NAME
from app.modules.auth.models import RefreshToken, User
from tests.auth.conftest import PASSWORD

pytestmark = pytest.mark.db

URL = "/api/v1/auth/me"
NOT_AUTHENTICATED = {"error": {"code": "unauthorized", "message": "Not authenticated."}}

CreateUser = Callable[..., User]


@pytest.fixture
def session_login(client: TestClient, create_user: CreateUser) -> dict[str, Any]:
    """A real login: the user, its access token, and the cookies the server set."""
    user = create_user()
    response = client.post("/api/v1/auth/login", json={"email": "user@example.com", "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    return {
        "user": user,
        "access_token": body["access_token"],
        "csrf_token": body["csrf_token"],
        "refresh_token": response.cookies.get(REFRESH_COOKIE_NAME),
    }


def _get(client: TestClient, token: str | None = None, **headers: str):
    if token is not None:
        headers.setdefault("Authorization", f"Bearer {token}")
    return client.get(URL, headers=headers)


def _assert_rejected(response) -> None:
    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert response.headers["www-authenticate"] == "Bearer"


def _claims(settings: Settings, user_id: uuid.UUID, session_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    claims: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(user_id),
        "sid": str(session_id),
        "jti": "test-jti",
        "typ": "access",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=15),
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


def _sign(claims: dict[str, Any], settings: Settings, *, key: str | None = None, algorithm: str = "HS256") -> str:
    assert settings.jwt_secret is not None
    return jwt.encode(claims, key or settings.jwt_secret.get_secret_value(), algorithm=algorithm)


def _session_id(token: str, settings: Settings) -> uuid.UUID:
    from app.core.security import decode_access_token

    return decode_access_token(token, settings=settings).session_id


# --- Success ----------------------------------------------------------------------------------


def test_me_returns_the_authenticated_user(client: TestClient, session_login: dict[str, Any]) -> None:
    response = _get(client, session_login["access_token"])

    assert response.status_code == 200
    body = response.json()
    user = session_login["user"]
    assert body["id"] == str(user.id)
    assert body["email"] == "user@example.com"
    assert body["is_active"] is True
    assert body["last_login_at"] is not None
    assert set(body) == {"id", "email", "is_active", "created_at", "last_login_at"}
    assert response.headers["cache-control"] == "no-store"


def test_me_never_returns_secrets(client: TestClient, session_login: dict[str, Any]) -> None:
    response = _get(client, session_login["access_token"])

    with get_session_factory()() as session:
        password_hash = session.get_one(User, session_login["user"].id).password_hash
        token_hash = session.scalars(select(RefreshToken.token_hash)).one()
    for secret in (
        PASSWORD,
        password_hash,
        "$argon2",
        token_hash,
        session_login["access_token"],
        session_login["refresh_token"],
        session_login["csrf_token"],
    ):
        assert secret not in response.text
    for field in ("password", "hash", "token", "session"):
        assert field not in response.text
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_bearer_scheme_is_case_insensitive(client: TestClient, session_login: dict[str, Any], scheme: str) -> None:
    response = _get(client, Authorization=f"{scheme} {session_login['access_token']}")

    assert response.status_code == 200


def test_surrounding_whitespace_is_tolerated(client: TestClient, session_login: dict[str, Any]) -> None:
    assert _get(client, Authorization=f"  Bearer   {session_login['access_token']}  ").status_code == 200


def test_session_stays_valid_after_rotation(
    client: TestClient, settings: Settings, session_login: dict[str, Any]
) -> None:
    """A rotated (revoked) token does not end the session while its replacement is active."""
    family_id = _session_id(session_login["access_token"], settings)
    now = datetime.now(UTC)
    with get_session_factory()() as session:
        old = session.scalars(select(RefreshToken)).one()
        new = RefreshToken(
            user_id=old.user_id,
            family_id=family_id,
            token_hash=hash_opaque_token(generate_opaque_token()),
            expires_at=old.expires_at,
            absolute_expires_at=old.absolute_expires_at,
        )
        session.add(new)
        session.flush()
        old.revoked_at, old.revocation_reason, old.replaced_by_id = now, "rotated", new.id
        session.commit()

    assert _get(client, session_login["access_token"]).status_code == 200


# --- Header problems --------------------------------------------------------------------------


def test_missing_authorization_header(client: TestClient) -> None:
    _assert_rejected(client.get(URL))


@pytest.mark.parametrize(
    "header",
    [
        "Basic dXNlcjpwYXNz",
        "Token abc.def.ghi",
        "Bearer",
        "Bearer ",
        "",
        "   ",
        "Bearerabc.def.ghi",
    ],
)
def test_malformed_authorization_headers(client: TestClient, header: str) -> None:
    _assert_rejected(_get(client, Authorization=header))


def test_wrong_scheme_with_a_valid_token(client: TestClient, session_login: dict[str, Any]) -> None:
    _assert_rejected(_get(client, Authorization=f"Basic {session_login['access_token']}"))


def test_token_with_embedded_whitespace(client: TestClient, session_login: dict[str, Any]) -> None:
    token = session_login["access_token"]
    _assert_rejected(_get(client, Authorization=f"Bearer {token} {token}"))


def test_token_in_query_string_is_ignored(client: TestClient, session_login: dict[str, Any]) -> None:
    _assert_rejected(client.get(URL, params={"access_token": session_login["access_token"]}))


def test_session_cookies_do_not_authenticate(client: TestClient, session_login: dict[str, Any]) -> None:
    """The refresh cookie's path covers /api/v1/auth/me, but it is never accepted as authentication."""
    client.cookies.set(REFRESH_COOKIE_NAME, session_login["refresh_token"], domain="testserver", path="/api/v1/auth")
    client.cookies.set(CSRF_COOKIE_NAME, session_login["csrf_token"], domain="testserver", path="/")

    _assert_rejected(client.get(URL))


@pytest.mark.parametrize("token_key", ["refresh_token", "csrf_token"])
def test_other_tokens_are_not_access_tokens(client: TestClient, session_login: dict[str, Any], token_key: str) -> None:
    _assert_rejected(_get(client, session_login[token_key]))


# --- Invalid JWTs -----------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["not-a-jwt", "a.b.c", "eyJhbGciOiJIUzI1NiJ9..", "x" * 5000])
def test_malformed_jwt(client: TestClient, token: str) -> None:
    _assert_rejected(_get(client, token))


def test_invalid_signature(client: TestClient, settings: Settings, session_login: dict[str, Any]) -> None:
    claims = _claims(settings, session_login["user"].id, _session_id(session_login["access_token"], settings))

    _assert_rejected(_get(client, _sign(claims, settings, key="another-secret-of-sufficient-length-0123456789")))


def test_tampered_payload(client: TestClient, session_login: dict[str, Any]) -> None:
    header, payload, signature = session_login["access_token"].split(".")
    tampered_payload = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB")

    _assert_rejected(_get(client, f"{header}.{tampered_payload}.{signature}"))


def test_unsigned_token(client: TestClient, settings: Settings, session_login: dict[str, Any]) -> None:
    claims = _claims(settings, session_login["user"].id, _session_id(session_login["access_token"], settings))

    _assert_rejected(_get(client, jwt.encode(claims, key=None, algorithm="none")))


def test_expired_token(client: TestClient, settings: Settings, session_login: dict[str, Any]) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    sid = _session_id(session_login["access_token"], settings)
    claims = _claims(settings, session_login["user"].id, sid, iat=past, nbf=past, exp=past + timedelta(minutes=15))

    _assert_rejected(_get(client, _sign(claims, settings)))


def test_token_not_yet_valid(client: TestClient, settings: Settings, session_login: dict[str, Any]) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    sid = _session_id(session_login["access_token"], settings)
    claims = _claims(settings, session_login["user"].id, sid, nbf=future, exp=future + timedelta(minutes=15))

    _assert_rejected(_get(client, _sign(claims, settings)))


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "someone-else"},
        {"aud": "another-api"},
        {"typ": "refresh"},
        {"sub": "not-a-uuid"},
        {"sid": "not-a-uuid"},
        {"sid": 42},
    ],
    ids=["issuer", "audience", "type", "subject", "session-id", "non-string-session-id"],
)
def test_wrong_claims(
    client: TestClient, settings: Settings, session_login: dict[str, Any], overrides: dict[str, str]
) -> None:
    sid = _session_id(session_login["access_token"], settings)
    claims = _claims(settings, session_login["user"].id, sid, **overrides)

    _assert_rejected(_get(client, _sign(claims, settings)))


@pytest.mark.parametrize("claim", ["sub", "sid", "exp", "typ", "iss", "aud", "nbf", "iat", "jti"])
def test_missing_claims(client: TestClient, settings: Settings, session_login: dict[str, Any], claim: str) -> None:
    sid = _session_id(session_login["access_token"], settings)
    claims = _claims(settings, session_login["user"].id, sid)
    del claims[claim]

    _assert_rejected(_get(client, _sign(claims, settings)))


# --- Valid JWT, invalid user or session ------------------------------------------------------


def test_missing_user(client: TestClient, settings: Settings) -> None:
    token, _ = create_access_token(user_id=uuid.uuid4(), session_id=uuid.uuid4(), settings=settings)

    _assert_rejected(_get(client, token))


def test_deleted_user(client: TestClient, session_login: dict[str, Any]) -> None:
    with get_session_factory()() as session:
        session.execute(text("DELETE FROM users WHERE id = :id"), {"id": session_login["user"].id})
        session.commit()

    _assert_rejected(_get(client, session_login["access_token"]))


def test_inactive_user(client: TestClient, session_login: dict[str, Any]) -> None:
    with get_session_factory()() as session:
        session.get_one(User, session_login["user"].id).is_active = False
        session.commit()

    _assert_rejected(_get(client, session_login["access_token"]))


def test_missing_session(client: TestClient, settings: Settings, create_user: CreateUser) -> None:
    user = create_user()
    token, _ = create_access_token(user_id=user.id, session_id=uuid.uuid4(), settings=settings)

    _assert_rejected(_get(client, token))


@pytest.mark.parametrize("reason", ["logout", "reuse_detected", "revoked", "rotated"])
def test_revoked_session(client: TestClient, session_login: dict[str, Any], reason: str) -> None:
    with get_session_factory()() as session:
        session.execute(text("UPDATE refresh_tokens SET revoked_at = now(), revocation_reason = :r"), {"r": reason})
        session.commit()

    _assert_rejected(_get(client, session_login["access_token"]))


def test_session_past_its_rolling_expiry(client: TestClient, session_login: dict[str, Any]) -> None:
    with get_session_factory()() as session:
        session.execute(text("UPDATE refresh_tokens SET expires_at = now() - interval '1 second'"))
        session.commit()

    _assert_rejected(_get(client, session_login["access_token"]))


def test_session_past_its_absolute_expiry(client: TestClient, session_login: dict[str, Any]) -> None:
    with get_session_factory()() as session:
        session.execute(
            text(
                "UPDATE refresh_tokens SET expires_at = now() - interval '2 seconds',"
                " absolute_expires_at = now() - interval '1 second'"
            )
        )
        session.commit()

    _assert_rejected(_get(client, session_login["access_token"]))


def test_session_of_another_user(
    client: TestClient, settings: Settings, session_login: dict[str, Any], create_user: CreateUser
) -> None:
    """A token naming user B with user A's session id is rejected, even though both exist."""
    other = create_user("other@example.com")
    sid = _session_id(session_login["access_token"], settings)
    token, _ = create_access_token(user_id=other.id, session_id=sid, settings=settings)

    _assert_rejected(_get(client, token))


def test_all_rejections_look_identical(
    client: TestClient, settings: Settings, session_login: dict[str, Any], create_user: CreateUser
) -> None:
    """Missing header, bad token, unknown user and dead session are indistinguishable to the client."""
    inactive = create_user("inactive@example.com", is_active=False)
    responses = [
        client.get(URL),
        _get(client, "garbage"),
        _get(client, create_access_token(user_id=uuid.uuid4(), session_id=uuid.uuid4(), settings=settings)[0]),
        _get(client, create_access_token(user_id=inactive.id, session_id=uuid.uuid4(), settings=settings)[0]),
        _get(
            client,
            create_access_token(user_id=session_login["user"].id, session_id=uuid.uuid4(), settings=settings)[0],
        ),
    ]

    assert {(r.status_code, r.text, r.headers["www-authenticate"]) for r in responses} == {
        (401, '{"error":{"code":"unauthorized","message":"Not authenticated."}}', "Bearer")
    }


def test_me_does_not_require_an_origin(client: TestClient, session_login: dict[str, Any]) -> None:
    """GET is not state-changing; Origin validation applies only to state-changing requests."""
    client.headers.pop("Origin")

    assert _get(client, session_login["access_token"]).status_code == 200


def test_me_is_read_only(client: TestClient, session_login: dict[str, Any]) -> None:
    with get_session_factory()() as session:
        before = session.get_one(User, session_login["user"].id)
        before_state = (before.last_login_at, before.updated_at)

    assert _get(client, session_login["access_token"]).status_code == 200

    with get_session_factory()() as session:
        after = session.get_one(User, session_login["user"].id)
        assert (after.last_login_at, after.updated_at) == before_state


def test_rejections_log_no_tokens(
    client: TestClient, settings: Settings, session_login: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    forged = _sign(
        _claims(settings, session_login["user"].id, uuid.uuid4()), settings, key="forged-secret-of-sufficient-length-01234"
    )
    missing_session, _ = create_access_token(user_id=session_login["user"].id, session_id=uuid.uuid4(), settings=settings)

    with caplog.at_level(logging.DEBUG):
        _get(client, session_login["access_token"])
        _get(client, forged)
        _get(client, missing_session)
        _get(client, Authorization=f"Basic {session_login['access_token']}")

    for secret in (session_login["access_token"], forged, missing_session, session_login["refresh_token"]):
        assert secret not in caplog.text
    reasons = [getattr(r, "reason", None) for r in caplog.records if r.getMessage() == "Authentication rejected"]
    assert reasons == ["invalid_token", "inactive_session", "malformed_authorization_header"]
