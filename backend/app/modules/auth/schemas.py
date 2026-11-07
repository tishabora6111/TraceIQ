"""Request and response schemas for the auth API.

Passwords are ``SecretStr`` so they never appear in a repr or log line. Emails are trimmed and
lower-cased before validation, so the stored and compared form is always the normalized one.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, SecretStr

from app.core.security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH


def normalize_email(value: str) -> str:
    """The canonical form of an email address: surrounding whitespace removed, lower-cased."""
    return value.strip().lower()


def _normalize_if_str(value: object) -> object:
    return normalize_email(value) if isinstance(value, str) else value


# email-validator also normalizes (e.g. Unicode NFC); lower-casing again keeps the result canonical.
NormalizedEmail = Annotated[EmailStr, BeforeValidator(_normalize_if_str), AfterValidator(normalize_email)]


class RegisterRequest(BaseModel):
    email: NormalizedEmail
    password: SecretStr = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class LoginRequest(BaseModel):
    email: NormalizedEmail
    # No minimum beyond non-empty: a policy change must not stop existing users from logging in.
    password: SecretStr = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    # Access-token lifetime in seconds.
    expires_in: int
    # Also set as a cookie; sent back in the X-CSRF-Token header on refresh and logout.
    csrf_token: str
    user: UserRead
