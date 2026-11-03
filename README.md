# TraceIQ

**AI model and LLM observability platform.**

TraceIQ gives engineering teams visibility into LLM applications, RAG pipelines, and machine learning models in production: trace every step, measure quality, track cost, and detect drift — in one place.

> **Project status: early development.** The architecture and documentation are complete. No application features are implemented yet. See the [roadmap](docs/ROADMAP.md) for progress.

## Features

All features below are **planned**.

### LLM and RAG observability (first)
- **Tracing** – nested traces and spans for LLM calls, retrieval, tools, and chains.
- **Cost and usage analytics** – tokens, cost, latency, and error rates by model and project.
- **Provider abstraction** – a single internal interface across LLM providers.
- **RAG evaluation** – context relevance, faithfulness, answer relevance, and retrieval metrics over datasets or production traces.
- **Incidents** – automatic detection and grouping of LLM/RAG problems with a tracked lifecycle.
- **Investigation reports** – evidence snapshots with clearly separated observed facts, hypotheses, and recommended actions; AI-drafted items must cite recorded evidence, and reports are edited and approved by users.

### ML observability (second)
- **Prediction logging** – per model version, with optional ground truth.
- **Drift detection** – KS, chi-square, and PSI per feature.
- **Performance monitoring** – classification and regression metrics.
- **Explainability** – SHAP global and per-prediction explanations.

### Platform
- **Alerting** on metric thresholds.
- **Web dashboard** for LLM and ML monitoring.

## Architecture

TraceIQ is a **modular monolith**: a single FastAPI backend divided into modules with strict boundaries, run as an API process and a separate worker process, backed by one PostgreSQL database, with a React frontend.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for components, module boundaries, data flow, and database design.

## Tech Stack

| Area | Technology |
|---|---|
| Backend | Python 3.11, FastAPI, Pydantic, SQLAlchemy |
| Background jobs | Separate Python worker process using a PostgreSQL-backed job queue |
| Frontend | React, TypeScript, Vite, Tailwind CSS, Recharts |
| Database | PostgreSQL, Alembic migrations |
| ML | scikit-learn, SciPy, XGBoost, SHAP |
| GenAI | LLM provider abstraction, RAG evaluation |
| Infrastructure | Docker, Pytest, GitHub Actions |

## Setup

Local development uses Docker Compose for the API, worker, frontend, and PostgreSQL. M1 – Foundation is in progress, so expect this to change.

1. Copy `.env.example` to `.env`. Never commit `.env`.
2. Set `POSTGRES_PASSWORD`.
3. Set the two authentication secrets. Generate each one separately:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
   Put one value in `TRACEIQ_JWT_SECRET` and a *different* value in `TRACEIQ_CSRF_SECRET`. The API refuses to start if either is missing, shorter than 32 bytes, a placeholder, or the same as the other. The worker does not need them.
4. Start everything:
   ```bash
   docker compose up -d --build
   ```
   The one-off `migrate` service applies database migrations before `api` and `worker` start.

### Authentication configuration
All settings are in `.env.example` and are passed only to the `api` service.

| Setting | Default | Notes |
|---|---|---|
| `TRACEIQ_JWT_SECRET`, `TRACEIQ_CSRF_SECRET` | *(required)* | At least 32 random bytes each, and different from each other. Changing one signs everyone out. |
| `TRACEIQ_CORS_ORIGINS` | `http://localhost:5173` | Browser origins allowed to call the API. Login, refresh, logout and registration also require the request's Origin to be listed. |
| `TRACEIQ_AUTH_COOKIE_SECURE` | `true` | Must be `true` in production; the API refuses to start otherwise. Chrome and Firefox accept Secure cookies on `http://localhost`. Set `false` only for local HTTP development in browsers that don't. |
| `TRACEIQ_AUTH_COOKIE_SAMESITE` | `lax` | Assumes the frontend and API are on the same site. |
| `TRACEIQ_AUTH_COOKIE_DOMAIN` | *(empty)* | Empty means host-only cookies (recommended). |
| `TRACEIQ_AUTH_REGISTRATION_ENABLED` | *(empty)* | Empty means enabled in development and disabled in production. |
| `TRACEIQ_ACCESS_TOKEN_TTL_SECONDS` | `900` | Access-token lifetime (15 minutes). |
| `TRACEIQ_REFRESH_TOKEN_TTL_SECONDS` | `604800` | Rolling refresh-token lifetime (7 days), renewed on each refresh. |
| `TRACEIQ_REFRESH_TOKEN_ABSOLUTE_TTL_SECONDS` | `2592000` | A session ends 30 days after login, however often it is refreshed. |
| `TRACEIQ_AUTH_MAX_CONCURRENT_HASHES` | `4` | Concurrent Argon2id operations per API process (1–64, 64 MiB each). |
| `TRACEIQ_AUTH_HASH_WAIT_TIMEOUT_SECONDS` | `10` | How long a login waits for hashing capacity before returning 503. |

The auth endpoints are under `/api/v1/auth`: `register`, `login`, `refresh`, `logout` and `me`. Details are in [docs/ARCHITECTURE.md §7](docs/ARCHITECTURE.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/PRD.md](docs/PRD.md) | Product requirements and core features |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design and data model |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Milestones and status |
| [CLAUDE.md](CLAUDE.md) | Development rules and coding standards |

## License

Released under the [MIT License](LICENSE).
