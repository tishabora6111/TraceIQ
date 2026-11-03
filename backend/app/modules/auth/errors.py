"""Errors raised by the auth module. Messages never contain emails, passwords, or tokens."""

from app.core.errors import AppError, ConflictError, ForbiddenError, ServiceUnavailableError, UnauthorizedError


class AuthenticationBusyError(ServiceUnavailableError):
    """Password hashing capacity is exhausted. The same for every account, so it reveals nothing."""

    code = "authentication_busy"
    headers = {"Retry-After": "5"}

    def __init__(self) -> None:
        super().__init__("Authentication is temporarily busy. Retry shortly.")


class InvalidCredentialsError(UnauthorizedError):
    """Unknown email, wrong password, or inactive account: deliberately indistinguishable."""

    def __init__(self) -> None:
        super().__init__("Invalid email or password.")


class NotAuthenticatedError(UnauthorizedError):
    """Missing or invalid bearer token, or a token whose user or session is no longer valid.

    One message for every case, so responses never reveal which check failed.
    """

    def __init__(self) -> None:
        super().__init__("Not authenticated.")


class EmailAlreadyRegisteredError(ConflictError):
    code = "email_already_registered"

    def __init__(self) -> None:
        super().__init__("An account with this email already exists.")


class RegistrationDisabledError(ForbiddenError):
    code = "registration_disabled"

    def __init__(self) -> None:
        super().__init__("Registration is disabled.")


class InvalidPasswordError(AppError):
    status_code = 422
    code = "invalid_password"


class CsrfValidationError(ForbiddenError):
    """The X-CSRF-Token header and CSRF cookie are missing, differ, or are not signed for this session."""

    code = "csrf_failed"

    def __init__(self) -> None:
        super().__init__("CSRF validation failed.")


class OriginNotAllowedError(ForbiddenError):
    code = "origin_not_allowed"

    def __init__(self) -> None:
        super().__init__("Request origin is not allowed.")
