"""Explicit registry of job handlers.

A job's ``type`` is only ever used as a key into a registry. Nothing is imported, evaluated, or
resolved from job payloads or database rows; a type that is not registered cannot run.
Modules register handlers in their ``jobs.py``; the worker imports those modules explicitly.
"""

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from app.modules.jobs.errors import InvalidJobPayloadError, JobRegistrationError, UnknownJobTypeError

# "<module>.<action>", lowercase, e.g. "rag_eval.run_evaluation".
JOB_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
MAX_JOB_TYPE_LENGTH = 255

# Schedule names: lowercase, dot-separated, e.g. "incidents.detect".
SCHEDULE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
SCHEDULE_DEDUPE_PREFIX = "schedule:"
# The dedupe key "schedule:<name>" must fit the 255-character dedupe_key column.
MAX_SCHEDULE_NAME_LENGTH = 255 - len(SCHEDULE_DEDUPE_PREFIX)
# job_schedules.interval_seconds is a PostgreSQL integer.
MAX_SCHEDULE_INTERVAL_SECONDS = 2**31 - 1


class JobPayload(BaseModel):
    """Base class for job payload models. Unknown fields are rejected, not silently dropped."""

    model_config = ConfigDict(extra="forbid", frozen=True)


PayloadT = TypeVar("PayloadT", bound=JobPayload)


@dataclass(frozen=True)
class JobContext:
    """Passed to a handler when its job runs. ``session`` is the job's transaction."""

    session: Session
    job_id: uuid.UUID
    job_type: str
    attempt: int
    max_attempts: int


HandlerFunc = Callable[[JobContext, PayloadT], None]


@dataclass(frozen=True)
class JobHandler(Generic[PayloadT]):
    name: str
    func: HandlerFunc[PayloadT]
    payload_model: type[PayloadT]
    # None means "use the configured default".
    max_attempts: int | None = None
    lease_seconds: int | None = None


@dataclass(frozen=True)
class ScheduleDefinition:
    """A recurring job, run every ``interval_seconds``. Interval-based only; no cron expressions."""

    name: str
    job_type: str
    interval_seconds: int
    # Validated against the job type's payload model and serialized to JSON at registration.
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return f"{SCHEDULE_DEDUPE_PREFIX}{self.name}"


def validate_payload(handler: JobHandler[Any], payload: JobPayload | Mapping[str, Any] | None) -> JobPayload:
    """Validate ``payload`` against the handler's model and return the model instance."""
    model = handler.payload_model
    if isinstance(payload, JobPayload) and not isinstance(payload, model):
        raise InvalidJobPayloadError(handler.name, f"expected {model.__name__}, got {type(payload).__name__}")
    try:
        if isinstance(payload, model):
            # Re-validate so instances built with model_construct() cannot bypass validation.
            return model.model_validate(payload.model_dump(warnings=False))
        return model.model_validate({} if payload is None else payload)
    except ValidationError as exc:
        raise InvalidJobPayloadError(
            handler.name, "validation failed", exc.errors(include_url=False, include_context=False)
        ) from None


def serialize_payload(handler: JobHandler[Any], payload: JobPayload) -> dict[str, Any]:
    """Convert a validated payload into plain JSON data suitable for JSONB."""
    data = payload.model_dump(mode="json")
    try:
        # Rejects values PostgreSQL JSONB cannot store (NaN, Infinity) and guarantees plain JSON types.
        return json.loads(json.dumps(data, allow_nan=False))
    except ValueError as exc:
        raise InvalidJobPayloadError(handler.name, f"payload is not JSON-serializable: {exc}") from None


def _interval_seconds(name: str, interval: timedelta | int) -> int:
    if isinstance(interval, timedelta):
        seconds = interval.total_seconds()
        if seconds != int(seconds):
            raise JobRegistrationError(f"Interval for schedule {name!r} must be a whole number of seconds")
        seconds = int(seconds)
    elif isinstance(interval, int) and not isinstance(interval, bool):
        seconds = interval
    else:
        raise JobRegistrationError(
            f"Interval for schedule {name!r} must be a timedelta or a number of seconds (cron is not supported)"
        )
    if not 1 <= seconds <= MAX_SCHEDULE_INTERVAL_SECONDS:
        raise JobRegistrationError(
            f"Interval for schedule {name!r} must be between 1 and {MAX_SCHEDULE_INTERVAL_SECONDS} seconds"
        )
    return seconds


class JobRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler[Any]] = {}
        self._schedules: dict[str, ScheduleDefinition] = {}

    def register(
        self,
        name: str,
        func: HandlerFunc[PayloadT],
        *,
        payload: type[PayloadT],
        max_attempts: int | None = None,
        lease_seconds: int | None = None,
    ) -> JobHandler[PayloadT]:
        if not isinstance(name, str) or len(name) > MAX_JOB_TYPE_LENGTH or not JOB_TYPE_PATTERN.fullmatch(name):
            raise JobRegistrationError(f"Invalid job type name {name!r}: expected '<module>.<action>' in lowercase")
        if name in self._handlers:
            raise JobRegistrationError(f"Job type {name!r} is already registered")
        if not callable(func):
            raise JobRegistrationError(f"Handler for {name!r} is not callable")
        if not (isinstance(payload, type) and issubclass(payload, JobPayload)):
            raise JobRegistrationError(f"Payload model for {name!r} must subclass JobPayload")
        for label, value in (("max_attempts", max_attempts), ("lease_seconds", lease_seconds)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise JobRegistrationError(f"{label} for {name!r} must be a positive integer")

        handler = JobHandler(
            name=name, func=func, payload_model=payload, max_attempts=max_attempts, lease_seconds=lease_seconds
        )
        self._handlers[name] = handler
        return handler

    def handler(
        self,
        name: str,
        *,
        payload: type[PayloadT],
        max_attempts: int | None = None,
        lease_seconds: int | None = None,
    ) -> Callable[[HandlerFunc[PayloadT]], HandlerFunc[PayloadT]]:
        """Decorator form of :meth:`register`; returns the function unchanged."""

        def decorator(func: HandlerFunc[PayloadT]) -> HandlerFunc[PayloadT]:
            self.register(name, func, payload=payload, max_attempts=max_attempts, lease_seconds=lease_seconds)
            return func

        return decorator

    def get(self, name: str) -> JobHandler[Any]:
        try:
            return self._handlers[name]
        except (KeyError, TypeError):
            raise UnknownJobTypeError(str(name)) from None

    def __contains__(self, name: object) -> bool:
        return name in self._handlers

    def names(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def register_schedule(
        self,
        name: str,
        job_type: str,
        *,
        interval: timedelta | int,
        payload: JobPayload | Mapping[str, Any] | None = None,
    ) -> ScheduleDefinition:
        """Run ``job_type`` every ``interval`` (a timedelta or whole seconds).

        The job type must already be registered in this registry; the payload is validated now.
        """
        if (
            not isinstance(name, str)
            or len(name) > MAX_SCHEDULE_NAME_LENGTH
            or not SCHEDULE_NAME_PATTERN.fullmatch(name)
        ):
            raise JobRegistrationError(f"Invalid schedule name {name!r}: expected lowercase, dot-separated words")
        if name in self._schedules:
            raise JobRegistrationError(f"Schedule {name!r} is already registered")
        if job_type not in self._handlers:
            raise JobRegistrationError(f"Schedule {name!r} refers to unregistered job type {job_type!r}")
        seconds = _interval_seconds(name, interval)
        handler = self._handlers[job_type]
        try:
            data = serialize_payload(handler, validate_payload(handler, payload))
        except InvalidJobPayloadError as exc:
            raise JobRegistrationError(f"Invalid payload for schedule {name!r}: {exc.message}") from exc

        schedule = ScheduleDefinition(name=name, job_type=job_type, interval_seconds=seconds, payload=data)
        self._schedules[name] = schedule
        return schedule

    def schedules(self) -> tuple[ScheduleDefinition, ...]:
        """Registered schedules, ordered by name."""
        return tuple(self._schedules[name] for name in sorted(self._schedules))


# The production registry. Intentionally empty until modules register real handlers.
default_registry = JobRegistry()
job_handler = default_registry.handler
