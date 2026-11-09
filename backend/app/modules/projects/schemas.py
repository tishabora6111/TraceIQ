"""Request and response schemas for the projects API.

Names and descriptions are normalized with Python's ``str.strip()`` (which also removes Unicode
whitespace such as U+00A0) before their length is checked. The database check on names covers
ASCII whitespace only and is a backstop.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, StrictInt, model_validator

from app.core.config import MAX_RETENTION_DAYS, MIN_RETENTION_DAYS
from app.modules.projects.models import (
    API_KEY_NAME_MAX_LENGTH,
    PROJECT_DESCRIPTION_MAX_LENGTH,
    PROJECT_NAME_MAX_LENGTH,
    ProjectRole,
)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50


def _strip(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _strip_to_none(value: object) -> object:
    """An empty or whitespace-only description means "no description"."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


ProjectName = Annotated[str, BeforeValidator(_strip), Field(min_length=1, max_length=PROJECT_NAME_MAX_LENGTH)]
ProjectDescription = Annotated[
    Annotated[str, Field(max_length=PROJECT_DESCRIPTION_MAX_LENGTH)] | None, BeforeValidator(_strip_to_none)
]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ProjectName
    description: ProjectDescription = None


class ProjectUpdate(BaseModel):
    """Partial update: only fields present in the request change. ``description: null`` clears it."""

    model_config = ConfigDict(extra="forbid")

    name: ProjectName | None = None
    description: ProjectDescription = None

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self

    def changes(self) -> dict[str, str | None]:
        return {field: getattr(self, field) for field in self.model_fields_set}


class ProjectDeleteConfirmation(BaseModel):
    """Body of DELETE /projects/{id}: the project's exact current name (surrounding whitespace is
    ignored). In the body rather than the URL, so the name never reaches access logs."""

    model_config = ConfigDict(extra="forbid")

    confirm_name: Annotated[str, Field(min_length=1, max_length=PROJECT_NAME_MAX_LENGTH * 2)]


class ProjectRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    # The caller's role in this project.
    role: ProjectRole
    created_at: datetime
    updated_at: datetime


class ProjectSummary(BaseModel):
    id: uuid.UUID
    name: str
    role: ProjectRole
    created_at: datetime


# Roles that can be granted through membership endpoints. The Owner role only moves by transfer.
AssignableRole = Literal[ProjectRole.ADMIN, ProjectRole.MEMBER]


class MemberAdd(BaseModel):
    """Add an existing registered user, found by email (normalized as at registration)."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    role: AssignableRole


class MemberRoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: AssignableRole


class MemberRead(BaseModel):
    user_id: uuid.UUID
    email: str
    role: ProjectRole
    # False when the member's account was deactivated after joining; they keep the membership
    # but cannot sign in, so it stays visible to the other members.
    is_active: bool
    created_at: datetime


class OwnershipTransfer(BaseModel):
    """Make ``user_id``, an existing active member of this project, the Owner."""

    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID


class OwnershipTransferResult(BaseModel):
    """Both changed memberships: the new Owner, and the previous Owner (now Admin)."""

    new_owner: MemberRead
    previous_owner: MemberRead


# --- Retention ----------------------------------------------------------------------------------

# Every overridable period. Each key is also the stored override key and, prefixed with
# ``retention_``, the name of its default in ``core.config.Settings``.
RETENTION_KEYS = (
    "traces_days",
    "evaluation_results_days",
    "prediction_logs_days",
    "incidents_days_after_resolution",
    "investigation_reports_days_after_update",
)
# Model artifacts are never deleted automatically and cannot be overridden (D7).
MODEL_ARTIFACTS_RETENTION = "until_manually_deleted"

# Strict: booleans, numeric strings and floats are rejected rather than coerced.
RetentionDays = Annotated[StrictInt, Field(ge=MIN_RETENTION_DAYS, le=MAX_RETENTION_DAYS)]


class RetentionPeriods(BaseModel):
    """A complete set of periods in days: the configured defaults, or the effective values."""

    traces_days: int
    evaluation_results_days: int
    prediction_logs_days: int
    incidents_days_after_resolution: int
    investigation_reports_days_after_update: int


class RetentionOverrides(BaseModel):
    """The project's overrides; null means the configured default applies."""

    traces_days: int | None = None
    evaluation_results_days: int | None = None
    prediction_logs_days: int | None = None
    incidents_days_after_resolution: int | None = None
    investigation_reports_days_after_update: int | None = None


class RetentionRead(BaseModel):
    project_id: uuid.UUID
    # Defaults plus overrides: what retention cleanup applies to this project.
    effective: RetentionPeriods
    overrides: RetentionOverrides
    defaults: RetentionPeriods
    model_artifacts: Literal["until_manually_deleted"] = MODEL_ARTIFACTS_RETENTION


class RetentionUpdate(BaseModel):
    """Partial update: only fields present change; ``null`` resets that period to its default.
    Unknown fields, including ``model_artifacts``, are rejected."""

    model_config = ConfigDict(extra="forbid")

    traces_days: RetentionDays | None = None
    evaluation_results_days: RetentionDays | None = None
    prediction_logs_days: RetentionDays | None = None
    incidents_days_after_resolution: RetentionDays | None = None
    investigation_reports_days_after_update: RetentionDays | None = None

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one retention setting to update")
        return self

    def changes(self) -> dict[str, int | None]:
        return {field: getattr(self, field) for field in self.model_fields_set}


# --- API keys -----------------------------------------------------------------------------------

# Trimmed like project names, then 1–100 characters (the column's limit).
ApiKeyName = Annotated[str, BeforeValidator(_strip), Field(min_length=1, max_length=API_KEY_NAME_MAX_LENGTH)]


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ApiKeyName


class ApiKeyRead(BaseModel):
    """Safe metadata only: never the key itself or its hash."""

    id: uuid.UUID
    name: str
    # The key's first characters (``tiq_`` plus 8), to tell keys apart.
    prefix: str
    is_active: bool
    created_by: uuid.UUID | None
    created_at: datetime
    # Set by ingestion once it exists (M2); always null until then.
    last_used_at: datetime | None
    revoked_at: datetime | None
    revoked_by: uuid.UUID | None


class ApiKeyCreated(ApiKeyRead):
    """The creation response: the only time the full key is ever returned."""

    key: str


class ApiKeyPrincipal(BaseModel):
    """What a valid, unrevoked API key authenticates: one project."""

    model_config = ConfigDict(frozen=True)

    project_id: uuid.UUID
    api_key_id: uuid.UUID


class ProjectAccess(BaseModel):
    """A verified membership: the caller (``user_id``) belongs to ``project_id`` with ``role``."""

    model_config = ConfigDict(frozen=True)

    project_id: uuid.UUID
    user_id: uuid.UUID
    role: ProjectRole
