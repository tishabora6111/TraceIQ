import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.modules.auth.dependencies import CurrentUser, require_allowed_origin_for_writes
from app.modules.projects import service
from app.modules.projects.dependencies import ProjectAdminAccess, ProjectMemberAccess, ProjectOwnerAccess
from app.modules.projects.models import ProjectRole
from app.modules.projects.schemas import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyRead,
    MemberAdd,
    MemberRead,
    MemberRoleUpdate,
    OwnershipTransfer,
    OwnershipTransferResult,
    ProjectCreate,
    ProjectDeleteConfirmation,
    ProjectRead,
    ProjectSummary,
    ProjectUpdate,
    RetentionRead,
    RetentionUpdate,
)

# Every write route on this router is Origin-checked (ARCHITECTURE §7.2); every route also
# requires a bearer access token (CurrentUser, directly or through the project-role dependency).
router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(require_allowed_origin_for_writes)])

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProjectRead)
def create_project(body: ProjectCreate, user: CurrentUser, session: DbSession) -> ProjectRead:
    """Create a project. The caller becomes its Owner."""
    return service.create_project(session, user_id=user.id, name=body.name, description=body.description)


@router.get("", response_model=list[ProjectSummary])
def list_projects(
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ProjectSummary]:
    """The projects the caller belongs to, with the caller's role, ordered by name."""
    return service.list_projects(session, user_id=user.id, limit=limit, offset=offset)


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(access: ProjectMemberAccess, session: DbSession) -> ProjectRead:
    """Project details. Any member."""
    return service.get_project(session, access=access)


@router.patch("/{project_id}", response_model=ProjectRead)
def update_project(body: ProjectUpdate, access: ProjectAdminAccess, session: DbSession) -> ProjectRead:
    """Change the name and/or description. Owner or Admin."""
    return service.update_project(session, access=access, changes=body.changes())


@router.get("/{project_id}/members", response_model=list[MemberRead])
def list_members(access: ProjectMemberAccess, session: DbSession) -> list[MemberRead]:
    """The project's members with email, role and account state. Any member."""
    return service.list_members(session, access=access)


@router.post("/{project_id}/members", status_code=status.HTTP_201_CREATED, response_model=MemberRead)
def add_member(body: MemberAdd, access: ProjectAdminAccess, session: DbSession) -> MemberRead:
    """Add an existing registered user by email as Admin or Member. Owner or Admin."""
    return service.add_member(session, access=access, email=str(body.email), role=ProjectRole(body.role))


@router.patch("/{project_id}/members/{user_id}", response_model=MemberRead)
def change_member_role(
    user_id: uuid.UUID, body: MemberRoleUpdate, access: ProjectAdminAccess, session: DbSession
) -> MemberRead:
    """Change a member's role to Admin or Member. Owner or Admin; the Owner's role never changes here."""
    return service.change_member_role(session, access=access, user_id=user_id, role=ProjectRole(body.role))


@router.delete("/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(user_id: uuid.UUID, access: ProjectMemberAccess, session: DbSession) -> None:
    """Remove a member (Owner or Admin), or leave the project (your own user id; any role but Owner).

    The Owner can neither leave nor be removed: transfer ownership first.
    """
    service.remove_member(session, access=access, user_id=user_id)


@router.post("/{project_id}/ownership-transfer", response_model=OwnershipTransferResult)
def transfer_ownership(body: OwnershipTransfer, access: ProjectOwnerAccess, session: DbSession) -> OwnershipTransferResult:
    """Make another active member the Owner; you become an Admin. Owner only."""
    return service.transfer_ownership(session, access=access, user_id=body.user_id)


@router.get("/{project_id}/retention", response_model=RetentionRead)
def get_retention(access: ProjectMemberAccess, session: DbSession, settings: AppSettings) -> RetentionRead:
    """Effective retention periods, the project's overrides and the configured defaults. Any member."""
    return service.get_retention(session, access=access, settings=settings)


@router.patch("/{project_id}/retention", response_model=RetentionRead)
def update_retention(
    body: RetentionUpdate, access: ProjectAdminAccess, session: DbSession, settings: AppSettings
) -> RetentionRead:
    """Override retention periods (1–3650 days); null resets one to its default. Owner or Admin.

    Only the fields sent change. Model artifacts are kept until manually deleted and cannot be
    overridden.
    """
    return service.update_retention(session, access=access, changes=body.changes(), settings=settings)


@router.post("/{project_id}/api-keys", status_code=status.HTTP_201_CREATED, response_model=ApiKeyCreated)
def create_api_key(
    body: ApiKeyCreate, access: ProjectAdminAccess, session: DbSession, response: Response
) -> ApiKeyCreated:
    """Create an API key. Owner or Admin; at most 25 active keys per project.

    The response contains the full key. This is the only time it is shown: store it now.
    """
    created = service.create_api_key(session, access=access, name=body.name)
    # The body holds a secret: never cache it anywhere.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return created


@router.get("/{project_id}/api-keys", response_model=list[ApiKeyRead])
def list_api_keys(
    access: ProjectAdminAccess,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ApiKeyRead]:
    """The project's keys, revoked included, newest first. Metadata only, never the key. Owner or Admin."""
    return service.list_api_keys(session, access=access, limit=limit, offset=offset)


@router.post("/{project_id}/api-keys/{api_key_id}/revoke", response_model=ApiKeyRead)
def revoke_api_key(api_key_id: uuid.UUID, access: ProjectAdminAccess, session: DbSession) -> ApiKeyRead:
    """Revoke a key permanently; it stays listed. Owner or Admin. Repeating it changes nothing."""
    return service.revoke_api_key(session, access=access, api_key_id=api_key_id)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(body: ProjectDeleteConfirmation, access: ProjectOwnerAccess, session: DbSession) -> None:
    """Delete the project with its memberships and API keys. Owner only; irreversible.

    The body must repeat the project's exact current name. It is sent in the body, not the URL,
    so the name never appears in access logs.
    """
    service.delete_project(session, access=access, confirm_name=body.confirm_name)
