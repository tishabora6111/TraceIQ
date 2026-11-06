"""Errors raised by the projects module. Messages never contain names, emails, or keys."""

from app.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError


class ProjectNotFoundError(NotFoundError):
    """The project does not exist, or the caller is not a member: deliberately indistinguishable,
    so non-members cannot learn which projects exist."""

    code = "project_not_found"

    def __init__(self) -> None:
        super().__init__("Project not found.")


class InsufficientProjectRoleError(ForbiddenError):
    code = "insufficient_project_role"

    def __init__(self) -> None:
        super().__init__("Your project role does not allow this action.")


class MemberNotFoundError(NotFoundError):
    """No such member in this project (memberships are always looked up by project and user)."""

    code = "member_not_found"

    def __init__(self) -> None:
        super().__init__("Member not found.")


class UserNotFoundError(NotFoundError):
    """No registered user has this email. Only existing users can be added (ARCHITECTURE D18)."""

    code = "user_not_found"

    def __init__(self) -> None:
        super().__init__("No registered user has this email.")


class InactiveUserError(ConflictError):
    code = "user_inactive"

    def __init__(self) -> None:
        super().__init__("This user's account is inactive.")


class AlreadyMemberError(ConflictError):
    code = "already_member"

    def __init__(self) -> None:
        super().__init__("This user is already a member of the project.")


class OwnerRoleChangeForbiddenError(ConflictError):
    """The Owner's membership is changed only by an ownership transfer: never through role
    changes, removal, or leaving, and the Owner role is never assigned directly."""

    code = "owner_role_change_forbidden"

    def __init__(self) -> None:
        super().__init__("The Owner cannot be changed or removed here. Transfer ownership first.")


class OwnershipTransferToSelfError(ConflictError):
    code = "ownership_transfer_to_self"

    def __init__(self) -> None:
        super().__init__("You are already the Owner; choose another member.")


class DeletionConfirmationError(AppError):
    """DELETE /projects/{id} requires the project's exact name as confirmation."""

    status_code = 422
    code = "confirmation_mismatch"

    def __init__(self) -> None:
        super().__init__("The confirmation does not match the project name.")


class ApiKeyNotFoundError(NotFoundError):
    """No such key in this project (keys are always looked up by project and key id)."""

    code = "api_key_not_found"

    def __init__(self) -> None:
        super().__init__("API key not found.")


class ApiKeyLimitError(ConflictError):
    code = "api_key_limit_reached"

    def __init__(self, limit: int) -> None:
        super().__init__(f"A project can have at most {limit} active API keys. Revoke one first.")
