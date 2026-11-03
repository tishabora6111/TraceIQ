from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.database import get_session_factory
from app.core.security import hash_password
from app.modules.auth.models import User

ORIGIN = "https://app.traceiq.test"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"cors_origins": [ORIGIN]})


@pytest.fixture
def make_client(settings: Settings, clean_database: None) -> Iterator[Callable[..., TestClient]]:
    """Build a client for given setting overrides. Requests carry the allowed Origin by default."""
    from app.main import create_app

    clients: list[TestClient] = []

    def factory(**overrides: Any) -> TestClient:
        client = TestClient(
            create_app(settings.model_copy(update=overrides)),
            base_url="https://testserver",
            headers={"Origin": ORIGIN},
            raise_server_exceptions=False,
        )
        clients.append(client)
        return client.__enter__()

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


@pytest.fixture
def create_user() -> Callable[..., User]:
    def factory(email: str = "user@example.com", password: str = PASSWORD, **fields: Any) -> User:
        with get_session_factory()() as session:
            user = User(email=email, password_hash=fields.pop("password_hash", None) or hash_password(password), **fields)
            session.add(user)
            session.commit()
            return user

    return factory
