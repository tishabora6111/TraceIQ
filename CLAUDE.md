# CLAUDE.md

Permanent instructions for working in the TraceIQ repository.

## Project

TraceIQ is an AI model and LLM observability platform, built as a **modular monolith**.
Development order: **LLM/RAG observability first**, traditional ML observability second.

Source of truth:
- Product scope: [docs/PRD.md](docs/PRD.md)
- Architecture and data model: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Milestones and status: [docs/ROADMAP.md](docs/ROADMAP.md)

If a change conflicts with these documents, stop and ask before proceeding.

## Approved Stack

| Area | Technology |
|---|---|
| Backend | Python 3.11, FastAPI, Pydantic, SQLAlchemy |
| Auth | Email/password (Argon2id via pwdlib), JWT via PyJWT, refresh-token rotation |
| Frontend | React, TypeScript, Vite, Tailwind CSS, Recharts |
| Database | PostgreSQL, Alembic migrations |
| Background jobs | PostgreSQL job table + separate Python worker process |
| File storage | Local filesystem behind a storage abstraction |
| ML | scikit-learn, SciPy, XGBoost, SHAP |
| GenAI | Internal LLM provider abstraction, RAG evaluation |
| Infrastructure | Docker, Pytest, GitHub Actions |

Do not add frameworks, services, or infrastructure (Redis, Celery, other queues, caches, message brokers, ORMs, state libraries, chart libraries, etc.) without explicit approval.

## Architecture Rules

1. **One deployable backend.** All backend modules live in a single FastAPI application and share one PostgreSQL database.
2. **Module boundaries are enforced by convention:**
   - Each module lives in `backend/app/modules/<module>/`.
   - Modules call each other only through the other module's `service.py` public functions.
   - A module never imports another module's `models.py`, `repository.py`, or internal helpers.
   - A module owns its tables; no other module writes to them.
3. **Layering inside a module:** `api.py` (router) → `service.py` (business logic) → `repository.py` (SQLAlchemy queries). Routers contain no business logic; services contain no HTTP concerns.
4. **Shared code** goes in `backend/app/core/` (config, database session, logging, errors, security). Keep it small and domain-free.
5. **Pydantic schemas** (`schemas.py`) define every API request/response. Never return ORM objects from routes.
6. **LLM providers** are accessed only through the provider abstraction in the `providers` module. No vendor SDK calls elsewhere.
7. **Heavy computation** (evaluations, drift, SHAP, retention cleanup) runs as jobs in the separate worker process, never in the request path. Job handlers must be idempotent because failed jobs are retried.
8. **Files** (reference datasets, model artifacts) are read and written only through the storage abstraction in `core/storage`; never use direct filesystem paths in modules.
9. **Frontend** talks to the backend only through the versioned REST API (`/api/v1`).
10. **AI-generated content** (investigation drafts) must cite evidence from the report's snapshot; reject uncited output. Keep observed facts, hypotheses, and recommended actions separate and labelled by source. AI output never sets a hypothesis to confirmed or presents it as a root cause; only users confirm and approve.
11. **Integration** is REST API first; the Python SDK is a later milestone and must wrap the public REST API only.

## Coding Standards

### Python
- Python 3.11, full type hints on public functions.
- Pydantic v2 models for validation; SQLAlchemy 2.x style (`select()`, typed `Mapped[]`).
- Format and lint with the project-configured tools once added; do not introduce new ones.
- Raise domain exceptions from `core/errors`; map to HTTP errors in one place.
- No secrets in code; configuration via environment variables loaded in `core/config` (including JWT secret and retention defaults).
- Never store, log, or return plaintext passwords; never log JWTs, refresh tokens, or API keys.
- Hash passwords only with pwdlib (Argon2id); encode/decode JWTs only with PyJWT, via `core/security`.
- Every project-scoped endpoint enforces the caller's project role (Owner, Admin, Member) through the shared authorization dependency; the permission matrix is in ARCHITECTURE.md §7.3.
- Cookie-authenticated endpoints require the signed CSRF token (`X-CSRF-Token`); browser state-changing requests pass Origin validation. Do not weaken cookie flags (HttpOnly, Secure, SameSite=Lax, restricted path) without approval.
- Retention defaults come from `core/config`; never hard-code retention periods in modules.

### TypeScript / React
- Strict TypeScript; no `any` without a comment explaining why.
- Function components and hooks only.
- Tailwind for styling; no additional CSS frameworks.
- Recharts for all charts.
- API types mirror backend Pydantic schemas; keep API calls in `frontend/src/api/`.
- Keep the access token in memory only; never put tokens in `localStorage` or `sessionStorage`. The refresh token lives in an HttpOnly cookie managed by the backend; send the CSRF token in the `X-CSRF-Token` header on refresh and logout.

### Testing
- Pytest for backend. Every service function and API route gets tests.
- Tests live in `backend/tests/<module>/`, mirroring the source layout.
- Tests that need a database use PostgreSQL (via Docker), not SQLite.
- Mock external LLM providers in tests; never call real provider APIs in CI.

### Database
- All schema changes go through Alembic migrations; never edit applied migrations.
- Use `JSONB` only for genuinely variable data (span attributes, metadata). Query-critical fields are real columns.
- Index foreign keys and time columns used for filtering.

## Development Workflow (token-efficient)

1. **Read the relevant doc section first**, not the whole docs folder.
2. **Search before reading:** use Grep/Glob to locate code; read only the files and line ranges needed.
3. **Work within one module per task** where possible. Touching multiple modules signals a boundary question; check ARCHITECTURE.md.
4. **Make small, focused changes.** No speculative abstractions, unrelated refactors, or drive-by reformatting.
5. **Check decisions first:** resolved architecture decisions are recorded in ARCHITECTURE.md §12; do not reopen them without approval.
6. **Run targeted tests** (`pytest backend/tests/<module>`) before the full suite.
7. **Do not re-read files** just edited; trust the edit result.
8. **Update docs in the same change** when behavior, boundaries, or schema change. Mark roadmap items done only when implemented and tested.
9. **Do not** install dependencies, commit, or push unless asked.

## Status Convention

In all documentation, features are labelled:
- **Implemented** – merged, tested, and working.
- **In progress** – partially built.
- **Planned** – designed, not yet built.

Never describe a planned feature as available.
