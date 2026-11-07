import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_session_factory
from app.core.security import generate_opaque_token, hash_opaque_token
from app.modules.auth.models import RefreshToken, RevocationReason, User

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

# A syntactically valid Argon2id hash; the models never verify it.
PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaGhhc2hoYXNoaGFzaGhhc2g"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _user(email: str = "user@example.com", **overrides: Any) -> User:
    return User(email=email, password_hash=PASSWORD_HASH, **overrides)


def _token(user: User, family_id: uuid.UUID | None = None, **overrides: Any) -> RefreshToken:
    values: dict[str, Any] = {
        "user": user,
        "family_id": family_id or uuid.uuid4(),
        "token_hash": hash_opaque_token(generate_opaque_token()),
        "expires_at": NOW + timedelta(days=7),
        "absolute_expires_at": NOW + timedelta(days=30),
    }
    values.update(overrides)
    return RefreshToken(**values)


def _commit(*objects: object) -> None:
    with get_session_factory()() as session:
        session.add_all(objects)
        session.commit()


def _count(session: Session, model: type[User] | type[RefreshToken]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# --- Users ------------------------------------------------------------------------------------


def test_user_defaults_and_python_generated_uuid() -> None:
    with get_session_factory()() as session:
        user = _user()
        session.add(user)
        session.flush()
        assert isinstance(user.id, uuid.UUID)
        user_id = user.id
        session.commit()

    with get_session_factory()() as session:
        stored = session.get_one(User, user_id)
        assert stored.email == "user@example.com"
        assert stored.password_hash == PASSWORD_HASH
        assert stored.is_active is True
        assert stored.last_login_at is None
        assert stored.created_at.tzinfo is not None and stored.updated_at.tzinfo is not None
        assert stored.refresh_tokens == []


def test_user_updated_at_changes_on_update() -> None:
    with get_session_factory()() as session:
        user = _user()
        session.add(user)
        session.commit()
        # Move the stored value into the past so the update is observable despite clock resolution.
        session.execute(text("UPDATE users SET updated_at = now() - interval '1 hour' WHERE id = :id"), {"id": user.id})
        session.commit()
        session.refresh(user)
        before = user.updated_at

        user.last_login_at = NOW
        session.commit()
        session.refresh(user)

        assert user.updated_at > before
        assert user.last_login_at == NOW


def test_user_email_is_unique() -> None:
    _commit(_user("user@example.com"))

    with pytest.raises(IntegrityError, match="uq_users_email"):
        _commit(_user("user@example.com"))


@pytest.mark.parametrize(
    "email",
    [
        "User@example.com",
        "user@EXAMPLE.com",
        " user@example.com",
        "user@example.com ",
        "user@example.com\n",
        "\tuser@example.com",
        "us er@example.com",
    ],
)
def test_user_email_must_be_normalized(email: str) -> None:
    with pytest.raises(IntegrityError, match="ck_users_email_normalized"):
        _commit(_user(email))


@pytest.mark.parametrize("email", ["", "userexample.com", "@example.com"])
def test_user_email_must_contain_local_part_and_at(email: str) -> None:
    with pytest.raises(IntegrityError, match="ck_users_email_has_at"):
        _commit(_user(email))


def test_user_email_length_is_bounded() -> None:
    with pytest.raises(DataError, match="value too long"):
        _commit(_user("a" * 64 + "@" + "b" * 256))


def test_unicode_email_lower_cased_in_python_is_accepted() -> None:
    # Python's lower() output must satisfy the database's lower(): the service normalizes in Python.
    emails = [email.lower() for email in ("ÜNÏCÖDE@example.com", "İSTANBUL@example.com", "STRASSE@exämple.com")]

    _commit(*(_user(email) for email in emails))

    with get_session_factory()() as session:
        assert set(session.scalars(select(User.email))) == set(emails)


def test_user_password_hash_must_not_be_empty() -> None:
    with pytest.raises(IntegrityError, match="ck_users_password_hash_not_empty"):
        _commit(User(email="user@example.com", password_hash=""))


def test_inactive_user() -> None:
    _commit(_user(is_active=False))

    with get_session_factory()() as session:
        assert session.scalars(select(User.is_active)).one() is False


# --- Refresh tokens ---------------------------------------------------------------------------


def test_refresh_token_defaults_and_relationship() -> None:
    family_id = uuid.uuid4()
    with get_session_factory()() as session:
        user = _user()
        token = _token(user, family_id)
        session.add(token)
        session.commit()
        token_id, user_id = token.id, user.id

    with get_session_factory()() as session:
        stored = session.get_one(RefreshToken, token_id)
        assert stored.user_id == user_id
        assert stored.user.email == "user@example.com"
        assert stored.family_id == family_id
        assert len(stored.token_hash) == 64
        assert stored.revoked_at is None and stored.revocation_reason is None
        assert stored.replaced_by_id is None and stored.replaced_by is None
        assert stored.created_at.tzinfo is not None
        assert stored.expires_at == NOW + timedelta(days=7)
        assert stored.absolute_expires_at == NOW + timedelta(days=30)
        assert [t.id for t in stored.user.refresh_tokens] == [token_id]


def test_rotation_chain_within_a_family() -> None:
    family_id = uuid.uuid4()
    with get_session_factory()() as session:
        user = _user()
        old = _token(user, family_id)
        session.add(old)
        session.flush()

        new = _token(user, family_id)
        session.add(new)
        session.flush()
        old.revoked_at = NOW
        old.revocation_reason = RevocationReason.ROTATED
        old.replaced_by = new
        session.commit()
        old_id, new_id = old.id, new.id

    with get_session_factory()() as session:
        stored = session.get_one(RefreshToken, old_id)
        assert stored.replaced_by_id == new_id
        assert stored.replaced_by is not None and stored.replaced_by.id == new_id
        assert stored.revocation_reason == "rotated"
        family = session.scalars(select(RefreshToken).where(RefreshToken.family_id == family_id)).all()
        assert {t.id for t in family} == {old_id, new_id}


def test_refresh_token_hash_is_unique() -> None:
    user = _user()
    token_hash = hash_opaque_token(generate_opaque_token())

    with pytest.raises(IntegrityError, match="uq_refresh_tokens_token_hash"):
        _commit(_token(user, token_hash=token_hash), _token(user, token_hash=token_hash))


@pytest.mark.parametrize(
    "token_hash",
    [
        "",
        "a" * 63,
        "A" * 64,  # upper-case hex
        "g" * 64,  # not hex
        "a" * 63 + " ",
    ],
)
def test_refresh_token_hash_format(token_hash: str) -> None:
    with pytest.raises(IntegrityError, match="ck_refresh_tokens_token_hash_format"):
        _commit(_token(_user(), token_hash=token_hash))


def test_refresh_token_hash_longer_than_digest_is_rejected() -> None:
    with pytest.raises(DataError, match="value too long"):
        _commit(_token(_user(), token_hash="a" * 65))


def test_rolling_expiry_cannot_exceed_absolute_expiry() -> None:
    with pytest.raises(IntegrityError, match="ck_refresh_tokens_expiry_within_absolute"):
        _commit(_token(_user(), expires_at=NOW + timedelta(days=31), absolute_expires_at=NOW + timedelta(days=30)))


def test_rolling_expiry_may_equal_absolute_expiry() -> None:
    # The last rotation before the absolute deadline is capped to it.
    _commit(_token(_user(), expires_at=NOW, absolute_expires_at=NOW))


@pytest.mark.parametrize("reason", list(RevocationReason))
def test_every_revocation_reason_is_accepted(reason: RevocationReason) -> None:
    _commit(_token(_user(), revoked_at=NOW, revocation_reason=reason))


def test_revocation_reasons_are_exactly_the_approved_values() -> None:
    assert [reason.value for reason in RevocationReason] == ["rotated", "reuse_detected", "logout", "revoked"]


def test_revoked_reason_is_accepted() -> None:
    _commit(_token(_user(), revoked_at=NOW, revocation_reason="revoked"))

    with get_session_factory()() as session:
        stored = session.scalars(select(RefreshToken)).one()
        assert stored.revocation_reason == RevocationReason.REVOKED
        assert stored.revoked_at == NOW


@pytest.mark.parametrize("reason", ["expired", "REVOKED", "revoked ", "", "password_change"])
def test_unknown_revocation_reason_is_rejected(reason: str) -> None:
    with pytest.raises(IntegrityError, match="ck_refresh_tokens_revocation_reason"):
        _commit(_token(_user(), revoked_at=NOW, revocation_reason=reason))


@pytest.mark.parametrize(
    ("revoked_at", "reason"),
    [(NOW, None), (None, RevocationReason.LOGOUT)],
)
def test_revoked_at_and_reason_are_set_together(revoked_at: datetime | None, reason: str | None) -> None:
    with pytest.raises(IntegrityError, match="ck_refresh_tokens_revoked_iff_reason"):
        _commit(_token(_user(), revoked_at=revoked_at, revocation_reason=reason))


@pytest.mark.parametrize("reason", [RevocationReason.LOGOUT, RevocationReason.REUSE_DETECTED])
def test_only_rotated_tokens_have_a_replacement(reason: RevocationReason) -> None:
    with get_session_factory()() as session:
        user = _user()
        replacement = _token(user)
        session.add(replacement)
        session.flush()
        session.add(_token(user, revoked_at=NOW, revocation_reason=reason, replaced_by_id=replacement.id))
        with pytest.raises(IntegrityError, match="ck_refresh_tokens_replaced_only_when_rotated"):
            session.flush()


def test_token_cannot_replace_itself() -> None:
    token_id = uuid.uuid4()
    token = _token(_user(), id=token_id, revoked_at=NOW, revocation_reason="rotated", replaced_by_id=token_id)

    with pytest.raises(IntegrityError, match="ck_refresh_tokens_not_replaced_by_self"):
        _commit(token)


def test_token_is_replaced_at_most_once() -> None:
    with get_session_factory()() as session:
        user = _user()
        replacement = _token(user)
        session.add(replacement)
        session.flush()
        rotated = {"revoked_at": NOW, "revocation_reason": "rotated", "replaced_by_id": replacement.id}
        session.add_all([_token(user, **rotated), _token(user, **rotated)])
        with pytest.raises(IntegrityError, match="uq_refresh_tokens_replaced_by_id"):
            session.flush()


def test_replacement_must_exist() -> None:
    token = _token(_user(), revoked_at=NOW, revocation_reason="rotated", replaced_by_id=uuid.uuid4())

    with pytest.raises(IntegrityError, match="fk_refresh_tokens_replaced_by_id_refresh_tokens"):
        _commit(token)


def test_refresh_token_requires_an_existing_user() -> None:
    with get_session_factory()() as session, pytest.raises(IntegrityError, match="fk_refresh_tokens_user_id_users"):
        session.execute(
            text(
                "INSERT INTO refresh_tokens (id, user_id, family_id, token_hash, expires_at, absolute_expires_at)"
                " VALUES (:id, :user_id, :family_id, :token_hash, now(), now())"
            ),
            {
                "id": uuid.uuid4(),
                "user_id": uuid.uuid4(),
                "family_id": uuid.uuid4(),
                "token_hash": "a" * 64,
            },
        )


def test_deleting_a_replacement_clears_the_link() -> None:
    with get_session_factory()() as session:
        user = _user()
        new = _token(user)
        session.add(new)
        session.flush()
        old = _token(user, revoked_at=NOW, revocation_reason="rotated", replaced_by_id=new.id)
        session.add(old)
        session.commit()
        old_id, new_id = old.id, new.id

    with get_session_factory()() as session:
        session.execute(text("DELETE FROM refresh_tokens WHERE id = :id"), {"id": new_id})
        session.commit()

    with get_session_factory()() as session:
        stored = session.get_one(RefreshToken, old_id)
        assert stored.replaced_by_id is None
        assert stored.revocation_reason == "rotated"


def test_deleting_a_user_cascades_to_refresh_tokens_in_the_database() -> None:
    other = _user("other@example.com")
    with get_session_factory()() as session:
        user = _user()
        family_id = uuid.uuid4()
        new = _token(user, family_id)
        session.add_all([new, _token(other)])
        session.flush()
        session.add(_token(user, family_id, revoked_at=NOW, revocation_reason="rotated", replaced_by_id=new.id))
        session.commit()
        user_id = user.id

    with get_session_factory()() as session:
        # Raw SQL: the database itself enforces the cascade, independent of the ORM.
        session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        session.commit()

    with get_session_factory()() as session:
        assert _count(session, User) == 1
        remaining = session.scalars(select(RefreshToken)).all()
        assert [token.user_id for token in remaining] == [other.id]


def test_deleting_a_user_through_the_orm_removes_its_tokens() -> None:
    with get_session_factory()() as session:
        user = _user()
        session.add_all([_token(user), _token(user)])
        session.commit()

        session.delete(user)
        session.commit()

        assert _count(session, User) == 0
        assert _count(session, RefreshToken) == 0


def test_removing_a_token_from_the_relationship_deletes_it() -> None:
    with get_session_factory()() as session:
        user = _user()
        session.add_all([_token(user), _token(user)])
        session.commit()

        user.refresh_tokens.pop()
        session.commit()

        assert _count(session, RefreshToken) == 1


def test_tokens_are_found_by_hash_family_and_user() -> None:
    family_id = uuid.uuid4()
    plaintext = generate_opaque_token()
    with get_session_factory()() as session:
        user = _user()
        session.add_all([_token(user, family_id, token_hash=hash_opaque_token(plaintext)), _token(user, family_id)])
        session.add(_token(_user("other@example.com")))
        session.commit()
        user_id = user.id

    with get_session_factory()() as session:
        by_hash = session.scalars(select(RefreshToken).where(RefreshToken.token_hash == hash_opaque_token(plaintext)))
        assert by_hash.one().family_id == family_id
        assert len(session.scalars(select(RefreshToken).where(RefreshToken.family_id == family_id)).all()) == 2
        assert len(session.scalars(select(RefreshToken).where(RefreshToken.user_id == user_id)).all()) == 2
