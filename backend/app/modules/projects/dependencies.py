"""FastAPI dependencies provided by the projects module (public, like auth.dependencies)."""

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Path
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.auth.dependencies import CurrentUser
from app.modules.projects import service
from app.modules.projects.models import ProjectRole
from app.modules.projects.schemas import ProjectAccess


def require_project_role(minimum: ProjectRole) -> Callable[..., ProjectAccess]:
    """A dependency verifying the current user's role in the ``{project_id}`` path parameter.

    Usage: ``access: Annotated[ProjectAccess, Depends(require_project_role(ProjectRole.ADMIN))]``.
    Non-members get 404 ``project_not_found`` (project existence is never revealed); members below
    ``minimum`` get 403 ``insufficient_project_role``. Write services re-check the role under a
    project-row lock, so this check alone is never trusted for a change.
    """

    def dependency(
        project_id: Annotated[uuid.UUID, Path()],
        user: CurrentUser,
        session: Annotated[Session, Depends(get_db)],
    ) -> ProjectAccess:
        return service.get_project_access(session, project_id=project_id, user_id=user.id, minimum=minimum)

    dependency.__name__ = f"require_project_role_{minimum.value}"
    return dependency


ProjectMemberAccess = Annotated[ProjectAccess, Depends(require_project_role(ProjectRole.MEMBER))]
ProjectAdminAccess = Annotated[ProjectAccess, Depends(require_project_role(ProjectRole.ADMIN))]
ProjectOwnerAccess = Annotated[ProjectAccess, Depends(require_project_role(ProjectRole.OWNER))]
