"""Errors raised by the auth module. Messages never contain emails, passwords, or tokens."""

from app.core.errors import AppError, ConflictError, ForbiddenError, UnauthorizedError


class InvalidCredentialsError(UnauthorizedError):
    """Unknown email, wrong password, or inactive account: deliberately indistinguishable."""

    def __init__(self) -> None:
        super().__init__("Invalid email or password.")


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


class OriginNotAllowedError(ForbiddenError):
    code = "origin_not_allowed"

    def __init__(self) -> None:
        super().__init__("Request origin is not allowed.")
