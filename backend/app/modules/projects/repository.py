"""SQL for the projects module's tables. Callers own the transaction: nothing here commits."""

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.modules.projects.models import ApiKey, Project, ProjectMember, ProjectRole


def get_membership(session: Session, *, project_id: uuid.UUID, user_id: uuid.UUID) -> ProjectMember | None:
    """The user's membership in the project, reloaded from the database."""
    statement = (
        select(ProjectMember)
        .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    return session.scalars(statement).one_or_none()


def lock_project(session: Session, project_id: uuid.UUID) -> Project | None:
    """``SELECT ... FOR UPDATE`` on the project row, reloading it.

    Every change to a project or its memberships holds this lock, so concurrent changes to one
    project are serialized and each re-checks the caller's role against committed state.
    """
    statement = (
        select(Project).where(Project.id == project_id).with_for_update().execution_options(populate_existing=True)
    )
    return session.scalars(statement).one_or_none()


def get_project(session: Session, project_id: uuid.UUID) -> Project | None:
    return session.get(Project, project_id)


def insert_project(session: Session, *, name: str, description: str | None) -> Project:
    project = Project(id=uuid.uuid4(), name=name, description=description)
    session.add(project)
    session.flush()
    return project


def insert_member(session: Session, *, project_id: uuid.UUID, user_id: uuid.UUID, role: ProjectRole) -> ProjectMember:
    member = ProjectMember(project_id=project_id, user_id=user_id, role=role.value)
    session.add(member)
    session.flush()
    return member


def list_user_projects(
    session: Session, *, user_id: uuid.UUID, limit: int, offset: int
) -> list[tuple[Project, str]]:
    """The projects the user belongs to, with the user's role, ordered by name then id."""
    statement = (
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
        .order_by(Project.name, Project.id)
        .limit(limit)
        .offset(offset)
    )
    return [(project, role) for project, role in session.execute(statement).tuples()]


def list_members(session: Session, project_id: uuid.UUID) -> list[ProjectMember]:
    """Every membership of the project, in one query (reloaded from the database)."""
    statement = (
        select(ProjectMember)
        .where(ProjectMember.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    return list(session.scalars(statement))


def set_member_role(session: Session, *, project_id: uuid.UUID, user_id: uuid.UUID, role: ProjectRole) -> int:
    """Change one membership's role with its own UPDATE statement, executed immediately.

    Used where statement order matters: the Owner index is checked per statement and cannot be
    deferred, so an ownership transfer must demote before it promotes.
    """
    return session.execute(
        update(ProjectMember)
        .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
        .values(role=role.value, updated_at=func.now())
        .execution_options(synchronize_session=False)
    ).rowcount


def delete_member(session: Session, *, project_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return session.execute(
        delete(ProjectMember)
        .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
        .execution_options(synchronize_session=False)
    ).rowcount


def delete_project(session: Session, project_id: uuid.UUID) -> int:
    """Delete the project; memberships and API keys go with it (ON DELETE CASCADE)."""
    return session.execute(
        delete(Project).where(Project.id == project_id).execution_options(synchronize_session=False)
    ).rowcount


# --- API keys ----------------------------------------------------------------------------------


def count_active_api_keys(session: Session, project_id: uuid.UUID) -> int:
    statement = select(func.count()).where(ApiKey.project_id == project_id, ApiKey.revoked_at.is_(None))
    return session.scalar(statement) or 0


def insert_api_key(
    session: Session, *, project_id: uuid.UUID, name: str, prefix: str, key_hash: str, created_by: uuid.UUID
) -> ApiKey:
    api_key = ApiKey(
        id=uuid.uuid4(), project_id=project_id, name=name, prefix=prefix, key_hash=key_hash, created_by=created_by
    )
    session.add(api_key)
    session.flush()
    session.refresh(api_key)  # load the database-set created_at
    return api_key


def list_api_keys(session: Session, *, project_id: uuid.UUID, limit: int, offset: int) -> list[ApiKey]:
    """The project's keys, revoked ones included, newest first."""
    statement = (
        select(ApiKey)
        .where(ApiKey.project_id == project_id)
        .order_by(ApiKey.created_at.desc(), ApiKey.id)
        .limit(limit)
        .offset(offset)
        .execution_options(populate_existing=True)
    )
    return list(session.scalars(statement))


def get_api_key(session: Session, *, project_id: uuid.UUID, api_key_id: uuid.UUID) -> ApiKey | None:
    """A key of *this* project, reloaded; a key of another project is never returned."""
    statement = (
        select(ApiKey)
        .where(ApiKey.project_id == project_id, ApiKey.id == api_key_id)
        .execution_options(populate_existing=True)
    )
    return session.scalars(statement).one_or_none()


def revoke_api_key(session: Session, *, api_key: ApiKey, revoked_by: uuid.UUID) -> None:
    api_key.revoked_at = func.now()
    api_key.revoked_by = revoked_by
    session.flush()
    session.refresh(api_key)  # load the database-set revoked_at


def find_active_api_key(session: Session, key_hash: str) -> ApiKey | None:
    """The unrevoked key with this digest (unique index on ``key_hash``)."""
    statement = select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
    return session.scalars(statement).one_or_none()
