# TraceIQ

**AI model and LLM observability platform.**

TraceIQ gives engineering teams visibility into LLM applications, RAG pipelines, and machine learning models in production: trace every step, measure quality, track cost, and detect drift — in one place.

> **Project status: early development (M1 – Foundation in progress).** Implemented and tested: the backend and worker skeleton, background jobs, authentication (sign-in, refresh-token rotation, CSRF and Origin protection), and projects with roles, ownership transfer, retention settings and API-key management, and the file storage abstraction. Still outstanding in M1: CI. The observability features below are not implemented yet; trace ingestion, the first use of API keys, arrives in M2. See the [roadmap](docs/ROADMAP.md).

## Features

All features below are **planned**; none is implemented yet.

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

### Storage configuration
Files such as reference datasets and model artifacts are stored on the local filesystem through `core/storage`, never in PostgreSQL. No module stores files yet; the first will be ML monitoring (M6).

- `TRACEIQ_STORAGE_ROOT` is the storage directory. Outside production it defaults to `data/storage`, relative to the backend's working directory (git-ignored). In production it must be an absolute path; otherwise the API, worker and `migrate` refuse to start.
- Under Docker Compose it is always `/data/storage`, the mount point of the named volume `storage-data`. The `api` and `worker` services share that volume, so both see the same files, and the files persist when containers are recreated. `docker compose down` removes containers and networks but keeps named volumes, so stored files survive it.
- **Warning:** `docker compose down -v` also removes the named volumes: `storage-data`, which holds every stored file, **and** `postgres-data`, which holds the PostgreSQL database. Running it permanently deletes both your stored files and your database contents.
- `TRACEIQ_STORAGE_ROOT` is not in `.env.example`, and Compose sets it directly, so setting it in `.env` has no effect under Docker. It takes effect only when the backend runs outside Docker.

Details are in [docs/ARCHITECTURE.md §9](docs/ARCHITECTURE.md).

### Projects API
Projects are under `/api/v1/projects`. Every request needs a bearer access token, and every write also needs an allowed Origin.

| Endpoint | Who |
|---|---|
| `POST /projects` | any signed-in user; the creator becomes the Owner |
| `GET /projects` | lists your projects; `limit` up to 100, plus `offset` |
| `GET /projects/{id}` | any member |
| `PATCH /projects/{id}` | Owner or Admin |
| `DELETE /projects/{id}` | the Owner only; the body must be `{"confirm_name": "<exact project name>"}` |
| `GET /projects/{id}/members` | any member |
| `POST /projects/{id}/members` | Owner or Admin; `{email, role}` adds an existing registered user as `admin` or `member` |
| `PATCH /projects/{id}/members/{user_id}` | Owner or Admin; never the Owner's own membership |
| `DELETE /projects/{id}/members/{user_id}` | Owner or Admin removes others; any member except the Owner can leave |
| `POST /projects/{id}/ownership-transfer` | the Owner only; `{user_id}` of an active member, who becomes the Owner while you become an Admin |
| `GET /projects/{id}/retention` | any member; effective periods, overrides and configured defaults |
| `PATCH /projects/{id}/retention` | Owner or Admin; partial update, `null` resets a period to its default |
| `POST /projects/{id}/api-keys` | Owner or Admin; `{name}` creates a key and returns it once |
| `GET /projects/{id}/api-keys` | Owner or Admin; metadata only, revoked keys included |
| `POST /projects/{id}/api-keys/{key_id}/revoke` | Owner or Admin; permanent, and repeating it changes nothing |

Non-members get 404, as if the project did not exist. The Owner must transfer ownership before leaving. An Owner whose account is deactivated cannot transfer ownership; administrative recovery is not implemented yet. See [docs/ARCHITECTURE.md §7.3](docs/ARCHITECTURE.md).

#### Retention
Defaults come from the `TRACEIQ_RETENTION_*` settings in `core/config`: traces 30 days, evaluation results 90, prediction logs 30, incidents 180 after resolution, investigation reports 180 after their last update. A project can override each one with a whole number of days from 1 to 3650, for example `{"traces_days": 7, "prediction_logs_days": null}`. Only the fields you send change, `null` removes that override, and an empty body is rejected; send every field as `null` to reset them all.

Both retention endpoints return `effective` (the periods that apply to this project), `overrides` (the project's own values, `null` where none is set), `defaults` (the configured `TRACEIQ_RETENTION_*` values, the same for every project) and `model_artifacts: "until_manually_deleted"`. Model artifacts are kept until manually deleted and can't be overridden. Automatic deletion jobs are planned and not running yet.

Docker Compose doesn't pass `TRACEIQ_RETENTION_*` variables to the containers yet, and they aren't in `.env.example`, so under Docker the defaults above always apply; setting them in `.env` has no effect. They take effect only when the backend runs with those variables in its own environment.

#### API keys
API keys will authenticate ingestion clients once the ingestion API exists (M2); today you can create, list and revoke them.

- **Shown once.** The creation response (sent with `Cache-Control: no-store`) is the only place the full key ever appears. Copy it then: it can't be retrieved later. A lost key must be revoked and replaced.
- **Stored hashed.** Keys look like `tiq_` plus 43 random characters (256 bits). TraceIQ stores only their SHA-256 hash and a 12-character prefix for telling keys apart; listings never include the key or its hash.
- **Revocation** is immediate and permanent. Revoked keys stay listed with `is_active: false`.
- **Limit:** 25 active keys per project; revoked keys don't count.
- Members can't see or manage keys. Key names are trimmed and must be 1–100 characters.

## Documentation

| Document | Contents |
|---|---|
| [docs/PRD.md](docs/PRD.md) | Product requirements and core features |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design and data model |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Milestones and status |
| [CLAUDE.md](CLAUDE.md) | Development rules and coding standards |

## License

Released under the [MIT License](LICENSE).
