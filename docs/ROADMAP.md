# TraceIQ – Roadmap

Milestones are delivered in order. LLM/RAG observability comes first; traditional ML observability follows.

**Status legend:** ✅ Done · 🟡 In progress · ⬜ Planned

| # | Milestone | Status |
|---|---|---|
| M0 | Project documentation | ✅ |
| M1 | Foundation | 🟡 |
| M2 | LLM tracing | ⬜ |
| M3 | Provider abstraction and cost analytics | ⬜ |
| M4 | RAG evaluation | ⬜ |
| M5 | LLM dashboard, incidents and investigations | ⬜ |
| M6 | ML model monitoring | ⬜ |
| M7 | Drift and performance | ⬜ |
| M8 | Explainability | ⬜ |
| M9 | Alerting and hardening | ⬜ |
| M10 | Python SDK | ⬜ |

---

## M0 – Project Documentation ✅
- CLAUDE.md, PRD, architecture, roadmap, README.

## M1 – Foundation 🟡
- ✅ Backend skeleton: FastAPI app factory, `core` (config, DB session, errors, logging), health endpoint.
- ✅ Frontend skeleton: Vite + React + TypeScript + Tailwind.
- ✅ PostgreSQL via Docker Compose; Alembic migration environment.
- ✅ `jobs` module and separate worker process: job table, statuses, retries with backoff, stale-job recovery, recurring scheduling.
- ✅ `auth` module:
  - users and email/password login (Argon2id via pwdlib);
  - access-token issuance and validation (PyJWT, 15 minutes);
  - refresh-token rotation and server-side revocation (7 days, reuse detection, logout);
  - refresh cookie (HttpOnly, Secure, SameSite=Lax, restricted path);
  - CSRF protection: signed double-submit token in `X-CSRF-Token`, Origin validation.
- ✅ `projects` module (M1.4): projects, memberships, role-based authorization (Owner, Admin, Member) including ownership transfer, hashed API keys (create, list, revoke; ingestion authentication arrives with M2), retention settings. Migration `0004_projects`.
- ⬜ `core/storage`: storage abstraction with local filesystem implementation.
- ✅ Pytest setup with PostgreSQL.
- ⬜ GitHub Actions CI (lint + tests); no linter is configured yet.

**Exit criteria:** API, worker, and database start locally with Docker; CI green; a user can sign in, refresh, and log out; a reused refresh token is rejected and its family revoked; requests without a valid CSRF token or allowed Origin are rejected; each role is limited to its permissions; a project and API key can be created; a test job runs, fails, and is retried.

**Remaining for M1:** `core/storage` and CI. The other criteria are met: by the test suite, and for the Docker start-up with migration `0004_projects` by an isolated Docker Compose smoke test and the development deployment (database migrated to `0004_projects`, API and worker started).

## M2 – LLM Tracing ⬜
- `tracing` module: traces, spans, retrieved documents.
- `POST /api/v1/traces` ingestion with API-key auth and validation.
- Query endpoints: list/filter traces, trace detail with span tree.
- Trace retention cleanup job (configurable, 30-day default).

**Exit criteria:** a multi-span RAG trace can be ingested and retrieved as a tree; traces older than the retention period are deleted.

## M3 – Provider Abstraction and Cost Analytics ⬜
- `providers` module: `LLMProvider` interface, initial adapter(s), fake provider for tests.
- Model pricing configuration; cost computed at ingestion.
- Aggregation endpoints: tokens, cost, latency percentiles, error rate by model and time.

**Exit criteria:** cost and usage analytics returned correctly for ingested traces.

## M4 – RAG Evaluation ⬜
- `rag_eval` module: datasets, items, runs, results.
- Retrieval metrics (hit rate, MRR).
- LLM-as-judge metrics via `providers`: context relevance, faithfulness, answer relevance.
- Runs executed as background jobs; run comparison endpoint.
- Evaluation result retention cleanup (90-day default).

**Exit criteria:** an evaluation run over a dataset produces stored per-item and aggregate scores.

## M5 – LLM Dashboard, Incidents and Investigations ⬜
- `incidents` module: detectors for error-rate spikes, latency regressions, cost spikes, and evaluation score drops; fingerprint grouping; lifecycle (open → acknowledged → investigating → resolved, reopen on recurrence); status history.
- `investigations` module: reports per incident; immutable evidence snapshots; separate observed facts, hypotheses, and recommended actions; AI drafting via `providers` with evidence-citation validation (AI hypotheses always *proposed*); user editing and approval (draft → in review → approved); revision history with actor and AI model metadata.
- Incident and report retention cleanup (180-day defaults).
- UI: login with in-memory access token, silent refresh, and CSRF header; projects and member management; trace explorer, trace detail, cost/latency analytics (Recharts), evaluation results, incidents, and investigation reports.

**Exit criteria:** all M2–M4 capabilities usable from the UI; an injected error-rate spike opens one grouped incident that moves through its lifecycle; its report has an evidence snapshot, AI-drafted items that all cite evidence (uncited output rejected in tests), and is edited and approved by a user with every change recorded as a revision.

## M6 – ML Model Monitoring ⬜
- `ml_monitoring` module: models, versions, feature schema, reference datasets and artifacts stored via the storage abstraction.
- Prediction logging API with optional delayed ground truth.
- Prediction log retention cleanup (30-day default); model artifacts kept until manually deleted.

**Exit criteria:** predictions logged and queryable per model version.

## M7 – Drift and Performance ⬜
- Feature drift: KS test, chi-square (SciPy), PSI.
- Performance metrics via scikit-learn for classification and regression.
- Scheduled and on-demand drift jobs; reports and UI views.
- Drift reports added as an incident signal source.

**Exit criteria:** a synthetic shifted dataset is correctly flagged as drifted.

## M8 – Explainability ⬜
- `explainability` module: SHAP for tree-based models (XGBoost, scikit-learn).
- Global feature importance and per-prediction explanations; UI views.

**Exit criteria:** SHAP explanations produced for a sample XGBoost model.

## M9 – Alerting and Hardening ⬜
- `alerts` module: threshold rules over LLM, RAG, and ML metrics; alert events in UI, opening or attaching to incidents.
- Content redaction option, performance review of indexes.
- Production Docker images (API and worker) and deployment documentation.

**Exit criteria:** alerts fire on configured thresholds; release candidate documented.

## M10 – Python SDK ⬜
- Thin Python client over the public REST API for trace ingestion and prediction logging, authenticated with project API keys.
- Packaged under `sdk/python/` with its own tests.

**Exit criteria:** an example LLM app and an example ML service report data using only the SDK.

---

## Post-M9 Considerations
Not scheduled; to be evaluated after M9.
- Incident and alert notifications: email, Slack, and webhooks.
