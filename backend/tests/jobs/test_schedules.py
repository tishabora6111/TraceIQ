"""Schedule registration (no database)."""

from datetime import timedelta

import pytest

from app.modules.jobs.errors import JobRegistrationError
from app.modules.jobs.registry import (
    MAX_SCHEDULE_INTERVAL_SECONDS,
    MAX_SCHEDULE_NAME_LENGTH,
    JobContext,
    JobPayload,
    JobRegistry,
    ScheduleDefinition,
    default_registry,
)


class WindowPayload(JobPayload):
    window_minutes: int = 5


def _noop(context: JobContext, payload: WindowPayload) -> None:
    pass


@pytest.fixture
def registry() -> JobRegistry:
    registry = JobRegistry()
    registry.register("tests.detect", _noop, payload=WindowPayload)
    return registry


def test_register_schedule(registry: JobRegistry) -> None:
    schedule = registry.register_schedule("tests.detect_hourly", "tests.detect", interval=timedelta(hours=1))

    assert schedule == ScheduleDefinition(
        name="tests.detect_hourly", job_type="tests.detect", interval_seconds=3600, payload={"window_minutes": 5}
    )
    assert schedule.dedupe_key == "schedule:tests.detect_hourly"
    assert registry.schedules() == (schedule,)


def test_interval_as_seconds_and_payload(registry: JobRegistry) -> None:
    schedule = registry.register_schedule(
        "tests.detect_fast", "tests.detect", interval=30, payload=WindowPayload(window_minutes=1)
    )

    assert schedule.interval_seconds == 30
    assert schedule.payload == {"window_minutes": 1}


def test_schedules_are_ordered_by_name(registry: JobRegistry) -> None:
    registry.register_schedule("tests.b", "tests.detect", interval=60)
    registry.register_schedule("tests.a", "tests.detect", interval=60)

    assert [schedule.name for schedule in registry.schedules()] == ["tests.a", "tests.b"]


def test_duplicate_schedule_name_is_rejected(registry: JobRegistry) -> None:
    registry.register_schedule("tests.detect_hourly", "tests.detect", interval=3600)

    with pytest.raises(JobRegistrationError, match="already registered"):
        registry.register_schedule("tests.detect_hourly", "tests.detect", interval=60)
    assert registry.schedules()[0].interval_seconds == 3600


def test_several_schedules_may_share_a_job_type(registry: JobRegistry) -> None:
    registry.register_schedule("tests.fast", "tests.detect", interval=60, payload={"window_minutes": 1})
    registry.register_schedule("tests.slow", "tests.detect", interval=3600, payload={"window_minutes": 60})

    assert len(registry.schedules()) == 2


@pytest.mark.parametrize(
    "name",
    ["", "Tests.detect", "tests detect", "tests..detect", ".detect", "1detect", "tests.detect*", "a" * 247, None],
)
def test_invalid_schedule_name_is_rejected(registry: JobRegistry, name: object) -> None:
    with pytest.raises(JobRegistrationError, match="Invalid schedule name"):
        registry.register_schedule(name, "tests.detect", interval=60)  # type: ignore[arg-type]


def test_longest_schedule_name_fits_dedupe_key(registry: JobRegistry) -> None:
    schedule = registry.register_schedule("a" * MAX_SCHEDULE_NAME_LENGTH, "tests.detect", interval=60)

    assert len(schedule.dedupe_key) == 255


def test_unregistered_job_type_is_rejected(registry: JobRegistry) -> None:
    with pytest.raises(JobRegistrationError, match="unregistered job type"):
        registry.register_schedule("tests.missing", "tests.missing", interval=60)


@pytest.mark.parametrize(
    "payload",
    [{"window_minutes": "soon"}, {"unexpected": 1}],
)
def test_invalid_payload_is_rejected(registry: JobRegistry, payload: dict[str, object]) -> None:
    with pytest.raises(JobRegistrationError, match="Invalid payload for schedule"):
        registry.register_schedule("tests.detect_hourly", "tests.detect", interval=60, payload=payload)


@pytest.mark.parametrize(
    "interval",
    [0, -1, timedelta(0), timedelta(seconds=-5), MAX_SCHEDULE_INTERVAL_SECONDS + 1],
)
def test_non_positive_or_too_large_interval_is_rejected(registry: JobRegistry, interval: object) -> None:
    with pytest.raises(JobRegistrationError, match="between 1 and"):
        registry.register_schedule("tests.detect_hourly", "tests.detect", interval=interval)  # type: ignore[arg-type]


@pytest.mark.parametrize("interval", ["*/5 * * * *", "0 * * * *", "@hourly", 1.5, True, None])
def test_cron_and_non_integer_intervals_are_rejected(registry: JobRegistry, interval: object) -> None:
    with pytest.raises(JobRegistrationError, match="timedelta or a number of seconds"):
        registry.register_schedule("tests.detect_hourly", "tests.detect", interval=interval)  # type: ignore[arg-type]


def test_fractional_timedelta_is_rejected(registry: JobRegistry) -> None:
    with pytest.raises(JobRegistrationError, match="whole number of seconds"):
        registry.register_schedule("tests.detect_hourly", "tests.detect", interval=timedelta(seconds=1.5))


def test_failed_schedule_registration_leaves_registry_unchanged(registry: JobRegistry) -> None:
    with pytest.raises(JobRegistrationError):
        registry.register_schedule("tests.detect_hourly", "tests.detect", interval=0)

    assert registry.schedules() == ()


def test_production_registry_has_no_schedules() -> None:
    assert default_registry.schedules() == ()
