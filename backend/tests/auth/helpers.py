"""Helpers for session tests: log in, and call cookie-authenticated endpoints with explicit cookies.

Cookies are sent as an explicit ``Cookie`` header (the client's jar is cleared first), so every
test controls exactly which refresh and CSRF tokens a request presents.
"""

from dataclasses import dataclass
from http.cookies import SimpleCookie

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from app.core.database import get_session_factory
from app.modules.auth.cookies import CSRF_COOKIE_NAME, REFRESH_COOKIE_NAME
from app.modules.auth.models import RefreshToken
from tests.auth.conftest import PASSWORD

LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/auth/me"


@dataclass(frozen=True)
class Session:
    """The tokens a browser holds after login or refresh."""

    access_token: str
    csrf_token: str
    refresh_token: str


def set_cookies(response: Response) -> dict[str, dict[str, str | bool]]:
    """Every Set-Cookie header as {name: {"value": ..., attribute: value}}."""
    cookies: dict[str, dict[str, str | bool]] = {}
    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(header)
        for name, morsel in parsed.items():
            attributes: dict[str, str | bool] = {"value": morsel.value}
            attributes.update({key: value for key, value in morsel.items() if value})
            cookies[name] = attributes
    return cookies


def session_from(response: Response) -> Session:
    assert response.status_code == 200, response.text
    cookies = set_cookies(response)
    body = response.json()
    assert cookies[CSRF_COOKIE_NAME]["value"] == body["csrf_token"]
    return Session(
        access_token=body["access_token"],
        csrf_token=body["csrf_token"],
        refresh_token=str(cookies[REFRESH_COOKIE_NAME]["value"]),
    )


def login(client: TestClient, email: str = "user@example.com", password: str = PASSWORD) -> Session:
    client.cookies.clear()
    session = session_from(client.post(LOGIN_URL, json={"email": email, "password": password}))
    client.cookies.clear()
    return session


def post_with_cookies(
    client: TestClient,
    url: str,
    *,
    refresh_token: str | None = None,
    csrf_cookie: str | None = None,
    csrf_header: str | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    client.cookies.clear()
    request_headers = dict(headers or {})
    cookie_parts = []
    if refresh_token is not None:
        cookie_parts.append(f"{REFRESH_COOKIE_NAME}={refresh_token}")
    if csrf_cookie is not None:
        cookie_parts.append(f"{CSRF_COOKIE_NAME}={csrf_cookie}")
    if cookie_parts:
        request_headers["Cookie"] = "; ".join(cookie_parts)
    if csrf_header is not None:
        request_headers["X-CSRF-Token"] = csrf_header
    response = client.post(url, headers=request_headers)
    client.cookies.clear()
    return response


def refresh(client: TestClient, session: Session, **overrides: str | None) -> Response:
    """POST /refresh as the browser would, with any of the three values overridden."""
    values: dict[str, str | None] = {
        "refresh_token": session.refresh_token,
        "csrf_cookie": session.csrf_token,
        "csrf_header": session.csrf_token,
    }
    headers = overrides.pop("headers", None)
    values.update(overrides)
    return post_with_cookies(client, REFRESH_URL, headers=headers, **values)  # type: ignore[arg-type]


def logout(client: TestClient, session: Session, **overrides: str | None) -> Response:
    values: dict[str, str | None] = {
        "refresh_token": session.refresh_token,
        "csrf_cookie": session.csrf_token,
        "csrf_header": session.csrf_token,
    }
    headers = overrides.pop("headers", None)
    values.update(overrides)
    return post_with_cookies(client, LOGOUT_URL, headers=headers, **values)  # type: ignore[arg-type]


def me(client: TestClient, access_token: str) -> Response:
    client.cookies.clear()
    return client.get(ME_URL, headers={"Authorization": f"Bearer {access_token}"})


def tokens() -> list[RefreshToken]:
    with get_session_factory()() as session:
        return list(session.scalars(select(RefreshToken).order_by(RefreshToken.created_at, RefreshToken.id)))


def active_tokens() -> list[RefreshToken]:
    return [token for token in tokens() if token.revoked_at is None]
