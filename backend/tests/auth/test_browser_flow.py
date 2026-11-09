"""Session flows as a browser (or Swagger UI on the API's own origin) performs them.

Unlike the other session tests, cookies here are never set by hand: the client's cookie jar keeps
whatever the server set, exactly as a browser does, and the CSRF token is sent only as the
X-CSRF-Token header copied from the login or refresh response body.
"""

import logging
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.modules.auth.cookies import CSRF_COOKIE_NAME, REFRESH_COOKIE_NAME
from app.modules.auth.models import User
from tests.auth.conftest import PASSWORD
from tests.auth.helpers import active_tokens, tokens

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

# Swagger UI is served by the API itself, so its requests carry the API's own origin.
DOCS_ORIGIN = "http://localhost:8000"
CREDENTIALS = {"email": "user@example.com", "password": PASSWORD}


@pytest.fixture
def browser(settings: Settings, create_user: Callable[..., User]) -> Iterator[TestClient]:
    """Plain-HTTP localhost, as in local development (Secure off, since the test client, unlike
    Chrome and Firefox, does not treat http://localhost as a secure context)."""
    from app.main import create_app

    create_user()
    app = create_app(settings.model_copy(update={"cors_origins": [DOCS_ORIGIN], "auth_cookie_secure": False}))
    with TestClient(
        app, base_url="http://localhost:8000", headers={"Origin": DOCS_ORIGIN}, raise_server_exceptions=False
    ) as client:
        yield client


def _login(browser: TestClient) -> str:
    response = browser.post("/api/v1/auth/login", json=CREDENTIALS)
    assert response.status_code == 200
    assert {cookie.name for cookie in browser.cookies.jar} == {REFRESH_COOKIE_NAME, CSRF_COOKIE_NAME}
    return response.json()["csrf_token"]


def _csrf_failure(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    [record] = [r for r in caplog.records if r.getMessage() == "CSRF validation failed"]
    return record


# --- The reported scenario: login, then logout ---------------------------------------------------


def test_logout_with_the_csrf_token_from_the_login_response(browser: TestClient) -> None:
    csrf_token = _login(browser)

    response = browser.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 204
    assert list(browser.cookies.jar) == []  # both cookies were expired by the response
    [token] = tokens()
    assert token.revocation_reason == "logout"


def test_access_token_is_dead_after_a_browser_logout(browser: TestClient) -> None:
    response = browser.post("/api/v1/auth/login", json=CREDENTIALS)
    access_token, csrf_token = response.json()["access_token"], response.json()["csrf_token"]

    browser.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})

    assert browser.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}).status_code == 401


def test_refresh_then_logout_uses_the_latest_csrf_token(browser: TestClient) -> None:
    first = _login(browser)
    refreshed = browser.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": first})
    assert refreshed.status_code == 200
    latest = refreshed.json()["csrf_token"]

    assert browser.post("/api/v1/auth/logout", headers={"X-CSRF-Token": latest}).status_code == 204
    assert active_tokens() == []


def test_repeated_browser_logout(browser: TestClient) -> None:
    csrf_token = _login(browser)
    browser.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})

    # The cookies are gone, so there is no session to protect: still 204.
    assert browser.post("/api/v1/auth/logout").status_code == 204


# --- Rejected CSRF: each failure is diagnosable from the logs, without values --------------------


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        (None, "missing_header"),  # the X-CSRF-Token field left empty
        ("partial", "header_cookie_mismatch"),  # double-click copies only up to '-' or '.'
        ("quoted", "header_cookie_mismatch"),  # copied with the JSON quotes
        ("access", "header_cookie_mismatch"),  # the access token pasted by mistake
    ],
)
def test_invalid_csrf_header_is_rejected_and_explained(
    browser: TestClient, caplog: pytest.LogCaptureFixture, header: str | None, reason: str
) -> None:
    response = browser.post("/api/v1/auth/login", json=CREDENTIALS)
    csrf_token, access_token = response.json()["csrf_token"], response.json()["access_token"]
    values = {
        None: None,
        "partial": csrf_token.split(".")[0],
        "quoted": f'"{csrf_token}"',
        "access": access_token,
    }

    with caplog.at_level(logging.INFO):
        rejected = browser.post(
            "/api/v1/auth/logout", headers={} if values[header] is None else {"X-CSRF-Token": values[header]}
        )

    assert rejected.status_code == 403
    assert rejected.json() == {"error": {"code": "csrf_failed", "message": "CSRF validation failed."}}
    assert len(active_tokens()) == 1  # nothing was revoked
    assert {cookie.name for cookie in browser.cookies.jar} == {REFRESH_COOKIE_NAME, CSRF_COOKIE_NAME}
    record = _csrf_failure(caplog)
    assert record.reason == reason
    assert record.cookie_length == len(csrf_token)
    assert record.header_length == (len(values[header]) if values[header] else 0)
    for secret in (csrf_token, access_token, browser.cookies.get(REFRESH_COOKIE_NAME)):
        assert secret not in caplog.text


def test_old_csrf_token_after_refresh_is_rejected(browser: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    first = _login(browser)
    browser.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": first})

    with caplog.at_level(logging.INFO):
        response = browser.post("/api/v1/auth/logout", headers={"X-CSRF-Token": first})

    assert response.status_code == 403
    assert _csrf_failure(caplog).reason == "header_cookie_mismatch"
    assert len(active_tokens()) == 1


def test_missing_csrf_cookie_is_rejected(browser: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    csrf_token = _login(browser)
    browser.cookies.delete(CSRF_COOKIE_NAME)

    with caplog.at_level(logging.INFO):
        response = browser.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 403
    assert _csrf_failure(caplog).reason == "missing_cookie"


def test_matching_but_forged_csrf_values_are_rejected(browser: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    _login(browser)
    forged = "forged-nonce.forged-signature"
    [original] = [cookie for cookie in browser.cookies.jar if cookie.name == CSRF_COOKIE_NAME]
    browser.cookies.delete(CSRF_COOKIE_NAME)
    browser.cookies.set(CSRF_COOKIE_NAME, forged, domain=original.domain, path=original.path)

    with caplog.at_level(logging.INFO):
        response = browser.post("/api/v1/auth/logout", headers={"X-CSRF-Token": forged})

    assert response.status_code == 403
    assert _csrf_failure(caplog).reason == "invalid_signature"
    assert len(active_tokens()) == 1


def test_foreign_origin_is_still_rejected(browser: TestClient) -> None:
    csrf_token = _login(browser)

    response = browser.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token, "Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert len(active_tokens()) == 1


# --- What Swagger UI is shown -------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/auth/logout", "/api/v1/auth/refresh"])
def test_openapi_shows_only_the_csrf_header(browser: TestClient, path: str) -> None:
    """Swagger UI cannot send cookies it is given (browsers forbid setting the Cookie header), so the
    schema must not offer cookie fields; the browser sends the real cookies itself."""
    parameters = browser.get("/openapi.json").json()["paths"][path]["post"]["parameters"]

    assert [(p["in"], p["name"]) for p in parameters] == [("header", "X-CSRF-Token")]
    assert "csrf_token" in parameters[0]["description"]
