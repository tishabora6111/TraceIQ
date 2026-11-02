# TraceIQ – Architecture

**Status:** Approved design · **Implementation status:** Not started

## 1. Architectural Style

TraceIQ is a **modular monolith**: one backend codebase and one database, internally divided into modules with explicit boundaries. The codebase runs as two processes, an **API server** and a **worker**, which keeps deployment and operations simple while preserving the option to extract a module later if scale requires it.

```
                    ┌──────────────────────────────┐
  LLM / RAG apps ──▶│                              │
  ML services   ──▶ │   FastAPI backend (/api/v1)  │◀── React frontend
                    │                              │    (JWT + refresh cookie)
                    │  core · auth · projects      │
                    │  tracing · providers         │
                    │  rag_eval · incidents        │
                    │  investigations              │
                    │  ml_monitoring               │
                    │  explainability · alerts     │
                    │  jobs                        │
                    └──────┬────────────────┬──────┘
                           │ SQLAlchemy     │ storage abstraction
                    ┌──────▼──────┐   ┌─────▼──────────┐
                    │ PostgreSQL  │   │ Local files    │
                    │ (+ jobs)    │   └─────▲──────────┘
                    └──────▲──────┘         │
                           │                │
                    ┌──────┴────────────────┴──────┐
                    │ Worker process (same code)   │
                    └──────────────────────────────┘
```

## 2. System Components

| Component | Technology | Responsibility |
|---|---|---|
| API server | Python 3.11, FastAPI, Pydantic | Ingestion, query, auth, and management APIs |
| Worker | Python 3.11, same codebase | Runs jobs: evaluations, incident detection, evidence collection, drift, SHAP, alerts, retention cleanup |
| Persistence | SQLAlchemy, PostgreSQL | All application data and the job queue |
| Migrations | Alembic | Versioned schema changes |
| File storage | Local filesystem via storage abstraction | Reference datasets, model artifacts |
| Analytics / ML | scikit-learn, SciPy, XGBoost, SHAP | Metrics, drift tests, explanations |
| Frontend | React, TypeScript, Vite, Tailwind CSS, Recharts | Dashboard UI and charts |
| Packaging | Docker | Local and deployment runtime |
| CI | GitHub Actions, Pytest | Tests and checks on every PR |

No Redis, Celery, or message broker is used initially; PostgreSQL serves as the job queue.

## 3. Repository Layout (planned)

```
backend/
  app/
    main.py              # FastAPI app factory, router registration
    worker.py            # worker process entry point
    core/                # config, db session, logging, errors, security, storage
    modules/
      auth/
      projects/
      tracing/
      providers/
      rag_eval/
      incidents/
      investigations/
      ml_monitoring/
      explainability/
      alerts/
      jobs/
  alembic/               # migration environment and versions
  tests/                 # mirrors app/modules/
frontend/
  src/
    api/                 # typed API client
    features/            # one folder per product area
    components/          # shared UI components
sdk/python/              # Python SDK (later milestone)
docs/
docker-compose.yml
.github/workflows/
```

Each backend module follows the same internal layout:

```
modules/<name>/
  api.py          # FastAPI router (HTTP only)
  schemas.py      # Pydantic request/response models
  service.py      # business logic; the module's public interface
  repository.py   # SQLAlchemy queries
  models.py       # SQLAlchemy ORM tables owned by this module
  jobs.py         # job handlers registered with the worker (if any)
```

## 4. Module Boundaries

| Module | Owns | Depends on |
|---|---|---|
| `core` | Config, DB session, logging, error types, password hashing (pwdlib), API-key hashing, JWT encode/decode (PyJWT), CSRF token signing, Origin validation, storage abstraction | — |
| `auth` | Users, login, refresh tokens (rotation, revocation), current-user dependency | core |
| `projects` | Projects, memberships and roles, project-role authorization dependency, API keys, retention settings | core, auth |
| `jobs` | Job table, enqueue API, worker loop, retries, job status API | core |
| `tracing` | Traces, spans, retrieved documents; usage/cost aggregation; trace retention cleanup | projects, providers (pricing), jobs |
| `providers` | LLM provider interface, provider adapters, model pricing | core |
| `rag_eval` | Eval datasets, items, runs, results; RAG metrics | tracing, providers, jobs |
| `incidents` | Incident detection, grouping (signals → incidents), status and lifecycle | projects, tracing, rag_eval, jobs; ml_monitoring (from M7) |
| `investigations` | Investigation reports: evidence snapshots, observed facts, hypotheses, recommended actions, AI drafting, review/approval, revision history | projects, incidents, tracing, rag_eval, providers, jobs |
| `ml_monitoring` | ML models, versions, reference data, prediction logs, drift and performance reports | projects, jobs |
| `explainability` | SHAP explanations | ml_monitoring, jobs |
| `alerts` | Alert rules, alert events; opens or attaches to incidents | tracing, rag_eval, ml_monitoring, incidents (via services), jobs |

**Rules**
- Cross-module calls go through `service.py` only. No cross-module imports of `models.py` or `repository.py`.
- Dependencies point in one direction as listed; cycles are not allowed.
- A module writes only to its own tables, including during retention cleanup.
- `providers` is the only place that imports vendor LLM SDKs or calls provider HTTP APIs.
- File access goes only through `core/storage`.

## 5. Data Flow

### 5.1 LLM trace ingestion
1. Client sends `POST /api/v1/traces` with trace and spans, authenticated by project API key.
2. `projects` resolves the key to a project.
3. `tracing` validates the payload (Pydantic), computes cost via `providers` pricing, and persists trace, spans, and retrieved documents in one transaction.
4. Response returns immediately; no evaluation runs in the request.

### 5.2 RAG evaluation
1. User starts an evaluation run (dataset or sampled traces) via API.
2. `rag_eval` creates a run and enqueues a job in `jobs`.
3. The worker claims the job and executes the run: retrieval metrics computed locally; LLM-as-judge metrics call models through `providers`.
4. Per-item results and aggregates are stored; run and job status are updated.

### 5.3 Incidents
1. A recurring detection job runs built-in detectors over LLM/RAG signals read through module services: error-rate spikes, latency regressions, cost spikes, and evaluation score drops. Thresholds are project settings with configured defaults.
2. Each detected signal gets a **fingerprint** (project, detector, scope such as model or trace name). A signal whose fingerprint matches an open incident is attached to it (updating `last_seen_at` and `signal_count`); otherwise a new incident is opened.
3. Lifecycle: `open` → `acknowledged` → `investigating` → `resolved`. A resolved incident whose fingerprint recurs within the configured reopen window returns to `open`. Every status change is recorded with actor and timestamp.
4. Later milestones add signal sources: drift reports (M7) and user-defined alert rules (M9).

### 5.4 Investigation reports
1. A user creates a report for an incident.
2. **Evidence snapshot:** an evidence-collection job copies the incident's linked traces, spans, evaluation results, and metric windows into `investigation_evidence`, each item with a stable ID. Evidence is copied, not referenced, so reports remain readable after source data expires (traces are kept 30 days; reports 180 days). Snapshot items are immutable; re-collection adds items and a new revision.
3. **Observed facts** (`kind = fact`) are derived from evidence by the system or written by users, and must cite evidence.
4. **AI drafting (optional):** a drafting job sends only the evidence snapshot and observed facts to an LLM through `providers` and asks for hypotheses and recommended actions, each citing evidence IDs. The job validates the output before saving:
   - every item must cite at least one evidence ID that exists in this report's snapshot; uncited or invalid items are discarded;
   - AI hypotheses are saved with `source = ai` and `status = proposed`;
   - AI output can never set a hypothesis to `confirmed` or label it a root cause.
5. **Review:** users edit, add, or delete any item and set hypothesis status. Only a user action can set `confirmed`; only confirmed hypotheses are shown as root causes. The UI labels every item by kind (fact / hypothesis / recommended action) and source (system / AI / user).
6. **Approval:** report status `draft` → `in_review` → `approved`. Approval records approver and time; approved reports are read-only. Returning to `draft` clears the approval and creates a new revision.
7. **Revision history:** every evidence collection, AI draft, edit, and status change writes an `investigation_report_revisions` row with actor (user or system), timestamp, change type, a content snapshot, and for AI drafts the provider, model, prompt version, and token usage.

### 5.5 ML monitoring
1. Client registers a model version; the reference dataset and model artifact are saved through the storage abstraction.
2. Client logs predictions via `POST /api/v1/models/{id}/predictions`.
3. Scheduled or on-demand drift jobs compare a prediction window against the reference using SciPy tests and PSI; performance metrics use scikit-learn when ground truth exists.
4. `explainability` computes SHAP values for supported model artifacts as jobs.

### 5.6 Alerting
The worker periodically runs an alert-evaluation job that checks rules against metrics exposed by module services, records alert events, and opens or attaches to incidents through the `incidents` service.

### 5.7 Retention
A scheduled job per data-owning module deletes records older than the project's retention period. Global defaults are set in configuration; projects may override them.

| Data | Owner module | Default | Measured from |
|---|---|---|---|
| Traces (with spans, retrieved documents) | `tracing` | 30 days | `started_at` |
| Evaluation results | `rag_eval` | 90 days | `created_at` |
| Prediction logs | `ml_monitoring` | 30 days | `timestamp` |
| Incidents (with signals, status history) | `incidents` | 180 days | `resolved_at`; unresolved incidents are never deleted |
| Investigation reports | `investigations` | 180 days | `updated_at` |
| Model artifacts | `ml_monitoring` | Until manually deleted | — |

### 5.8 Frontend
The React app signs in with email/password, holds the access token in memory, and sends it as a bearer token on every request to `/api/v1`. On page load or token expiry it calls the refresh endpoint, which uses the refresh-token cookie and CSRF header (see §7). No direct database access and no server-side rendering. Charts use Recharts.

## 6. Background Jobs

- **Queue:** `jobs` table in PostgreSQL. Producers enqueue jobs inside their own transaction.
- **Worker:** separate Python process (`backend/app/worker.py`) using the same codebase and database. Multiple workers may run safely.
- **Claiming:** `SELECT … FOR UPDATE SKIP LOCKED` on queued jobs whose `run_at` has passed.
- **Statuses:** `queued` → `running` → `succeeded` | `failed`. A failed attempt with attempts remaining returns to `queued` with a backoff `run_at`; after `max_attempts` the job stays `failed` with the last error recorded.
- **Stale jobs:** jobs left `running` past a lease timeout (e.g. worker crash) are requeued.
- **Handlers:** registered by job type in each module's `jobs.py`; must be idempotent.
- **Scheduling:** recurring jobs (incident detection, drift, alerts, retention) are enqueued by the worker on a fixed interval.

## 7. Authentication and Security

### 7.1 Authentication
- **Users:** email/password. Passwords hashed with Argon2id via pwdlib; plaintext never stored or logged.
- **Access tokens:** JWTs created and verified with PyJWT, signed with a secret from configuration, 15-minute lifetime. Held in frontend memory only; sent as `Authorization: Bearer`.
- **Refresh tokens:** opaque random tokens, 7-day lifetime, stored hashed in `refresh_tokens`. Each use rotates the token (old one revoked, new one issued in the same family). Reuse of a revoked token revokes the whole family. Logout and password change revoke server-side.
- **Ingestion API:** per-project API keys, stored hashed and shown once at creation.
- **Python SDK** (later milestone) authenticates with project API keys over the same REST API.

### 7.2 Cookies and CSRF
- **Refresh cookie:** `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/api/v1/auth`. Never readable by JavaScript and never sent to other API paths.
- **CSRF token (signed double-submit):** issued at login and on each refresh as an HMAC-signed value bound to the refresh-token family, set in a separate non-HttpOnly `Secure`, `SameSite=Lax` cookie. The frontend sends it in the `X-CSRF-Token` header. The server requires header and cookie to match and the signature to verify. Required on all cookie-authenticated endpoints (refresh, logout).
- **Origin validation:** the `Origin` header (falling back to `Referer`) is checked against a configured allowlist on login, refresh, logout, and all JWT-authenticated state-changing requests (`POST`, `PUT`, `PATCH`, `DELETE`). API-key ingestion endpoints are exempt, as they serve non-browser clients.
- **Deployment assumption:** frontend and API are served from the same site. A cross-site deployment would need `SameSite=None` (with `Secure`), credentialed CORS, and a review of the CSRF controls; the SameSite policy is therefore configurable.

### 7.3 Authorization
Project roles are stored in `project_members` and enforced by the shared project-role dependency on every project-scoped endpoint. Each project has exactly one Owner.

| Capability | Owner | Admin | Member |
|---|:-:|:-:|:-:|
| View project data (traces, evaluations, models, metrics) | ✓ | ✓ | ✓ |
| Trace and evaluation operations (datasets, evaluation runs) | ✓ | ✓ | ✓ |
| Incidents and investigation reports (view, update status, run AI drafting, edit, approve) | ✓ | ✓ | ✓ |
| Project settings (retention, API keys, detector thresholds) | ✓ | ✓ | — |
| Member management (invite, remove, assign Admin/Member) | ✓ | ✓ | — |
| Delete project | ✓ | — | — |
| Transfer ownership | ✓ | — | — |

Admins cannot change or remove the Owner.

## 8. LLM Provider Abstraction

```
LLMProvider (interface)
  complete(request: LLMRequest) -> LLMResponse
  name, supported_models

LLMRequest:  model, messages, parameters
LLMResponse: text, input_tokens, output_tokens, latency_ms, raw_metadata
```

- Adapters are registered by name and selected by configuration.
- Model pricing (per input/output token) is stored in configuration and used by `tracing` for cost calculation.
- Tests use a deterministic fake provider.

## 9. Storage Abstraction

```
Storage (interface)
  save(key, data) -> uri
  open(uri) -> stream
  delete(uri)
```

- Initial implementation: local filesystem under a configured root directory (a Docker volume).
- Database rows store the returned `uri`, never absolute paths, so the backend can be swapped later without schema changes.

## 10. Database Design

PostgreSQL, single schema, managed with Alembic. UUID primary keys; `created_at` timestamps in UTC on all tables. `JSONB` only for variable attributes.

### 10.1 Platform
| Table | Key columns |
|---|---|
| `users` | id, email (unique), password_hash, is_active, created_at, last_login_at |
| `refresh_tokens` | id, user_id → users, family_id, token_hash, expires_at, revoked_at, replaced_by_id, created_at |
| `projects` | id, name, description, retention_overrides JSONB (null = config defaults), created_at |
| `project_members` | project_id → projects, user_id → users, role (owner / admin / member), created_at; PK (project_id, user_id) |
| `api_keys` | id, project_id → projects, key_hash, prefix, revoked_at |
| `jobs` | id, type, status, payload JSONB, attempts, max_attempts, run_at, locked_at, last_error, created_at, finished_at |

### 10.2 LLM / RAG
| Table | Key columns |
|---|---|
| `traces` | id, project_id, name, status, started_at, ended_at, total_tokens, total_cost, tags, metadata JSONB |
| `spans` | id, trace_id, parent_span_id, type, name, status, started_at, ended_at, provider, model, input, output, input_tokens, output_tokens, cost, error, attributes JSONB |
| `retrieved_documents` | id, span_id, rank, document_ref, content, score, metadata JSONB |
| `eval_datasets` | id, project_id, name, description |
| `eval_items` | id, dataset_id, question, expected_answer, reference_context JSONB |
| `eval_runs` | id, project_id, dataset_id (nullable), source, judge_model, status, aggregate_scores JSONB, job_id |
| `eval_results` | id, run_id, item_id / trace_id, metric, score, explanation |

### 10.3 Incidents and Investigations
| Table | Key columns |
|---|---|
| `incidents` | id, project_id → projects, title, detector, fingerprint, severity (low / medium / high / critical), status (open / acknowledged / investigating / resolved), assignee_id → users (nullable), first_seen_at, last_seen_at, signal_count, acknowledged_at, resolved_at, created_at, updated_at |
| `incident_signals` | id, incident_id → incidents, source_type (trace / span / eval_result / drift_report / alert_event), source_id, observed_at, value, details JSONB |
| `incident_status_history` | id, incident_id → incidents, from_status, to_status, actor_id → users (null = system), note, created_at |
| `investigation_reports` | id, project_id → projects, incident_id → incidents (nullable, `SET NULL` on delete), title, status (draft / in_review / approved), summary, current_revision, created_by → users, created_at, updated_at, evidence_collected_at, approved_by → users, approved_at |
| `investigation_evidence` | id, report_id → investigation_reports, source_type (trace / span / eval_result / metric_window / incident_signal), source_id, captured_at, content JSONB |
| `investigation_report_items` | id, report_id → investigation_reports, kind (fact / hypothesis / recommendation), source (system / ai / user), content, status (hypotheses only: proposed / confirmed / rejected), evidence_ids UUID[] (non-empty for `ai` items), related_item_ids UUID[], created_by → users (nullable), created_at, updated_at, updated_by → users |
| `investigation_report_revisions` | id, report_id → investigation_reports, revision, change_type (evidence_collected / ai_draft / edit / status_change), actor_id → users (null = system), provider, model, prompt_version, input_tokens, output_tokens, job_id, content_snapshot JSONB, created_at |

`incident_signals.source_id` and `investigation_evidence.source_id` are soft references, since source records may expire first. Signals and status history are deleted with their incident; evidence, items, and revisions are deleted with their report.

### 10.4 ML
| Table | Key columns |
|---|---|
| `ml_models` | id, project_id, name, task_type (classification / regression) |
| `ml_model_versions` | id, model_id, version, feature_schema JSONB, artifact_uri |
| `reference_datasets` | id, model_version_id, row_count, storage_uri |
| `prediction_logs` | id, model_version_id, timestamp, features JSONB, prediction, ground_truth |
| `drift_reports` | id, model_version_id, window_start, window_end, overall_score, status |
| `feature_drift` | id, report_id, feature, test, statistic, p_value, psi, drifted |
| `performance_reports` | id, model_version_id, window_start, window_end, metrics JSONB |
| `explanations` | id, model_version_id, scope (global / local), prediction_id, values JSONB |

### 10.5 Alerts
| Table | Key columns |
|---|---|
| `alert_rules` | id, project_id, metric, comparator, threshold, window, enabled |
| `alert_events` | id, rule_id, triggered_at, value, resolved_at, incident_id → incidents (nullable) |

### 10.6 Indexing
- Foreign keys indexed.
- `traces (project_id, started_at)`, `spans (trace_id)`, `prediction_logs (model_version_id, timestamp)`, `jobs (status, run_at)`, `users (email)` unique, `refresh_tokens (token_hash)` unique, `refresh_tokens (family_id)`.
- `incidents (project_id, status, last_seen_at)`, `incidents (project_id, fingerprint)`, `incidents (resolved_at)`, `investigation_reports (project_id, updated_at)`, `investigation_report_items (report_id, kind)`, `investigation_report_revisions (report_id, revision)` unique.
- A partial unique index on `project_members (project_id) WHERE role = 'owner'` enforces a single Owner.
- Retention deletes rely on the time indexes above. Partitioning of `spans` and `prediction_logs` by time is deferred until data volume requires it.

## 11. Cross-Cutting Concerns

- **Configuration:** environment variables, loaded once in `core/config` via Pydantic settings (database URL, JWT and CSRF signing secrets, access/refresh token lifetimes, cookie settings including SameSite policy, allowed origins, storage root, retention defaults per data type, incident detector defaults, investigation drafting provider/model and prompt version, worker poll interval).
- **Errors:** domain exceptions mapped to consistent JSON error responses.
- **Logging:** structured logs; prompts, responses, passwords, tokens, and keys are never logged by default.
- **Testing:** Pytest against PostgreSQL in Docker with Alembic-applied schema; fake LLM provider; temporary directory for storage; CI on GitHub Actions.

## 12. Decisions

| # | Topic | Decision |
|---|---|---|
| D1 | Background jobs | PostgreSQL job table + separate Python worker process with retries and job statuses. No Redis/Celery initially. |
| D2 | Migrations | Alembic. |
| D3 | Authentication | Email/password; JWT for UI and management API; API keys for ingestion. |
| D4 | Integration | REST API first; Python SDK in a later milestone. |
| D5 | File storage | Local filesystem behind a storage abstraction. |
| D6 | Frontend charts | Recharts. |
| D7 | Retention | Configurable per project. Defaults: traces 30 d, evaluation results 90 d, prediction logs 30 d, incidents 180 d after resolution (unresolved never deleted), investigation reports 180 d after last update, model artifacts until manually deleted. Confirmed. |
| D8 | Password hashing | Argon2id using pwdlib. |
| D9 | JWT library | PyJWT. |
| D10 | Token lifetimes | Access token 15 minutes; refresh token 7 days with rotation and server-side revocation. |
| D11 | Frontend token storage | Access token in memory; refresh token in an HttpOnly, Secure, SameSite=Lax cookie with a restricted path. |
| D12 | Project roles | Owner, Admin, Member, with the permissions in §7.3. Exactly one Owner per project. Admins manage project settings (including API keys, retention, and detector thresholds) and members, but cannot change or remove the Owner, delete the project, or transfer ownership. Confirmed. |
| D13 | CSRF | Signed double-submit token in the `X-CSRF-Token` header; Origin validation on state-changing requests; SameSite configurable for cross-site deployments. |
| D14 | Incidents and investigations | `incidents` module owns detection, grouping, status, and lifecycle; `investigations` module owns reports (evidence snapshots, observed facts, hypotheses, recommended actions). Delivered in M5. |
| D15 | Evidence snapshots | Report evidence is copied into the report and immutable, independent of source-data retention. Confirmed. |
| D16 | AI-assisted investigations | An LLM may draft hypotheses and recommended actions via `providers`. Every AI item must cite snapshot evidence; AI hypotheses are always `proposed`; only users confirm root causes. Users edit and approve reports; all generations and updates are recorded as revisions (§5.4). |
| D17 | Notifications | Email, Slack, and webhook notifications are excluded from the initial implementation; post-M9 consideration. |

### Remaining open decisions
| Topic | Notes |
|---|---|
| Approval controls | Whether report approval needs a different user from the author or a minimum role; currently any project role may approve. |
