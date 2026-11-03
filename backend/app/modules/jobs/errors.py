"""Errors raised by the jobs module."""

import uuid
from typing import Any

from app.core.errors import AppError, ConflictError


class JobRegistrationError(Exception):
    """A handler definition is invalid or conflicts with an existing one (a programming error)."""


class PermanentJobError(Exception):
    """Raised by a handler when retrying cannot help; the job fails immediately without retries."""


class UnknownJobTypeError(AppError):
    code = "unknown_job_type"

    def __init__(self, job_type: str) -> None:
        super().__init__(f"Unknown job type: {job_type!r}", details={"job_type": job_type})
        self.job_type = job_type


class InvalidJobPayloadError(AppError):
    status_code = 422
    code = "invalid_job_payload"

    def __init__(self, job_type: str, message: str, errors: Any = None) -> None:
        super().__init__(f"Invalid payload for job type {job_type!r}: {message}", details=errors)
        self.job_type = job_type


class DuplicateJobError(ConflictError):
    """An active (queued or running) job with the same dedupe key already exists."""

    code = "duplicate_job"

    def __init__(self, dedupe_key: str, existing_job_id: uuid.UUID | None) -> None:
        super().__init__(
            f"An active job with dedupe key {dedupe_key!r} already exists",
            details={"dedupe_key": dedupe_key, "existing_job_id": str(existing_job_id) if existing_job_id else None},
        )
        self.dedupe_key = dedupe_key
        self.existing_job_id = existing_job_id
