"""require_allowed_origin_for_writes as a router-level dependency on a JWT-authenticated router."""

from collections.abc import Callable, Iterator

import pytest
from fastapi import APIRouter, Depends, FastAPI, Response
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.modules.auth.dependencies import (
    SAFE_METHODS,
    CurrentUser,
    require_allowed_origin,
    require_allowed_origin_for_writes,
)
from app.modules.auth.models import User
from tests.auth.conftest import ORIGIN
from tests.auth.helpers import login

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

WRITE_METHODS = ["POST", "PUT", "PATCH", "DELETE"]
SAFE_REQUEST_METHODS = ["GET", "HEAD", "OPTIONS"]
ORIGIN_REJECTED = {"error": {"code": "origin_not_allowed", "message": "Request origin is not allowed."}}


def _protected_app(settings: Settings) -> FastAPI:
    """A router as future modules (projects) will declare it: Origin for writes on the router,
    bearer authentication on every route."""
    app = FastAPI()
    register_error_handlers(app)
    app.dependency_overrides[get_settings] = lambda: settings
    router = APIRouter(prefix="/api/v1/things", dependencies=[Depends(require_allowed_origin_for_writes)])

    @router.api_route("/item", methods=WRITE_METHODS + SAFE_REQUEST_METHODS)
    def item(user: CurrentUser) -> Response:
        return Response(status_code=204, headers={"X-User": str(user.id)})

    app.include_router(router)
    return app


@pytest.fixture
def api(settings: Settings, create_user: Callable[..., User]) -> Iterator[tuple[TestClient, str]]:
    """The protected app, and a valid access token from a real login."""
    from app.main import create_app

    create_user()
    with TestClient(create_app(settings), base_url="https://testserver", headers={"Origin": ORIGIN}) as auth_client:
        access_token = login(auth_client).access_token
    with TestClient(
        _protected_app(settings), base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        yield client, access_token


def _call(client: TestClient, method: str, token: str | None, **headers: str):
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.request(method, "/api/v1/things/item", headers=headers)


# --- Writes require an allowed Origin ---------------------------------------------------------


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_write_with_an_allowed_origin_succeeds(api: tuple[TestClient, str], method: str) -> None:
    client, token = api

    assert _call(client, method, token, Origin=ORIGIN).status_code == 204


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_write_with_an_allowed_referer_succeeds(api: tuple[TestClient, str], method: str) -> None:
    client, token = api

    assert _call(client, method, token, Referer=f"{ORIGIN}/projects/1").status_code == 204


@pytest.mark.parametrize("method", WRITE_METHODS)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}, {"Referer": "https://evil.example/x"}],
    ids=["missing", "foreign", "null", "foreign-referer"],
)
def test_write_without_an_allowed_origin_is_rejected(
    api: tuple[TestClient, str], method: str, headers: dict[str, str]
) -> None:
    client, token = api

    response = _call(client, method, token, **headers)

    assert response.status_code == 403
    assert response.json() == ORIGIN_REJECTED


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_origin_is_checked_before_authentication(api: tuple[TestClient, str], method: str) -> None:
    """A cross-site write is refused without even looking at the token."""
    client, _ = api

    response = _call(client, method, None, Origin="https://evil.example")

    assert response.status_code == 403


# --- Safe methods are not Origin-checked, but are still authenticated -------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example"}], ids=["no-origin", "foreign"])
def test_safe_methods_skip_the_origin_check(api: tuple[TestClient, str], method: str, headers: dict[str, str]) -> None:
    client, token = api

    assert _call(client, method, token, **headers).status_code == 204


@pytest.mark.parametrize("method", WRITE_METHODS + ["GET"])
def test_an_allowed_origin_never_replaces_authentication(api: tuple[TestClient, str], method: str) -> None:
    client, _ = api

    for token in (None, "not-a-jwt"):
        response = _call(client, method, token, Origin=ORIGIN)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


def test_every_non_safe_method_counts_as_a_write() -> None:
    assert SAFE_METHODS == {"GET", "HEAD", "OPTIONS", "TRACE"}
    assert not set(WRITE_METHODS) & SAFE_METHODS


def test_the_existing_dependency_still_checks_every_method(settings: Settings) -> None:
    """require_allowed_origin (login, register, refresh, logout) is unchanged: no method exemption."""
    app = FastAPI()
    register_error_handlers(app)
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/strict", dependencies=[Depends(require_allowed_origin)])
    def strict() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/strict").status_code == 403
        assert client.get("/strict", headers={"Origin": ORIGIN}).status_code == 200


def test_rejected_write_logs_no_token(api: tuple[TestClient, str], caplog: pytest.LogCaptureFixture) -> None:
    client, token = api

    with caplog.at_level("INFO"):
        _call(client, "POST", token, Origin="https://evil.example")

    assert token not in caplog.text
    assert any(r.getMessage() == "Request origin rejected" for r in caplog.records)
