import statistics
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from app.core.config import Settings
from app.core.database import get_engine, get_session_factory
from app.core.security import decode_access_token, generate_opaque_token, hash_opaque_token
from app.modules.auth import repository, service
from app.modules.auth.errors import (
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidPasswordError,
    NotAuthenticatedError,
    RegistrationDisabledError,
)
from app.modules.auth.models import RefreshToken, User
from app.modules.auth.schemas import UserRead, normalize_email
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


# --- User lookups for other modules ------------------------------------------------------------


@contextmanager
def _recorded_statements() -> Iterator[list[str]]:
    """SQL statements executed on the shared engine while the block runs."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_get_users_returns_users_by_id_in_one_query(create_user: CreateUser) -> None:
    alice, bob = create_user("alice@example.com"), create_user("bob@example.com")
    create_user("carol@example.com")

    with get_session_factory()() as session, _recorded_statements() as statements:
        users = service.get_users(session, [alice.id, bob.id, alice.id])

    assert set(users) == {alice.id, bob.id}
    assert users[alice.id].email == "alice@example.com" and users[bob.id].email == "bob@example.com"
    assert all(isinstance(user, UserRead) for user in users.values())
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 1


def test_get_users_skips_unknown_ids_and_includes_inactive_users(create_user: CreateUser) -> None:
    inactive = create_user("inactive@example.com", is_active=False)

    with get_session_factory()() as session:
        users = service.get_users(session, [inactive.id, uuid.uuid4()])

    assert list(users) == [inactive.id]
    assert users[inactive.id].is_active is False


def test_get_users_with_no_ids_makes_no_query() -> None:
    with get_session_factory()() as session, _recorded_statements() as statements:
        assert service.get_users(session, []) == {}
        assert service.get_users(session, iter(())) == {}
    assert statements == []


@pytest.mark.parametrize("email", ["user@example.com", "USER@Example.COM", "  user@example.com\t"])
def test_find_user_by_email_normalizes_the_email(create_user: CreateUser, email: str) -> None:
    user = create_user()

    with get_session_factory()() as session:
        found = service.find_user_by_email(session, email)

    assert found is not None and found.id == user.id


def test_find_user_by_email_returns_none_for_unknown_emails(create_user: CreateUser) -> None:
    create_user()

    with get_session_factory()() as session:
        assert service.find_user_by_email(session, "nobody@example.com") is None
        assert service.find_user_by_email(session, "") is None


def test_find_user_by_email_returns_inactive_users(create_user: CreateUser) -> None:
    create_user(is_active=False)

    with get_session_factory()() as session:
        found = service.find_user_by_email(session, "user@example.com")

    assert found is not None and found.is_active is False


def test_user_lookups_expose_no_secrets(create_user: CreateUser) -> None:
    user = create_user()

    with get_session_factory()() as session:
        results = [service.get_users(session, [user.id])[user.id], service.find_user_by_email(session, user.email)]

    for result in results:
        assert isinstance(result, UserRead)
        assert set(result.model_dump()) == {"id", "email", "is_active", "created_at", "last_login_at"}
        assert "$argon2" not in repr(result) and "$argon2" not in result.model_dump_json()


def test_user_lookups_are_read_only(create_user: CreateUser) -> None:
    """Neither commits nor rolls back: a caller's pending work in the same session is untouched."""
    user = create_user()

    with get_session_factory()() as session:
        pending = User(email="pending@example.com", password_hash="$argon2id$pending")
        session.add(pending)
        service.get_users(session, [user.id])
        service.find_user_by_email(session, "nobody@example.com")
        assert session.in_transaction()
        assert pending in session.new
        session.rollback()

    with get_session_factory()() as session:
        assert service.find_user_by_email(session, "pending@example.com") is None


# --- Authenticated user -----------------------------------------------------------------------


def _logged_in(settings: Settings, create_user: CreateUser, now: datetime | None = None) -> service.LoginResult:
    create_user()
    with get_session_factory()() as session:
        return service.login(session, email="user@example.com", password=PASSWORD, settings=settings, now=now)


def test_get_authenticated_user_returns_the_user(settings: Settings, create_user: CreateUser) -> None:
    result = _logged_in(settings, create_user)

    with get_session_factory()() as session:
        user = service.get_authenticated_user(session, user_id=result.user.id, session_id=result.session_id)

    assert user.id == result.user.id
    assert user.email == "user@example.com"


def test_get_authenticated_user_is_read_only(settings: Settings, create_user: CreateUser) -> None:
    """It never commits or rolls back: a caller's pending work in the same session is untouched."""
    result = _logged_in(settings, create_user)

    with get_session_factory()() as session:
        pending = User(email="pending@example.com", password_hash="$argon2id$pending")
        session.add(pending)
        service.get_authenticated_user(session, user_id=result.user.id, session_id=result.session_id)
        assert session.in_transaction()
        assert pending in session.new
        session.rollback()


def test_get_authenticated_user_failure_does_not_roll_back_the_caller(settings: Settings, create_user: CreateUser) -> None:
    create_user()

    with get_session_factory()() as session:
        pending = User(email="pending@example.com", password_hash="$argon2id$pending")
        session.add(pending)
        with pytest.raises(NotAuthenticatedError):
            service.get_authenticated_user(session, user_id=uuid.uuid4(), session_id=uuid.uuid4())
        assert pending in session.new
        session.rollback()


@pytest.mark.parametrize(
    ("offset", "authenticated"),
    [(timedelta(days=7) - timedelta(seconds=1), True), (timedelta(days=7), False), (timedelta(days=8), False)],
    ids=["before-expiry", "at-expiry", "after-expiry"],
)
def test_session_expiry_boundary(
    settings: Settings, create_user: CreateUser, offset: timedelta, authenticated: bool
) -> None:
    login_time = datetime.now(UTC).replace(microsecond=0)
    result = _logged_in(settings, create_user, now=login_time)

    with get_session_factory()() as session:
        call = lambda: service.get_authenticated_user(  # noqa: E731
            session, user_id=result.user.id, session_id=result.session_id, now=login_time + offset
        )
        if authenticated:
            assert call().id == result.user.id
        else:
            with pytest.raises(NotAuthenticatedError):
                call()


def test_one_active_token_keeps_the_family_active(settings: Settings, create_user: CreateUser) -> None:
    result = _logged_in(settings, create_user)
    now = datetime.now(UTC)

    with get_session_factory()() as session:
        assert repository.has_active_session(session, user_id=result.user.id, family_id=result.session_id, now=now)
        session.add(
            RefreshToken(
                user_id=result.user.id,
                family_id=result.session_id,
                token_hash=hash_opaque_token(generate_opaque_token()),
                expires_at=now + timedelta(days=7),
                absolute_expires_at=now + timedelta(days=30),
                revoked_at=now,
                revocation_reason="logout",
            )
        )
        session.commit()
        # A revoked sibling does not end the session while another token is still active.
        assert repository.has_active_session(session, user_id=result.user.id, family_id=result.session_id, now=now)
        for token in session.scalars(select(RefreshToken).where(RefreshToken.revoked_at.is_(None))):
            token.revoked_at, token.revocation_reason = now, "logout"
        session.commit()
        assert not repository.has_active_session(session, user_id=result.user.id, family_id=result.session_id, now=now)


@pytest.mark.parametrize("problem", ["unknown_user", "inactive_user", "inactive_session"])
def test_rejections_are_identical_and_logged_with_a_reason(
    settings: Settings, create_user: CreateUser, caplog: pytest.LogCaptureFixture, problem: str
) -> None:
    result = _logged_in(settings, create_user)
    user_id, session_id = result.user.id, result.session_id
    if problem == "unknown_user":
        user_id = uuid.uuid4()
    elif problem == "inactive_user":
        with get_session_factory()() as session:
            session.get_one(User, user_id).is_active = False
            session.commit()
    else:
        session_id = uuid.uuid4()

    with get_session_factory()() as session, caplog.at_level("INFO"), pytest.raises(NotAuthenticatedError) as excinfo:
        service.get_authenticated_user(session, user_id=user_id, session_id=session_id)

    assert (excinfo.value.status_code, excinfo.value.message) == (401, "Not authenticated.")
    [record] = [r for r in caplog.records if r.getMessage() == "Authentication rejected"]
    assert record.reason == problem
