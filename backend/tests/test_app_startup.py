"""The API refuses to start without valid signing secrets; the worker does not need them."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import AuthConfigurationError, Settings
from tests.conftest import TEST_CSRF_SECRET, TEST_JWT_SECRET

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _settings(settings: Settings, **overrides: object) -> Settings:
    return settings.model_copy(update=overrides)


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"jwt_secret": None}, "TRACEIQ_JWT_SECRET is not set"),
        ({"csrf_secret": None}, "TRACEIQ_CSRF_SECRET is not set"),
        ({"jwt_secret": SecretStr("too-short")}, "TRACEIQ_JWT_SECRET must be at least 32 bytes"),
        ({"csrf_secret": SecretStr(TEST_JWT_SECRET)}, "must be different"),
    ],
)
def test_create_app_rejects_invalid_secrets(settings: Settings, overrides: dict[str, object], problem: str) -> None:
    from app.main import create_app

    with pytest.raises(AuthConfigurationError, match=problem) as excinfo:
        create_app(_settings(settings, **overrides))

    for secret in (TEST_JWT_SECRET, TEST_CSRF_SECRET, "too-short"):
        assert secret not in str(excinfo.value)


def test_create_app_accepts_valid_secrets(settings: Settings) -> None:
    from app.main import create_app

    app = create_app(settings)

    assert {"/api/v1/auth/register", "/api/v1/auth/login"} <= set(app.openapi()["paths"])


def _run(code: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND_DIR, env=environment, capture_output=True, text=True, timeout=60
    )


def test_api_module_fails_to_import_without_secrets() -> None:
    # Empty values count as unset and override anything in .env.
    result = _run("import app.main", TRACEIQ_JWT_SECRET="", TRACEIQ_CSRF_SECRET="")

    assert result.returncode != 0
    assert "AuthConfigurationError" in result.stderr
    assert "TRACEIQ_JWT_SECRET is not set" in result.stderr


def test_worker_module_imports_without_secrets() -> None:
    result = _run("import app.worker; print('ok')", TRACEIQ_JWT_SECRET="", TRACEIQ_CSRF_SECRET="")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_empty_origin_allowlist_logs_a_warning(
    settings: Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main
    from app.main import create_app

    # configure_logging() would replace the root handlers, including pytest's capture handler.
    monkeypatch.setattr(app.main, "configure_logging", lambda level: None)

    with caplog.at_level("WARNING"):
        create_app(_settings(settings, cors_origins=[]))

    [warning] = [r for r in caplog.records if r.levelname == "WARNING" and "TRACEIQ_CORS_ORIGINS" in r.getMessage()]
    assert "403" in warning.getMessage()
    for secret in (TEST_JWT_SECRET, TEST_CSRF_SECRET):
        assert secret not in caplog.text


def test_configured_origin_allowlist_logs_no_warning(
    settings: Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main
    from app.main import create_app

    # configure_logging() would replace the root handlers, including pytest's capture handler.
    monkeypatch.setattr(app.main, "configure_logging", lambda level: None)

    with caplog.at_level("WARNING"):
        create_app(_settings(settings, cors_origins=["https://app.traceiq.test"]))

    assert not [r for r in caplog.records if "TRACEIQ_CORS_ORIGINS" in r.getMessage()]
