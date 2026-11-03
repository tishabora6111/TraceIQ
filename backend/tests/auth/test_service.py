import statistics
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.database import get_session_factory
from app.core.security import decode_access_token
from app.modules.auth import repository, service
from app.modules.auth.errors import (
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidPasswordError,
    RegistrationDisabledError,
)
from app.modules.auth.models import RefreshToken, User
from app.modules.auth.schemas import normalize_email
from tests.auth.conftest import PASSWORD

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

CreateUser = Callable[..., User]


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("User@Example.COM", "user@example.com"), ("  user@example.com\t\n", "user@example.com"), ("ü@exämple.com", "ü@exämple.com")],
)
def test_normalize_email(raw: str, normalized: str) -> None:
    assert normalize_email(raw) == normalized


def test_register_user_commits_its_own_transaction(settings: Settings) -> None:
    with get_session_factory()() as session:
        user = service.register_user(session, email=" A@Example.com ", password=PASSWORD, settings=settings)

    # Visible from a fresh session: the service committed.
    with get_session_factory()() as session:
        stored = session.get_one(User, user.id)
        assert stored.email == "a@example.com"
        assert stored.is_active is True


@pytest.mark.parametrize("password", ["x" * 11, "x" * 257])
def test_register_user_enforces_the_password_policy(settings: Settings, password: str) -> None:
    with get_session_factory()() as session, pytest.raises(InvalidPasswordError) as excinfo:
        service.register_user(session, email="a@example.com", password=password, settings=settings)

    assert excinfo.value.status_code == 422
    assert password not in excinfo.value.message


def test_register_user_respects_the_registration_policy(settings: Settings) -> None:
    disabled = settings.model_copy(update={"auth_registration_enabled": False})

    with get_session_factory()() as session, pytest.raises(RegistrationDisabledError):
        service.register_user(session, email="a@example.com", password=PASSWORD, settings=disabled)


def test_concurrent_registration_of_the_same_email_conflicts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unique constraint catches a registration that passes the existence check concurrently."""
    with get_session_factory()() as session:
        service.register_user(session, email="race@example.com", password=PASSWORD, settings=settings)

    monkeypatch.setattr(repository, "get_user_by_email", lambda session, email: None)
    with get_session_factory()() as session:
        with pytest.raises(EmailAlreadyRegisteredError):
            service.register_user(session, email="RACE@example.com", password=PASSWORD, settings=settings)
        # The failed transaction was rolled back; the session is usable.
        monkeypatch.undo()
        assert repository.get_user_by_email(session, "race@example.com") is not None


def test_login_result_repr_hides_tokens(settings: Settings, create_user: CreateUser) -> None:
    create_user()

    with get_session_factory()() as session:
        result = service.login(session, email="user@example.com", password=PASSWORD, settings=settings)

    text = repr(result)
    for secret in (result.access_token, result.refresh_token, result.csrf_token):
        assert secret not in text
    assert str(result.session_id) in text


def test_login_uses_the_given_clock(settings: Settings, create_user: CreateUser) -> None:
    create_user()
    now = datetime.now(UTC).replace(microsecond=0)

    with get_session_factory()() as session:
        result = service.login(session, email="user@example.com", password=PASSWORD, settings=settings, now=now)

    assert result.access_token_expires_at == now + timedelta(minutes=15)
    assert result.refresh_token_expires_at == now + timedelta(days=7)
    claims = decode_access_token(result.access_token, settings=settings)
    assert claims.session_id == result.session_id
    with get_session_factory()() as session:
        token = session.scalars(select(RefreshToken)).one()
        assert token.absolute_expires_at == now + timedelta(days=30)
        assert session.get_one(User, result.user.id).last_login_at == now


def test_rolling_expiry_is_capped_by_the_absolute_lifetime(settings: Settings, create_user: CreateUser) -> None:
    create_user()
    capped = settings.model_copy(update={"refresh_token_absolute_ttl_seconds": settings.refresh_token_ttl_seconds})

    with get_session_factory()() as session:
        result = service.login(session, email="user@example.com", password=PASSWORD, settings=capped)

    with get_session_factory()() as session:
        token = session.scalars(select(RefreshToken)).one()
        assert token.expires_at == token.absolute_expires_at == result.refresh_token_expires_at


def test_failed_login_leaves_no_partial_session(
    settings: Settings, create_user: CreateUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If issuing tokens fails, nothing is written (no refresh token, no last_login_at)."""
    user = create_user()

    def fail(*args: object, **kwargs: object) -> str:
        raise RuntimeError("signing failed")

    monkeypatch.setattr(service, "create_csrf_token", fail)
    with get_session_factory()() as session, pytest.raises(RuntimeError):
        service.login(session, email="user@example.com", password=PASSWORD, settings=settings)

    with get_session_factory()() as session:
        assert session.scalars(select(RefreshToken)).all() == []
        assert session.get_one(User, user.id).last_login_at is None


@pytest.mark.parametrize(
    ("email", "password", "active"),
    [
        ("nobody@example.com", PASSWORD, True),
        ("user@example.com", "wrong password 123", True),
        ("user@example.com", PASSWORD, False),
    ],
)
def test_login_failures_raise_the_same_error(
    settings: Settings, create_user: CreateUser, email: str, password: str, active: bool
) -> None:
    create_user(is_active=active)

    with get_session_factory()() as session, pytest.raises(InvalidCredentialsError) as excinfo:
        service.login(session, email=email, password=password, settings=settings)

    assert (excinfo.value.status_code, excinfo.value.message) == (401, "Invalid email or password.")


def test_unknown_email_and_wrong_password_take_similar_time(settings: Settings, create_user: CreateUser) -> None:
    """Both paths run one Argon2id verification. Generous bounds: this guards against a missing
    dummy verification (a ~100x difference), not against small variations."""
    create_user()

    def median_seconds(email: str, password: str) -> float:
        samples = []
        for _ in range(5):
            with get_session_factory()() as session:
                start = time.perf_counter()
                with pytest.raises(InvalidCredentialsError):
                    service.login(session, email=email, password=password, settings=settings)
                samples.append(time.perf_counter() - start)
        return statistics.median(samples)

    unknown = median_seconds(f"{uuid.uuid4().hex}@example.com", PASSWORD)
    wrong = median_seconds("user@example.com", "wrong password 123")

    assert 0.33 < unknown / wrong < 3


# --- Rollback on every exception --------------------------------------------------------------


def _boom(*args: object, **kwargs: object) -> None:
    raise RuntimeError("injected failure")


@pytest.mark.parametrize(
    ("target", "attribute"),
    [(repository, "insert_refresh_token"), (service, "create_access_token"), (service, "create_csrf_token")],
)
def test_login_rolls_back_on_any_failure(
    settings: Settings, create_user: CreateUser, monkeypatch: pytest.MonkeyPatch, target: object, attribute: str
) -> None:
    from pwdlib import PasswordHash
    from pwdlib.hashers.argon2 import Argon2Hasher

    weak_hash = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8192, parallelism=1),)).hash(PASSWORD)
    user = create_user(password_hash=weak_hash)
    monkeypatch.setattr(target, attribute, _boom)

    with get_session_factory()() as session:
        with pytest.raises(RuntimeError, match="injected failure"):
            service.login(session, email="user@example.com", password=PASSWORD, settings=settings)
        assert not session.in_transaction()
        assert not session.dirty and not session.new

    with get_session_factory()() as session:
        stored = session.get_one(User, user.id)
        assert stored.last_login_at is None
        assert stored.password_hash == weak_hash  # the hash upgrade was not persisted
        assert session.scalars(select(RefreshToken)).all() == []


def test_login_rolls_back_on_invalid_credentials(settings: Settings, create_user: CreateUser) -> None:
    create_user()

    with get_session_factory()() as session:
        with pytest.raises(InvalidCredentialsError):
            service.login(session, email="user@example.com", password="wrong password 123", settings=settings)
        assert not session.in_transaction()


def test_login_commit_failure_rolls_back(
    settings: Settings, create_user: CreateUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = create_user()

    with get_session_factory()() as session:
        monkeypatch.setattr(session, "commit", _boom)
        with pytest.raises(RuntimeError, match="injected failure"):
            service.login(session, email="user@example.com", password=PASSWORD, settings=settings)
        assert not session.in_transaction()

    with get_session_factory()() as session:
        assert session.get_one(User, user.id).last_login_at is None
        assert session.scalars(select(RefreshToken)).all() == []


@pytest.mark.parametrize(
    ("target", "attribute"), [(service, "hash_password"), (repository, "insert_user")]
)
def test_register_rolls_back_on_any_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, target: object, attribute: str
) -> None:
    monkeypatch.setattr(target, attribute, _boom)

    with get_session_factory()() as session:
        with pytest.raises(RuntimeError, match="injected failure"):
            service.register_user(session, email="a@example.com", password=PASSWORD, settings=settings)
        assert not session.in_transaction()

    with get_session_factory()() as session:
        assert session.scalars(select(User)).all() == []


def test_register_commit_failure_rolls_back(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    with get_session_factory()() as session:
        monkeypatch.setattr(session, "commit", _boom)
        with pytest.raises(RuntimeError, match="injected failure"):
            service.register_user(session, email="a@example.com", password=PASSWORD, settings=settings)
        assert not session.in_transaction()

    with get_session_factory()() as session:
        assert session.scalars(select(User)).all() == []


@pytest.mark.parametrize("email", ["dup@example.com", "short-password@example.com"])
def test_register_domain_errors_roll_back(settings: Settings, create_user: CreateUser, email: str) -> None:
    create_user("dup@example.com")
    password = PASSWORD if email.startswith("dup") else "short"

    with get_session_factory()() as session:
        with pytest.raises((EmailAlreadyRegisteredError, InvalidPasswordError)):
            service.register_user(session, email=email, password=password, settings=settings)
        assert not session.in_transaction()
