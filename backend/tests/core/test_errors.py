"""Error responses: 401/403 errors and sanitized validation errors."""

import logging
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database import get_engine
from app.core.errors import ForbiddenError, UnauthorizedError, register_error_handlers, sanitize_validation_errors
from app.core.logging import JsonFormatter

SECRET_PASSWORD = "hunter2-pw!"  # 11 characters: below the minimum
SECRET_EMAIL = "victim-address@@example.com"


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/login")
    def login(credentials: Credentials) -> dict[str, str]:
        return {"email": credentials.email}

    @app.get("/unauthorized")
    def unauthorized() -> None:
        raise UnauthorizedError("Invalid email or password.")

    @app.get("/forbidden")
    def forbidden() -> None:
        raise ForbiddenError("CSRF token missing or invalid.")

    @app.get("/only-get")
    def only_get() -> None:
        return None

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_unauthorized_error_response(client: TestClient) -> None:
    response = client.get("/unauthorized")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"error": {"code": "unauthorized", "message": "Invalid email or password."}}


def test_forbidden_error_response(client: TestClient) -> None:
    response = client.get("/forbidden")

    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert response.json() == {"error": {"code": "forbidden", "message": "CSRF token missing or invalid."}}


def test_error_classes() -> None:
    assert (UnauthorizedError.status_code, UnauthorizedError.code) == (401, "unauthorized")
    assert (ForbiddenError.status_code, ForbiddenError.code) == (403, "forbidden")


def test_validation_error_does_not_echo_submitted_values(client: TestClient) -> None:
    response = client.post("/login", json={"email": SECRET_EMAIL, "password": SECRET_PASSWORD})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert SECRET_PASSWORD not in response.text
    assert SECRET_EMAIL not in response.text
    for detail in body["error"]["details"]:
        assert set(detail) <= {"type", "loc", "msg"}
    # Still useful: which fields failed, and why.
    failures = {tuple(detail["loc"]): detail["type"] for detail in body["error"]["details"]}
    assert failures == {("body", "email"): "value_error", ("body", "password"): "string_too_short"}


def test_too_long_password_is_not_echoed(client: TestClient) -> None:
    long_password = "x" * 257

    response = client.post("/login", json={"email": "user@example.com", "password": long_password})

    assert response.status_code == 422
    assert long_password not in response.text
    assert response.json()["error"]["details"][0]["type"] == "string_too_long"


def test_malformed_json_body_is_not_echoed(client: TestClient) -> None:
    raw = '{"email": "user@example.com", "password": "' + SECRET_PASSWORD + '"'  # missing brace

    response = client.post("/login", content=raw, headers={"Content-Type": "application/json"})

    assert response.status_code == 422
    assert SECRET_PASSWORD not in response.text
    assert response.json()["error"]["details"][0]["type"] == "json_invalid"


def test_wrong_type_value_is_not_echoed(client: TestClient) -> None:
    response = client.post("/login", json={"email": "user@example.com", "password": {"nested": SECRET_PASSWORD}})

    assert response.status_code == 422
    assert SECRET_PASSWORD not in response.text


def test_sanitize_validation_errors_keeps_only_safe_fields() -> None:
    errors = [
        {
            "type": "string_too_short",
            "loc": ("body", "password"),
            "msg": "String should have at least 12 characters",
            "input": SECRET_PASSWORD,
            "ctx": {"min_length": 12, "value": SECRET_PASSWORD},
            "url": "https://errors.pydantic.dev/2/v/string_too_short",
        }
    ]

    assert sanitize_validation_errors(errors) == [
        {"type": "string_too_short", "loc": ["body", "password"], "msg": "String should have at least 12 characters"}
    ]


def test_http_errors_keep_their_headers(client: TestClient) -> None:
    response = client.post("/only-get")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.json()["error"]["code"] == "http_error"


# --- Database errors never leak row contents ---------------------------------------------------

LEAK_EMAIL = "Leak-Marker@Example.com"  # upper case: violates ck_users_email_normalized
LEAK_HASH = "FAKE-HASH-LEAK-MARKER"


def _violate_check_constraint() -> None:
    """A real PostgreSQL check violation whose DETAIL line contains the (fake) row values."""
    with get_engine().connect() as connection:
        connection.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :email, :hash)"),
            {"id": uuid.uuid4(), "email": LEAK_EMAIL, "hash": LEAK_HASH},
        )


@pytest.fixture
def db_client(database: str) -> Iterator[TestClient]:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/db-error")
    def db_error() -> None:
        _violate_check_constraint()

    @app.get("/wrapped-db-error")
    def wrapped_db_error() -> None:
        try:
            _violate_check_constraint()
        except IntegrityError as exc:
            raise RuntimeError("could not save") from exc

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _log_output(caplog: pytest.LogCaptureFixture) -> str:
    """Everything the records would emit, through both the JSON formatter and pytest's own."""
    formatter = JsonFormatter()
    return caplog.text + "\n".join(formatter.format(record) for record in caplog.records)


@pytest.mark.db
@pytest.mark.parametrize("path", ["/db-error", "/wrapped-db-error"])
def test_database_errors_are_logged_without_row_contents(
    db_client: TestClient, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = db_client.get(path)

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error", "message": "Internal server error."}}
    output = _log_output(caplog)
    for value in (LEAK_EMAIL, LEAK_EMAIL.lower(), LEAK_HASH, "Failing row"):
        assert value not in output
        assert value not in response.text
    [record] = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert record.exc_info is None  # no traceback: it would embed the message
    assert (record.db_error_type, record.sqlstate, record.constraint) == (
        "CheckViolation",
        "23514",
        "ck_users_email_normalized",
    )


@pytest.mark.db
def test_wrapped_database_error_records_the_outer_type(db_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        db_client.get("/wrapped-db-error")

    [record] = caplog.records
    assert record.error_type == "RuntimeError"


def test_non_database_errors_still_log_a_traceback(caplog: pytest.LogCaptureFixture) -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise ValueError("ordinary failure")

    with TestClient(app, raise_server_exceptions=False) as test_client, caplog.at_level(logging.ERROR):
        response = test_client.get("/boom")

    assert response.status_code == 500
    [record] = [r for r in caplog.records if r.name == "app.core.errors"]
    assert record.exc_info is not None
    assert "ordinary failure" in caplog.text
