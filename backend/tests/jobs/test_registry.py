import pytest
from pydantic import BaseModel

from app.modules.jobs.errors import JobRegistrationError, UnknownJobTypeError
from app.modules.jobs.registry import JobContext, JobPayload, JobRegistry, default_registry


class EmptyPayload(JobPayload):
    pass


def noop(context: JobContext, payload: EmptyPayload) -> None:
    pass


@pytest.fixture
def registry() -> JobRegistry:
    return JobRegistry()


def test_register_and_get(registry: JobRegistry) -> None:
    handler = registry.register("tests.noop", noop, payload=EmptyPayload, max_attempts=3, lease_seconds=60)

    assert registry.get("tests.noop") is handler
    assert handler.func is noop
    assert handler.payload_model is EmptyPayload
    assert (handler.max_attempts, handler.lease_seconds) == (3, 60)
    assert "tests.noop" in registry
    assert registry.names() == {"tests.noop"}


def test_overrides_default_to_none(registry: JobRegistry) -> None:
    handler = registry.register("tests.noop", noop, payload=EmptyPayload)

    assert handler.max_attempts is None
    assert handler.lease_seconds is None


def test_decorator_registers_and_returns_function_unchanged(registry: JobRegistry) -> None:
    @registry.handler("tests.decorated", payload=EmptyPayload, max_attempts=2)
    def decorated(context: JobContext, payload: EmptyPayload) -> None:
        pass

    assert registry.get("tests.decorated").func is decorated
    assert registry.get("tests.decorated").max_attempts == 2


def test_duplicate_registration_is_rejected(registry: JobRegistry) -> None:
    registry.register("tests.noop", noop, payload=EmptyPayload)

    with pytest.raises(JobRegistrationError, match="already registered"):
        registry.register("tests.noop", noop, payload=EmptyPayload)
    with pytest.raises(JobRegistrationError, match="already registered"):
        registry.handler("tests.noop", payload=EmptyPayload)(noop)


def test_registries_are_independent(registry: JobRegistry) -> None:
    first = registry.register("tests.noop", noop, payload=EmptyPayload)
    other = JobRegistry()

    # The same name may be registered in a separate registry without affecting the first.
    second = other.register("tests.noop", noop, payload=EmptyPayload, max_attempts=2)
    assert registry.get("tests.noop") is first
    assert other.get("tests.noop") is second
    assert "tests.noop" not in JobRegistry()


@pytest.mark.parametrize(
    "name",
    ["", "noop", "Tests.noop", "tests.", ".noop", "tests..noop", "tests.no-op", "tests.noop ", "1tests.noop",
     "tests." + "a" * 255],
)  # fmt: skip
def test_invalid_names_are_rejected(registry: JobRegistry, name: str) -> None:
    with pytest.raises(JobRegistrationError, match="Invalid job type name"):
        registry.register(name, noop, payload=EmptyPayload)


def test_payload_model_must_subclass_job_payload(registry: JobRegistry) -> None:
    class PlainModel(BaseModel):
        pass

    with pytest.raises(JobRegistrationError, match="must subclass JobPayload"):
        registry.register("tests.noop", noop, payload=PlainModel)  # type: ignore[type-var]
    with pytest.raises(JobRegistrationError, match="must subclass JobPayload"):
        registry.register("tests.noop", noop, payload=dict)  # type: ignore[type-var]


def test_handler_must_be_callable(registry: JobRegistry) -> None:
    with pytest.raises(JobRegistrationError, match="not callable"):
        registry.register("tests.noop", "app.modules.jobs.noop", payload=EmptyPayload)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["max_attempts", "lease_seconds"])
@pytest.mark.parametrize("value", [0, -1, 1.5, True, "3"])
def test_overrides_must_be_positive_integers(registry: JobRegistry, field: str, value: object) -> None:
    with pytest.raises(JobRegistrationError, match=field):
        registry.register("tests.noop", noop, payload=EmptyPayload, **{field: value})  # type: ignore[arg-type]


def test_failed_registration_leaves_registry_unchanged(registry: JobRegistry) -> None:
    with pytest.raises(JobRegistrationError):
        registry.register("tests.noop", noop, payload=EmptyPayload, max_attempts=0)

    assert "tests.noop" not in registry


@pytest.mark.parametrize("name", ["tests.missing", "", None, "os.system"])
def test_unknown_job_type_is_rejected(registry: JobRegistry, name: object) -> None:
    registry.register("tests.noop", noop, payload=EmptyPayload)

    with pytest.raises(UnknownJobTypeError):
        registry.get(name)  # type: ignore[arg-type]


def test_unknown_job_type_error_details() -> None:
    error = UnknownJobTypeError("tests.missing")

    assert error.code == "unknown_job_type"
    assert error.details == {"job_type": "tests.missing"}


def test_job_payload_rejects_unknown_fields() -> None:
    class Payload(JobPayload):
        value: int

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        Payload.model_validate({"value": 1, "unexpected": 2})


def test_production_registry_is_empty() -> None:
    assert default_registry.names() == frozenset()
