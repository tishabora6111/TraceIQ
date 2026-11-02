FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install .

COPY alembic.ini ./
COPY alembic ./alembic

RUN useradd --create-home --uid 1000 traceiq
USER traceiq

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Test image: runtime plus dev dependencies and tests. Build with `--target test`.
FROM runtime AS test

USER root
RUN pip install ".[dev]"
COPY tests ./tests
USER traceiq

ENV TRACEIQ_ENV=test \
    TRACEIQ_TEST_REQUIRE_DB=1

CMD ["pytest"]

# Default build target.
FROM runtime
