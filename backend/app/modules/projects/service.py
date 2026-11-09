"""Public interface of the projects module.

Each function that writes manages its own transaction: it commits on success and rolls back on
any exception. Read-only functions never commit or roll back. Writes lock the project row and
re-check the caller's role under that lock, so a role checked before the transaction (by the
``require_project_role`` dependency) can never be acted on after it became stale.
Log lines carry ids only, never project names, emails, or keys.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from app.core.config import MAX_RETENTION_DAYS, MIN_RETENTION_DAYS, Settings
from app.core.security import (
    API_KEY_DISPLAY_PREFIX_LENGTH,
    generate_api_key,
    hash_api_key,
    is_well_formed_api_key,
)
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import UserRead
from app.modules.projects import repository
from app.modules.projects.errors import (
    AlreadyMemberError,
    ApiKeyLimitError,
    ApiKeyNotFoundError,
    DeletionConfirmationError,
    InactiveUserError,
    InsufficientProjectRoleError,
    MemberNotFoundError,
    OwnerRoleChangeForbiddenError,
    OwnershipTransferToSelfError,
    ProjectNotFoundError,
    UserNotFoundError,
)
from app.modules.projects.models import ApiKey, Project, ProjectMember, ProjectRole
from app.modules.projects.schemas import (
    RETENTION_KEYS,
    ApiKeyCreated,
    ApiKeyPrincipal,
    ApiKeyRead,
    MemberRead,
    OwnershipTransferResult,
    ProjectAccess,
    ProjectRead,
    ProjectSummary,
    RetentionOverrides,
    RetentionPeriods,
    RetentionRead,
)

logger = logging.getLogger(__name__)

# Member < Admin < Owner.
_ROLE_RANK = {ProjectRole.MEMBER: 0, ProjectRole.ADMIN: 1, ProjectRole.OWNER: 2}
# Roles membership endpoints may grant; the Owner role only moves by ownership transfer.
_ASSIGNABLE_ROLES = frozenset({ProjectRole.ADMIN, ProjectRole.MEMBER})


def role_allows(role: ProjectRole, minimum: ProjectRole) -> bool:
    return _ROLE_RANK[role] >= _ROLE_RANK[minimum]


def get_project_access(
    session: Session, *, project_id: uuid.UUID, user_id: uuid.UUID, minimum: ProjectRole
) -> ProjectAccess:
    """The caller's verified access to the project.

    Raises ``ProjectNotFoundError`` (404) when the project does not exist or the user is not a
    member (indistinguishable), and ``InsufficientProjectRoleError`` (403) when the user's role is
    below ``minimum``. Read-only.
    """
    membership = repository.get_membership(session, project_id=project_id, user_id=user_id)
    if membership is None:
        raise ProjectNotFoundError()
    role = ProjectRole(membership.role)
    if not role_allows(role, minimum):
        raise InsufficientProjectRoleError()
    return ProjectAccess(project_id=project_id, user_id=user_id, role=role)


def _lock_with_role(session: Session, access: ProjectAccess, minimum: ProjectRole) -> tuple[Project, ProjectRole]:
    """Lock the project row, then re-check the caller's current role against committed state."""
    project = repository.lock_project(session, access.project_id)
    if project is None:
        raise ProjectNotFoundError()
    current = get_project_access(session, project_id=access.project_id, user_id=access.user_id, minimum=minimum)
    return project, current.role


def _read(project: Project, role: ProjectRole) -> ProjectRead:
    return ProjectRead(
        id=project.id,
        name=project.name,
        description=project.description,
        role=role,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def create_project(session: Session, *, user_id: uuid.UUID, name: str, description: str | None) -> ProjectRead:
    """Create a project with ``user_id`` as its Owner, atomically; commit."""
    try:
        project = repository.insert_project(session, name=name, description=description)
        repository.insert_member(session, project_id=project.id, user_id=user_id, role=ProjectRole.OWNER)
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info("Project created", extra={"project_id": str(project.id), "user_id": str(user_id)})
    return _read(project, ProjectRole.OWNER)


def list_projects(session: Session, *, user_id: uuid.UUID, limit: int, offset: int) -> list[ProjectSummary]:
    """The projects ``user_id`` belongs to, with their role, ordered by name. Read-only."""
    rows = repository.list_user_projects(session, user_id=user_id, limit=limit, offset=offset)
    return [
        ProjectSummary(id=project.id, name=project.name, role=ProjectRole(role), created_at=project.created_at)
        for project, role in rows
    ]


def get_project(session: Session, *, access: ProjectAccess) -> ProjectRead:
    """The project behind a verified access. Read-only."""
    project = repository.get_project(session, access.project_id)
    if project is None:
        raise ProjectNotFoundError()
    return _read(project, access.role)


def update_project(session: Session, *, access: ProjectAccess, changes: dict[str, str | None]) -> ProjectRead:
    """Apply ``changes`` (name and/or description); requires Admin or Owner; commit."""
    try:
        project, role = _lock_with_role(session, access, ProjectRole.ADMIN)
        for field, value in changes.items():
            if field not in ("name", "description"):
                raise ValueError(f"Unsupported project field: {field}")
            setattr(project, field, value)
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(
        "Project updated",
        extra={"project_id": str(project.id), "user_id": str(access.user_id), "fields": sorted(changes)},
    )
    return _read(project, role)


# --- Memberships -------------------------------------------------------------------------------


def _member_read(member: ProjectMember, user: UserRead) -> MemberRead:
    return MemberRead(
        user_id=member.user_id,
        email=user.email,
        role=ProjectRole(member.role),
        is_active=user.is_active,
        created_at=member.created_at,
    )


def list_members(session: Session, *, access: ProjectAccess) -> list[MemberRead]:
    """Every member with email and account state: two queries in total (memberships, then one
    batched user lookup), however many members there are. Owner first, then Admins, then Members,
    each by email. Read-only."""
    members = repository.list_members(session, access.project_id)
    users = auth_service.get_users(session, [member.user_id for member in members])
    result = []
    for member in members:
        user = users.get(member.user_id)
        if user is None:  # pragma: no cover - prevented by the foreign key (ON DELETE RESTRICT)
            logger.error("Member without a user account", extra={"project_id": str(access.project_id)})
            continue
        result.append(_member_read(member, user))
    return sorted(result, key=lambda m: (-_ROLE_RANK[m.role], m.email, str(m.user_id)))


def add_member(session: Session, *, access: ProjectAccess, email: str, role: ProjectRole) -> MemberRead:
    """Add an existing, active registered user by email with ``role`` (Admin or Member); requires
    Admin or Owner, re-checked under the project lock; commit.

    Raises ``UserNotFoundError`` (no such account), ``InactiveUserError``,
    ``AlreadyMemberError``, or ``OwnerRoleChangeForbiddenError`` (Owner is never assigned here).
    """
    try:
        if role not in _ASSIGNABLE_ROLES:
            raise OwnerRoleChangeForbiddenError()
        _lock_with_role(session, access, ProjectRole.ADMIN)
        user = auth_service.find_user_by_email(session, email)
        if user is None:
            raise UserNotFoundError()
        if not user.is_active:
            raise InactiveUserError()
        if repository.get_membership(session, project_id=access.project_id, user_id=user.id) is not None:
            raise AlreadyMemberError()
        member = repository.insert_member(session, project_id=access.project_id, user_id=user.id, role=role)
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(
        "Project member added",
        extra={
            "project_id": str(access.project_id),
            "user_id": str(access.user_id),
            "member_user_id": str(user.id),
            "role": role.value,
        },
    )
    return _member_read(member, user)


def change_member_role(
    session: Session, *, access: ProjectAccess, user_id: uuid.UUID, role: ProjectRole
) -> MemberRead:
    """Set another member's (or an Admin's own) role to Admin or Member; requires Admin or Owner,
    re-checked under the project lock; commit.

    The Owner's membership never changes here (``OwnerRoleChangeForbiddenError``): neither the
    Owner's role nor anyone's promotion to Owner. ``MemberNotFoundError`` if ``user_id`` is not a
    member of this project.
    """
    try:
        if role not in _ASSIGNABLE_ROLES:
            raise OwnerRoleChangeForbiddenError()
        _lock_with_role(session, access, ProjectRole.ADMIN)
        member = repository.get_membership(session, project_id=access.project_id, user_id=user_id)
        if member is None:
            raise MemberNotFoundError()
        if member.role == ProjectRole.OWNER:
            raise OwnerRoleChangeForbiddenError()
        user = auth_service.get_users(session, [user_id]).get(user_id)
        previous = member.role
        member.role = role.value
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(
        "Project member role changed",
        extra={
            "project_id": str(access.project_id),
            "user_id": str(access.user_id),
            "member_user_id": str(user_id),
            "from_role": previous,
            "to_role": role.value,
        },
    )
    assert user is not None  # the foreign key guarantees the account exists
    return _member_read(member, user)


def remove_member(session: Session, *, access: ProjectAccess, user_id: uuid.UUID) -> None:
    """Remove a member, or leave the project when ``user_id`` is the caller; commit.

    Anyone but the Owner may leave. Removing someone else requires Admin or Owner. The Owner can be
    neither removed nor leave (``OwnerRoleChangeForbiddenError``: transfer ownership first). All
    checks run against committed state under the project lock. ``MemberNotFoundError`` if
    ``user_id`` is not (or no longer) a member of this project.
    """
    leaving = user_id == access.user_id
    try:
        _, actor_role = _lock_with_role(session, access, ProjectRole.MEMBER)
        if not leaving and not role_allows(actor_role, ProjectRole.ADMIN):
            raise InsufficientProjectRoleError()
        member = repository.get_membership(session, project_id=access.project_id, user_id=user_id)
        if member is None:
            raise MemberNotFoundError()
        if member.role == ProjectRole.OWNER:
            raise OwnerRoleChangeForbiddenError()
        repository.delete_member(session, project_id=access.project_id, user_id=user_id)
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(
        "Project member left" if leaving else "Project member removed",
        extra={"project_id": str(access.project_id), "user_id": str(access.user_id), "member_user_id": str(user_id)},
    )


def transfer_ownership(session: Session, *, access: ProjectAccess, user_id: uuid.UUID) -> OwnershipTransferResult:
    """Make ``user_id`` the Owner and the current Owner an Admin (ARCHITECTURE D19), atomically; commit.

    Under the project lock: the caller must still be the Owner (``InsufficientProjectRoleError``
    otherwise, or ``ProjectNotFoundError`` if no longer a member), the target must be a member of
    *this* project (``MemberNotFoundError``) with an active account (``InactiveUserError``), and
    not the caller (``OwnershipTransferToSelfError``). The old Owner is demoted before the target
    is promoted, so the one-Owner index is never violated; any failure leaves both unchanged.
    """
    try:
        _lock_with_role(session, access, ProjectRole.OWNER)
        if user_id == access.user_id:
            raise OwnershipTransferToSelfError()
        target = repository.get_membership(session, project_id=access.project_id, user_id=user_id)
        if target is None:
            raise MemberNotFoundError()
        users = auth_service.get_users(session, [access.user_id, user_id])
        if not users[user_id].is_active:
            raise InactiveUserError()

        # Two statements, in this order: the Owner index is checked per statement.
        repository.set_member_role(session, project_id=access.project_id, user_id=access.user_id, role=ProjectRole.ADMIN)
        repository.set_member_role(session, project_id=access.project_id, user_id=user_id, role=ProjectRole.OWNER)
        new_owner = repository.get_membership(session, project_id=access.project_id, user_id=user_id)
        previous_owner = repository.get_membership(session, project_id=access.project_id, user_id=access.user_id)
        assert new_owner is not None and previous_owner is not None  # both rows are locked by our updates
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(
        "Project ownership transferred",
        extra={"project_id": str(access.project_id), "user_id": str(access.user_id), "new_owner_user_id": str(user_id)},
    )
    return OwnershipTransferResult(
        new_owner=_member_read(new_owner, users[user_id]),
        previous_owner=_member_read(previous_owner, users[access.user_id]),
    )


# --- Retention ---------------------------------------------------------------------------------


def _valid_days(value: object) -> bool:
    return type(value) is int and MIN_RETENTION_DAYS <= value <= MAX_RETENTION_DAYS


def _stored_overrides(project: Project) -> dict[str, int]:
    """The project's valid overrides. Entries the API could not have written (unknown keys,
    non-integers, out-of-range values) are ignored, so the defaults apply, and logged by count."""
    raw = project.retention_overrides or {}
    valid = {key: value for key, value in raw.items() if key in RETENTION_KEYS and _valid_days(value)}
    if len(valid) != len(raw):
        logger.warning(
            "Ignoring invalid retention overrides",
            extra={"project_id": str(project.id), "invalid_count": len(raw) - len(valid)},
        )
    return valid


def _retention_defaults(settings: Settings) -> dict[str, int]:
    return {key: getattr(settings, f"retention_{key}") for key in RETENTION_KEYS}


def _retention_read(project: Project, settings: Settings) -> RetentionRead:
    overrides = _stored_overrides(project)
    defaults = _retention_defaults(settings)
    return RetentionRead(
        project_id=project.id,
        effective=RetentionPeriods(**(defaults | overrides)),
        overrides=RetentionOverrides(**overrides),
        defaults=RetentionPeriods(**defaults),
    )


def get_retention(session: Session, *, access: ProjectAccess, settings: Settings) -> RetentionRead:
    """The project's effective retention (configured defaults plus its overrides). Read-only."""
    project = repository.get_project(session, access.project_id)
    if project is None:
        raise ProjectNotFoundError()
    return _retention_read(project, settings)


def update_retention(
    session: Session, *, access: ProjectAccess, changes: dict[str, int | None], settings: Settings
) -> RetentionRead:
    """Merge ``changes`` into the project's overrides; ``None`` removes an override (back to the
    default). Requires Admin or Owner, re-checked under the project lock; commit.

    Overrides not in ``changes`` are kept. The merge reads the overrides under the lock, so
    concurrent updates of different periods never lose each other. When no override remains the
    column is set to SQL NULL.
    """
    try:
        project, _ = _lock_with_role(session, access, ProjectRole.ADMIN)
        overrides = _stored_overrides(project)
        for key, value in changes.items():
            if key not in RETENTION_KEYS:
                raise ValueError(f"Unsupported retention setting: {key}")
            if value is None:
                overrides.pop(key, None)
            elif _valid_days(value):
                overrides[key] = value
            else:
                raise ValueError(f"Retention for {key} must be {MIN_RETENTION_DAYS}-{MAX_RETENTION_DAYS} days")
        # A new object, so the change is always detected (JSONB is not mutation-tracked).
        project.retention_overrides = dict(sorted(overrides.items())) or None
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(
        "Project retention updated",
        extra={"project_id": str(access.project_id), "user_id": str(access.user_id), "fields": sorted(changes)},
    )
    return _retention_read(project, settings)


# --- API keys ----------------------------------------------------------------------------------

# Revoked keys do not count (ARCHITECTURE D20).
MAX_ACTIVE_API_KEYS = 25


def _api_key_read(api_key: ApiKey) -> ApiKeyRead:
    return ApiKeyRead(
        id=api_key.id,
        name=api_key.name,
        prefix=api_key.prefix,
        is_active=api_key.revoked_at is None,
        created_by=api_key.created_by,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        revoked_at=api_key.revoked_at,
        revoked_by=api_key.revoked_by,
    )


def create_api_key(session: Session, *, access: ProjectAccess, name: str) -> ApiKeyCreated:
    """Create a key named ``name``; requires Admin or Owner, re-checked under the project lock;
    commit. The full key is in the result and nowhere else: only its SHA-256 digest is stored.

    The active-key count is taken under the project-row lock, which every key creation holds, so
    concurrent creations cannot exceed ``MAX_ACTIVE_API_KEYS`` (``ApiKeyLimitError``).
    """
    try:
        _lock_with_role(session, access, ProjectRole.ADMIN)
        if repository.count_active_api_keys(session, access.project_id) >= MAX_ACTIVE_API_KEYS:
            raise ApiKeyLimitError(MAX_ACTIVE_API_KEYS)
        key = generate_api_key()
        api_key = repository.insert_api_key(
            session,
            project_id=access.project_id,
            name=name,
            prefix=key[:API_KEY_DISPLAY_PREFIX_LENGTH],
            key_hash=hash_api_key(key),
            created_by=access.user_id,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(
        "API key created",
        extra={"project_id": str(access.project_id), "user_id": str(access.user_id), "api_key_id": str(api_key.id)},
    )
    return ApiKeyCreated(**_api_key_read(api_key).model_dump(), key=key)


def list_api_keys(session: Session, *, access: ProjectAccess, limit: int, offset: int) -> list[ApiKeyRead]:
    """The project's keys, revoked ones included, newest first; metadata only. Read-only.

    Requires Admin or Owner: the route's dependency checks it, and so does this function.
    """
    if not role_allows(access.role, ProjectRole.ADMIN):
        raise InsufficientProjectRoleError()
    keys = repository.list_api_keys(session, project_id=access.project_id, limit=limit, offset=offset)
    return [_api_key_read(api_key) for api_key in keys]


def revoke_api_key(session: Session, *, access: ProjectAccess, api_key_id: uuid.UUID) -> ApiKeyRead:
    """Revoke one of *this* project's keys; requires Admin or Owner, re-checked under the project
    lock; commit. The key is kept for audit and never authenticates again.

    Revoking an already revoked key changes nothing and returns it as it is (idempotent).
    ``ApiKeyNotFoundError`` if the key does not exist or belongs to another project.
    """
    try:
        _lock_with_role(session, access, ProjectRole.ADMIN)
        api_key = repository.get_api_key(session, project_id=access.project_id, api_key_id=api_key_id)
        if api_key is None:
            raise ApiKeyNotFoundError()
        newly_revoked = api_key.revoked_at is None
        if newly_revoked:
            repository.revoke_api_key(session, api_key=api_key, revoked_by=access.user_id)
        session.commit()
    except Exception:
        session.rollback()
        raise
    if newly_revoked:
        logger.info(
            "API key revoked",
            extra={"project_id": str(access.project_id), "user_id": str(access.user_id), "api_key_id": str(api_key_id)},
        )
    return _api_key_read(api_key)


def authenticate_api_key(session: Session, key: object) -> ApiKeyPrincipal | None:
    """The project a presented key belongs to, or None if the key is malformed, unknown or revoked.
    Read-only; never logs or returns the key.

    The lookup is by SHA-256 digest through the unique index, so the comparison happens on the
    digest, never on the key, and timing reveals nothing about stored keys. A deleted project's
    keys are gone with it (ON DELETE CASCADE).
    """
    if not is_well_formed_api_key(key):
        return None
    api_key = repository.find_active_api_key(session, hash_api_key(key))  # type: ignore[arg-type]
    if api_key is None:
        return None
    return ApiKeyPrincipal(project_id=api_key.project_id, api_key_id=api_key.id)


def delete_project(session: Session, *, access: ProjectAccess, confirm_name: str) -> None:
    """Delete the project with its memberships and API keys; requires the Owner and the project's
    exact name as confirmation; commit."""
    try:
        project, _ = _lock_with_role(session, access, ProjectRole.OWNER)
        if confirm_name.strip() != project.name:
            raise DeletionConfirmationError()
        repository.delete_project(session, project.id)
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info("Project deleted", extra={"project_id": str(access.project_id), "user_id": str(access.user_id)})
