import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_session_factory
from app.core.security import generate_opaque_token, hash_opaque_token
from app.modules.auth.models import User
from app.modules.projects.models import ApiKey, Project, ProjectMember, ProjectRole

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaGhhc2hoYXNoaGFzaGhhc2g"


@pytest.fixture
def make_user() -> Callable[[str], User]:
    def factory(email: str) -> User:
        with get_session_factory()() as session:
            user = User(email=email, password_hash=PASSWORD_HASH)
            session.add(user)
            session.commit()
            return user

    return factory


def _commit(*objects: object) -> None:
    with get_session_factory()() as session:
        session.add_all(objects)
        session.commit()


def _key(project: Project, **overrides: Any) -> ApiKey:
    values: dict[str, Any] = {
        "project": project,
        "name": "Ingestion",
        "prefix": "tiq_ab12",
        "key_hash": hash_opaque_token(generate_opaque_token()),
    }
    values.update(overrides)
    return ApiKey(**values)


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _sql(statement: str, **params: object) -> None:
    with get_session_factory()() as session:
        session.execute(text(statement), params)
        session.commit()


# --- projects ---------------------------------------------------------------------------------


def test_project_defaults_and_python_generated_uuid() -> None:
    with get_session_factory()() as session:
        project = Project(name="Search assistant")
        session.add(project)
        session.flush()
        assert isinstance(project.id, uuid.UUID)
        project_id = project.id
        session.commit()

    with get_session_factory()() as session:
        stored = session.get_one(Project, project_id)
        assert stored.description is None
        assert stored.retention_overrides is None
        assert stored.created_at.tzinfo is not None and stored.updated_at.tzinfo is not None
        assert stored.members == [] and stored.api_keys == []


def test_project_updated_at_changes_on_update() -> None:
    with get_session_factory()() as session:
        project = Project(name="Project")
        session.add(project)
        session.commit()
        session.execute(
            text("UPDATE projects SET updated_at = now() - interval '1 hour' WHERE id = :id"), {"id": project.id}
        )
        session.commit()
        session.refresh(project)
        before = project.updated_at

        project.description = "changed"
        session.commit()
        session.refresh(project)

        assert project.updated_at > before


@pytest.mark.parametrize("name", ["Search assistant", "a", "RAG  pipeline (prod)", "Ünïcode ✓", "x" * 100])
def test_valid_project_names(name: str) -> None:
    _commit(Project(name=name))


@pytest.mark.parametrize("name", ["", " ", " leading", "trailing ", "\tTab", "newline\n", "\r\n", " both "])
def test_project_name_must_be_trimmed_and_non_empty(name: str) -> None:
    with pytest.raises(IntegrityError, match="ck_projects_name_trimmed"):
        _commit(Project(name=name))


def test_database_name_check_covers_ascii_whitespace_only() -> None:
    """PostgreSQL's [[:space:]] does not include Unicode spaces such as U+00A0, which Python's
    str.strip() removes. The service trims names in Python first; the check is a backstop."""
    _commit(Project(name=" padded "))
    assert " padded ".strip() == "padded"


def test_project_name_and_description_lengths_are_bounded() -> None:
    with pytest.raises(DataError, match="value too long"):
        _commit(Project(name="x" * 101))
    _commit(Project(name="ok", description="d" * 1000))
    with pytest.raises(DataError, match="value too long"):
        _commit(Project(name="ok", description="d" * 1001))


@pytest.mark.parametrize("overrides", [None, {}, {"traces_days": 10, "prediction_logs_days": 7}])
def test_retention_overrides_accepts_null_and_objects(overrides: dict[str, int] | None) -> None:
    with get_session_factory()() as session:
        project = Project(name="Project", retention_overrides=overrides)
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.retention_overrides == overrides
        stored_null = session.scalar(
            text("SELECT retention_overrides IS NULL FROM projects WHERE id = :id"), {"id": project.id}
        )
        assert stored_null is (overrides is None)


def test_clearing_retention_overrides_stores_sql_null() -> None:
    """Python None must become SQL NULL ("use the defaults"), never a JSON null."""
    with get_session_factory()() as session:
        project = Project(name="Project", retention_overrides={"traces_days": 10})
        session.add(project)
        session.commit()
        project.retention_overrides = None
        session.commit()
        assert session.scalar(text("SELECT retention_overrides IS NULL FROM projects")) is True


@pytest.mark.parametrize("value", ["[]", "[1, 2]", '"text"', "5", "true", "null"])
def test_retention_overrides_must_be_an_object(value: str) -> None:
    """A JSON null inside the column (not SQL NULL) is not an object either."""
    with pytest.raises(IntegrityError, match="ck_projects_retention_overrides_object"):
        _sql(
            "INSERT INTO projects (id, name, retention_overrides) VALUES (:id, 'p', CAST(:value AS jsonb))",
            id=uuid.uuid4(),
            value=value,
        )


# --- project_members --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ProjectRole))
def test_every_role_is_accepted(make_user: Callable[[str], User], role: ProjectRole) -> None:
    user = make_user("user@example.com")
    project = Project(name="Project")

    _commit(ProjectMember(project=project, user_id=user.id, role=role.value))


@pytest.mark.parametrize("role", ["Owner", "OWNER", "viewer", "", "owner "])
def test_unknown_roles_are_rejected(make_user: Callable[[str], User], role: str) -> None:
    user = make_user("user@example.com")

    with pytest.raises(IntegrityError, match="ck_project_members_role"):
        _commit(ProjectMember(project=Project(name="Project"), user_id=user.id, role=role))


def test_roles_are_exactly_owner_admin_member() -> None:
    assert [role.value for role in ProjectRole] == ["owner", "admin", "member"]


def test_member_defaults_and_relationship(make_user: Callable[[str], User]) -> None:
    user = make_user("user@example.com")
    with get_session_factory()() as session:
        project = Project(name="Project", members=[ProjectMember(user_id=user.id, role="owner")])
        session.add(project)
        session.commit()
        project_id = project.id

    with get_session_factory()() as session:
        [member] = session.get_one(Project, project_id).members
        assert (member.user_id, member.role) == (user.id, "owner")
        assert member.created_at.tzinfo is not None and member.updated_at.tzinfo is not None
        assert member.project.id == project_id


def test_a_user_is_a_member_of_a_project_at_most_once(make_user: Callable[[str], User]) -> None:
    user = make_user("user@example.com")
    with get_session_factory()() as session:
        project = Project(name="Project")
        session.add(project)
        session.commit()
        project_id = project.id

    _commit(ProjectMember(project_id=project_id, user_id=user.id, role="member"))
    with pytest.raises(IntegrityError, match="pk_project_members"):
        _commit(ProjectMember(project_id=project_id, user_id=user.id, role="admin"))


def test_at_most_one_owner_per_project(make_user: Callable[[str], User]) -> None:
    alice, bob = make_user("alice@example.com"), make_user("bob@example.com")
    with get_session_factory()() as session:
        project = Project(name="Project", members=[ProjectMember(user_id=alice.id, role="owner")])
        session.add(project)
        session.commit()
        project_id = project.id

    with pytest.raises(IntegrityError, match="uq_project_members_owner"):
        _commit(ProjectMember(project_id=project_id, user_id=bob.id, role="owner"))


def test_each_project_has_its_own_owner_and_many_admins_and_members(make_user: Callable[[str], User]) -> None:
    users = [make_user(f"user{i}@example.com") for i in range(4)]

    _commit(
        Project(
            name="One",
            members=[
                ProjectMember(user_id=users[0].id, role="owner"),
                ProjectMember(user_id=users[1].id, role="admin"),
                ProjectMember(user_id=users[2].id, role="admin"),
                ProjectMember(user_id=users[3].id, role="member"),
            ],
        ),
        Project(name="Two", members=[ProjectMember(user_id=users[1].id, role="owner")]),
    )

    with get_session_factory()() as session:
        owners = session.scalars(select(ProjectMember).where(ProjectMember.role == "owner")).all()
        assert len(owners) == 2


def _project_with_owner_and_admin(make_user: Callable[[str], User]) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    owner, admin = make_user("owner@example.com"), make_user("admin@example.com")
    with get_session_factory()() as session:
        project = Project(
            name="Project",
            members=[ProjectMember(user_id=owner.id, role="owner"), ProjectMember(user_id=admin.id, role="admin")],
        )
        session.add(project)
        session.commit()
        return project.id, owner.id, admin.id


def _set_role(session: Session, project_id: uuid.UUID, user_id: uuid.UUID, role: str) -> None:
    session.execute(
        update(ProjectMember)
        .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
        .values(role=role)
    )


def test_ownership_transfer_demotes_first_within_one_transaction(make_user: Callable[[str], User]) -> None:
    """The unique index is not deferrable: the old Owner is demoted before the new one is promoted."""
    project_id, owner_id, admin_id = _project_with_owner_and_admin(make_user)

    with get_session_factory()() as session:
        _set_role(session, project_id, owner_id, "admin")
        _set_role(session, project_id, admin_id, "owner")
        session.commit()

    with get_session_factory()() as session:
        roles = dict(session.execute(select(ProjectMember.user_id, ProjectMember.role)).tuples().all())
        assert roles == {owner_id: "admin", admin_id: "owner"}


def test_promoting_before_demoting_violates_the_owner_index(make_user: Callable[[str], User]) -> None:
    project_id, owner_id, admin_id = _project_with_owner_and_admin(make_user)

    with get_session_factory()() as session, pytest.raises(IntegrityError, match="uq_project_members_owner"):
        _set_role(session, project_id, admin_id, "owner")

    with get_session_factory()() as session:
        roles = dict(session.execute(select(ProjectMember.user_id, ProjectMember.role)).tuples().all())
        assert roles == {owner_id: "owner", admin_id: "admin"}


def test_a_project_can_exist_without_an_owner_at_the_database_level(make_user: Callable[[str], User]) -> None:
    """"At least one Owner" is the service's job (under a project-row lock), not a constraint."""
    project_id, owner_id, _ = _project_with_owner_and_admin(make_user)

    _sql("DELETE FROM project_members WHERE project_id = :p AND user_id = :u", p=project_id, u=owner_id)


def test_membership_requires_existing_project_and_user(make_user: Callable[[str], User]) -> None:
    user = make_user("user@example.com")

    with pytest.raises(IntegrityError, match="fk_project_members_project_id_projects"):
        _commit(ProjectMember(project_id=uuid.uuid4(), user_id=user.id, role="member"))
    with pytest.raises(IntegrityError, match="fk_project_members_user_id_users"):
        _commit(ProjectMember(project=Project(name="Project"), user_id=uuid.uuid4(), role="member"))


def test_deleting_a_member_user_is_restricted(make_user: Callable[[str], User]) -> None:
    project_id, owner_id, _ = _project_with_owner_and_admin(make_user)

    with pytest.raises(IntegrityError, match="fk_project_members_user_id_users"):
        _sql("DELETE FROM users WHERE id = :id", id=owner_id)

    # Once the membership is gone, the user can be deleted.
    _sql("DELETE FROM project_members WHERE project_id = :p AND user_id = :u", p=project_id, u=owner_id)
    _sql("DELETE FROM users WHERE id = :id", id=owner_id)


# --- api_keys ---------------------------------------------------------------------------------


def test_api_key_defaults_and_relationship(make_user: Callable[[str], User]) -> None:
    user = make_user("user@example.com")
    with get_session_factory()() as session:
        project = Project(name="Project")
        key = _key(project, created_by=user.id)
        session.add(key)
        session.commit()
        key_id, project_id = key.id, project.id

    with get_session_factory()() as session:
        stored = session.get_one(ApiKey, key_id)
        assert isinstance(stored.id, uuid.UUID)
        assert stored.project_id == project_id and stored.project.api_keys == [stored]
        assert stored.created_by == user.id
        assert stored.created_at.tzinfo is not None
        assert (stored.last_used_at, stored.revoked_at, stored.revoked_by) == (None, None, None)


def test_api_key_hash_is_unique_across_projects() -> None:
    token_hash = hash_opaque_token(generate_opaque_token())

    with pytest.raises(IntegrityError, match="uq_api_keys_key_hash"):
        _commit(_key(Project(name="One"), key_hash=token_hash), _key(Project(name="Two"), key_hash=token_hash))


@pytest.mark.parametrize("key_hash", ["", "a" * 63, "A" * 64, "g" * 64, "a" * 63 + " "])
def test_api_key_hash_format(key_hash: str) -> None:
    with pytest.raises(IntegrityError, match="ck_api_keys_key_hash_format"):
        _commit(_key(Project(name="Project"), key_hash=key_hash))


def test_api_key_hash_longer_than_a_digest_is_rejected() -> None:
    with pytest.raises(DataError, match="value too long"):
        _commit(_key(Project(name="Project"), key_hash="a" * 65))


def test_revoked_by_requires_revoked_at(make_user: Callable[[str], User]) -> None:
    user = make_user("user@example.com")

    with pytest.raises(IntegrityError, match="ck_api_keys_revoked_by_requires_revoked_at"):
        _commit(_key(Project(name="Project"), revoked_by=user.id))


def test_revocation_with_and_without_a_recorded_user(make_user: Callable[[str], User]) -> None:
    user = make_user("user@example.com")

    _commit(
        _key(Project(name="One"), revoked_at=NOW, revoked_by=user.id),
        _key(Project(name="Two"), revoked_at=NOW),  # e.g. revoked by the system, or the user since deleted
    )


@pytest.mark.parametrize("name", ["", " ", " leading", "trailing\t"])
def test_api_key_name_must_be_trimmed_and_non_empty(name: str) -> None:
    with pytest.raises(IntegrityError, match="ck_api_keys_name_trimmed"):
        _commit(_key(Project(name="Project"), name=name))


def test_api_key_prefix_must_not_be_empty() -> None:
    with pytest.raises(IntegrityError, match="ck_api_keys_prefix_not_empty"):
        _commit(_key(Project(name="Project"), prefix=""))
    with pytest.raises(DataError, match="value too long"):
        _commit(_key(Project(name="Project"), prefix="x" * 17))


def test_api_key_requires_existing_project_and_creator(make_user: Callable[[str], User]) -> None:
    with pytest.raises(IntegrityError, match="fk_api_keys_project_id_projects"):
        _sql(
            "INSERT INTO api_keys (id, project_id, name, prefix, key_hash) VALUES (:id, :p, 'k', 'tiq_', :h)",
            id=uuid.uuid4(),
            p=uuid.uuid4(),
            h="a" * 64,
        )
    with pytest.raises(IntegrityError, match="fk_api_keys_created_by_users"):
        _commit(_key(Project(name="Project"), created_by=uuid.uuid4()))


def test_deleting_a_user_clears_api_key_attribution(make_user: Callable[[str], User]) -> None:
    creator, revoker = make_user("creator@example.com"), make_user("revoker@example.com")
    with get_session_factory()() as session:
        key = _key(Project(name="Project"), created_by=creator.id, revoked_at=NOW, revoked_by=revoker.id)
        session.add(key)
        session.commit()
        key_id = key.id

    _sql("DELETE FROM users WHERE id IN (:a, :b)", a=creator.id, b=revoker.id)

    with get_session_factory()() as session:
        stored = session.get_one(ApiKey, key_id)
        assert (stored.created_by, stored.revoked_by) == (None, None)
        assert stored.revoked_at == NOW  # the key stays revoked


# --- Cascades ---------------------------------------------------------------------------------


def _project_with_children(make_user: Callable[[str], User]) -> uuid.UUID:
    owner, member = make_user("owner@example.com"), make_user("member@example.com")
    with get_session_factory()() as session:
        project = Project(
            name="Project",
            members=[ProjectMember(user_id=owner.id, role="owner"), ProjectMember(user_id=member.id, role="member")],
        )
        session.add_all([project, _key(project), _key(project, revoked_at=NOW)])
        session.add(Project(name="Other", members=[ProjectMember(user_id=owner.id, role="owner")]))
        session.commit()
        return project.id


def test_deleting_a_project_cascades_in_the_database(make_user: Callable[[str], User]) -> None:
    project_id = _project_with_children(make_user)

    _sql("DELETE FROM projects WHERE id = :id", id=project_id)

    with get_session_factory()() as session:
        assert _count(session, Project) == 1
        assert session.scalars(select(ProjectMember.project_id)).all() != [] and all(
            pid != project_id for pid in session.scalars(select(ProjectMember.project_id))
        )
        assert _count(session, ApiKey) == 0
        assert _count(session, User) == 2  # users are never deleted with a project


def test_deleting_a_project_through_the_orm_removes_its_children(make_user: Callable[[str], User]) -> None:
    project_id = _project_with_children(make_user)

    with get_session_factory()() as session:
        session.delete(session.get_one(Project, project_id))
        session.commit()

        assert _count(session, ApiKey) == 0
        assert _count(session, ProjectMember) == 1  # the other project's Owner
