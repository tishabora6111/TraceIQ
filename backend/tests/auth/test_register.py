import logging
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import verify_password
from app.modules.auth.models import User
from tests.auth.conftest import ORIGIN, PASSWORD

pytestmark = pytest.mark.db

URL = "/api/v1/auth/register"


def _register(client: TestClient, email: str = "user@example.com", password: str = PASSWORD, **kwargs: object):
    return client.post(URL, json={"email": email, "password": password}, **kwargs)


def _stored_users() -> list[User]:
    with get_session_factory()() as session:
        return list(session.scalars(select(User)))


def test_register_creates_an_active_user(client: TestClient) -> None:
    response = _register(client)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "is_active", "created_at", "last_login_at"}
    assert body["email"] == "user@example.com"
    assert body["is_active"] is True
    assert body["last_login_at"] is None

    [user] = _stored_users()
    assert str(user.id) == body["id"]
    assert user.password_hash.startswith("$argon2id$")
    assert verify_password(PASSWORD, user.password_hash) == (True, None)


def test_register_does_not_return_or_store_the_password(client: TestClient) -> None:
    response = _register(client)

    assert PASSWORD not in response.text
    assert "password" not in response.text
    assert "$argon2" not in response.text
    [user] = _stored_users()
    assert PASSWORD not in user.password_hash


def test_register_does_not_start_a_session(client: TestClient) -> None:
    response = _register(client)

    assert "set-cookie" not in response.headers
    assert "access_token" not in response.json()


def test_register_normalizes_the_email(client: TestClient) -> None:
    response = _register(client, email="  New.User@Example.COM ")

    assert response.status_code == 201
    assert response.json()["email"] == "new.user@example.com"
    assert [user.email for user in _stored_users()] == ["new.user@example.com"]


@pytest.mark.parametrize("duplicate", ["user@example.com", "USER@example.com", " User@Example.Com "])
def test_register_rejects_a_duplicate_email(client: TestClient, duplicate: str) -> None:
    assert _register(client).status_code == 201

    response = _register(client, email=duplicate, password="another-password-123")

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "email_already_registered", "message": "An account with this email already exists."}
    }
    assert len(_stored_users()) == 1


@pytest.mark.parametrize("email", ["not-an-email", "user@", "@example.com", "user@@example.com", "", "a b@example.com"])
def test_register_rejects_invalid_emails(client: TestClient, email: str) -> None:
    response = _register(client, email=email)

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "email"]
    assert _stored_users() == []


@pytest.mark.parametrize(("password", "error_type"), [("x" * 11, "too_short"), ("x" * 257, "too_long")])
def test_register_enforces_password_length(client: TestClient, password: str, error_type: str) -> None:
    response = _register(client, password=password)

    assert response.status_code == 422
    [detail] = response.json()["error"]["details"]
    assert (detail["loc"], detail["type"]) == (["body", "password"], error_type)
    assert password not in response.text
    assert _stored_users() == []


@pytest.mark.parametrize("password", ["x" * 12, "x" * 256, "pässwörd-ünïcode-€"])
def test_register_accepts_passwords_within_the_policy(client: TestClient, password: str) -> None:
    assert _register(client, password=password).status_code == 201


def test_register_has_no_composition_rules(client: TestClient) -> None:
    assert _register(client, password="aaaaaaaaaaaa").status_code == 201


@pytest.mark.parametrize("body", [{}, {"email": "user@example.com"}, {"password": PASSWORD}, {"email": 1, "password": 2}])
def test_register_rejects_incomplete_bodies(client: TestClient, body: dict[str, object]) -> None:
    response = client.post(URL, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_validation_errors_do_not_echo_submitted_values(client: TestClient) -> None:
    response = _register(client, email="victim@@example.com", password="short-pw")

    assert response.status_code == 422
    assert "victim" not in response.text
    assert "short-pw" not in response.text


# --- Registration policy ----------------------------------------------------------------------


def test_registration_is_enabled_by_default_in_development(make_client: Callable[..., TestClient]) -> None:
    assert _register(make_client(env="development")).status_code == 201


def test_registration_is_disabled_by_default_in_production(make_client: Callable[..., TestClient]) -> None:
    response = _register(make_client(env="production"))

    assert response.status_code == 403
    assert response.json() == {"error": {"code": "registration_disabled", "message": "Registration is disabled."}}
    assert _stored_users() == []


def test_registration_can_be_enabled_in_production(make_client: Callable[..., TestClient]) -> None:
    client = make_client(env="production", auth_registration_enabled=True)

    assert _register(client).status_code == 201


def test_registration_can_be_disabled_in_development(make_client: Callable[..., TestClient]) -> None:
    client = make_client(env="development", auth_registration_enabled=False)

    assert _register(client).status_code == 403
    assert _stored_users() == []


# --- Origin validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://app.traceiq.test"},  # wrong scheme
        {"Origin": "https://app.traceiq.test:8443"},  # wrong port
        {"Origin": "https://evil.example", "Referer": f"{ORIGIN}/register"},  # Referer ignored when Origin is set
        {"Referer": "https://evil.example/register"},
    ],
)
def test_register_rejects_disallowed_origins(client: TestClient, headers: dict[str, str]) -> None:
    client.headers.pop("Origin")

    response = _register(client, headers=headers)

    assert response.status_code == 403
    assert response.json() == {"error": {"code": "origin_not_allowed", "message": "Request origin is not allowed."}}
    assert _stored_users() == []


def test_register_rejects_requests_without_origin_or_referer(client: TestClient) -> None:
    client.headers.pop("Origin")

    assert _register(client).status_code == 403
    assert _stored_users() == []


def test_register_accepts_referer_when_origin_is_absent(client: TestClient) -> None:
    client.headers.pop("Origin")

    assert _register(client, headers={"Referer": f"{ORIGIN}/register?next=/"}).status_code == 201


def test_origin_is_checked_before_the_body(client: TestClient) -> None:
    response = client.post(URL, json={"email": "bad"}, headers={"Origin": "https://evil.example"})

    assert response.status_code == 403


def test_registration_logs_contain_no_credentials(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        _register(client, email="logged-user@example.com")
        _register(client, email="logged-user@example.com")

    assert PASSWORD not in caplog.text
    assert "logged-user" not in caplog.text
    assert "$argon2" not in caplog.text


def test_other_integrity_errors_are_not_reported_as_duplicates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-email constraint violation is a 500 whose logs carry no row contents (email, hash)."""
    from app.core.logging import JsonFormatter
    from app.modules.auth import service

    monkeypatch.setattr(service, "hash_password", lambda password: "")  # violates ck_users_password_hash_not_empty

    with caplog.at_level(logging.DEBUG):
        response = _register(client, email="row-leak-marker@example.com")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    output = caplog.text + "".join(JsonFormatter().format(record) for record in caplog.records)
    assert "row-leak-marker" not in output
    assert "Failing row" not in output
    failures = [r for r in caplog.records if r.getMessage() == "User registration failed"]
    assert [(r.sqlstate, r.constraint) for r in failures] == [("23514", "ck_users_password_hash_not_empty")]
    assert _stored_users() == []


def test_rejected_origin_is_truncated_in_logs(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    long_origin = "https://" + "a" * 600 + ".example"

    with caplog.at_level(logging.WARNING):
        response = _register(client, headers={"Origin": long_origin})

    assert response.status_code == 403
    [record] = [r for r in caplog.records if r.getMessage() == "Request origin rejected"]
    assert record.origin == long_origin[:255]
    assert len(record.origin) == 255


def test_rejected_invalid_origin_is_logged_as_none(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _register(client, headers={"Origin": "null"})

    [record] = [r for r in caplog.records if r.getMessage() == "Request origin rejected"]
    assert record.origin is None
