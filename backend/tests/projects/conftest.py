from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.database import get_session_factory
from app.core.security import hash_password
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.projects.models import ProjectMember, ProjectRole

ORIGIN = "https://app.traceiq.test"
PASSWORD = "correct horse battery staple"
# One Argon2id hash for every test user, so creating users stays fast.
_PASSWORD_HASH = hash_password(PASSWORD)


@dataclass(frozen=True)
class Caller:
    """A registered user with a valid bearer token (real login, real session)."""

    user: User
    headers: dict[str, str]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"cors_origins": [ORIGIN]})


@pytest.fixture
def client(settings: Settings, clean_database: None) -> Iterator[TestClient]:
    """Requests carry the allowed Origin by default (tests remove it to check enforcement)."""
    from app.main import create_app

    with TestClient(
        create_app(settings), base_url="https://testserver", headers={"Origin": ORIGIN}, raise_server_exceptions=False
    ) as test_client:
        yield test_client


@pytest.fixture
def make_caller(settings: Settings, clean_database: None) -> Callable[[str], Caller]:
    def factory(email: str) -> Caller:
        with get_session_factory()() as session:
            user = User(email=email, password_hash=_PASSWORD_HASH)
            session.add(user)
            session.commit()
        with get_session_factory()() as session:
            result = auth_service.login(session, email=email, password=PASSWORD, settings=settings)
        return Caller(user=user, headers={"Authorization": f"Bearer {result.access_token}"})

    return factory


@pytest.fixture
def add_member() -> Callable[..., None]:
    """Insert a membership directly, bypassing the membership endpoints, for fast test setup."""

    def factory(project_id: Any, user: User, role: ProjectRole) -> None:
        with get_session_factory()() as session:
            session.add(ProjectMember(project_id=project_id, user_id=user.id, role=role.value))
            session.commit()

    return factory
