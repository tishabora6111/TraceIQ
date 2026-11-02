# TraceIQ – Product Requirements Document

**Status:** Approved for development · **Implementation status:** No features implemented yet

## 1. Overview

TraceIQ is an observability platform for AI systems. It gives engineering teams visibility into how LLM applications, RAG pipelines, and traditional machine learning models behave in production: what they did, how well they did it, what it cost, and when quality changes.

Development is phased: **LLM and RAG observability first**, followed by **traditional ML observability**.

## 2. Problem

- LLM applications are non-deterministic and multi-step; failures are hard to reproduce without full traces.
- RAG quality depends on retrieval and generation together; teams lack systematic measurement of either.
- Token usage and cost are spread across providers and are rarely attributed to features or users.
- Deployed ML models degrade silently through data and prediction drift.
- Teams use separate, disconnected tools for LLM and ML monitoring.

## 3. Target Users

| User | Primary need |
|---|---|
| ML / AI engineers | Debug LLM chains and RAG pipelines; inspect individual traces |
| Data scientists | Monitor model drift and performance; understand predictions |
| Platform / MLOps engineers | Track latency, errors, and cost across providers and models |
| Engineering leads | Quality and cost trends; alerting on regressions |

## 4. Goals and Non-Goals

**Goals**
- Capture and explore LLM and RAG traces with minimal integration effort.
- Measure RAG quality with repeatable evaluations.
- Track latency, token usage, cost, and errors per provider and model.
- Detect drift and performance degradation in tabular ML models.
- Explain ML predictions with SHAP.
- Provide one web UI for both LLM and ML observability.

**Non-goals (initial releases)**
- Model training, fine-tuning, or hosting.
- Acting as an LLM gateway or proxy for production traffic.
- Prompt management / versioning as a primary feature.
- Multi-region or horizontally scaled deployment.
- Computer vision or unstructured-data drift monitoring.
- External incident and alert notifications (email, Slack, webhooks) – a post-M9 consideration.

## 5. Core Features

All features below are **Planned**.

### 5.1 LLM Tracing
- Ingest traces and spans over a REST API (the primary integration path; a Python SDK wrapping the REST API follows in a later milestone).
- Span types: LLM call, retrieval, tool call, chain/step, custom.
- Record inputs, outputs, model, provider, parameters, latency, token counts, status, and errors.
- Nested span tree view per trace; search and filter by project, time, model, status, and tags.

### 5.2 LLM Provider Abstraction
- Unified internal interface for calling LLM providers (used by evaluations, e.g. LLM-as-judge).
- Per-model pricing configuration for cost calculation.
- Provider configuration via environment variables; no provider is hard-coded.

### 5.3 Cost, Latency, and Usage Analytics
- Aggregate token usage, cost, latency percentiles, and error rate by project, model, and time window.

### 5.4 RAG Evaluation
- Capture retrieved context (documents, scores) attached to retrieval spans.
- Evaluation datasets of question / expected-answer / reference-context items.
- Metrics: context relevance, faithfulness (groundedness), answer relevance, and retrieval metrics (hit rate, MRR) where ground truth exists.
- Evaluation runs over datasets or sampled production traces; results stored and compared across runs.

### 5.5 Incidents
- Automatic detection of LLM/RAG problems: error-rate spikes, latency regressions, cost spikes, and evaluation score drops, with configurable thresholds.
- Related signals grouped into a single incident instead of many duplicate alerts.
- Severity, assignee, and lifecycle: open → acknowledged → investigating → resolved, with automatic reopening on recurrence and a full status history.
- Later sources: ML drift (with ML monitoring) and user-defined alert rules (with alerting).

### 5.6 Investigation Reports
- One or more reports per incident.
- **Evidence snapshot:** traces, spans, evaluation results, and metric windows collected automatically and copied into the report, so it stays readable after source data expires.
- Three clearly separated, labelled sections:
  - **Observed facts** – statements derived directly from recorded evidence.
  - **Hypotheses** – possible causes, AI-generated or user-written, each with status *proposed*, *confirmed*, or *rejected*.
  - **Recommended actions** – suggested next steps, AI-generated or user-written, each linked to the facts or hypotheses it addresses.
- **AI drafting:** an LLM (via the provider abstraction) may draft hypotheses and recommended actions from the evidence snapshot.
- **Grounding:** every AI-generated item must cite evidence recorded in the snapshot; uncited items are rejected. AI-generated hypotheses are always created as *proposed* and labelled as AI-generated. Only a user can mark a hypothesis *confirmed*; an unconfirmed hypothesis is never presented as a root cause.
- **Review and approval:** users can edit any section and approve the report. Status: draft → in review → approved. Approved reports are read-only; further changes require returning the report to draft.
- **Audit metadata:** every generation and update records who or what made it (user or system), when, and for AI drafts the provider, model, prompt version, and token usage.

### 5.7 ML Model Monitoring
- Register models and versions with a feature schema.
- Log predictions (features, prediction, optional ground truth) via REST API.
- Reference (baseline) datasets per model version.
- Data drift per feature using statistical tests (SciPy: Kolmogorov–Smirnov, chi-square) and Population Stability Index.
- Performance metrics (scikit-learn) when ground truth is available: classification and regression.

### 5.8 Explainability
- SHAP explanations for supported tabular models (tree-based, including XGBoost and scikit-learn).
- Global feature importance and per-prediction explanations.

### 5.9 Alerting
- Threshold rules on metrics (e.g. error rate, cost, drift score, evaluation score).
- Alert events visible in the UI and linked to incidents. Email, Slack, and webhook notifications are a post-M9 consideration.

### 5.10 Web Dashboard
- Projects overview, trace explorer, trace detail, cost/latency analytics, RAG evaluation results, incidents, investigation reports, model monitoring, drift reports, explanations, and alerts.
- Charts rendered with Recharts.

### 5.11 User Accounts and Roles
- Email/password sign-in; passwords hashed with Argon2id.
- JWT access tokens (15 minutes) and rotating refresh tokens (7 days) that can be revoked server-side (logout, password change).
- Refresh token held in an HttpOnly, Secure, SameSite=Lax cookie; CSRF protection via a signed token in a custom header plus Origin validation.
- Project roles (each project has exactly one Owner):

| Capability | Owner | Admin | Member |
|---|:-:|:-:|:-:|
| Project data access | ✓ | ✓ | ✓ |
| Trace and evaluation operations | ✓ | ✓ | ✓ |
| Incident and investigation report access (including AI drafting, editing, and approval) | ✓ | ✓ | ✓ |
| Project settings (retention, API keys, detector thresholds) | ✓ | ✓ | — |
| Member management | ✓ | ✓ | — |
| Delete project | ✓ | — | — |
| Transfer ownership | ✓ | — | — |

### 5.12 Data Retention
- Retention is configurable per project, with these defaults:

| Data | Default retention |
|---|---|
| Traces | 30 days |
| Evaluation results | 90 days |
| Prediction logs | 30 days |
| Incidents | 180 days |
| Investigation reports | 180 days |
| Model artifacts | Until manually deleted |

- Expired data removed by a scheduled background job. Unresolved incidents are never deleted.

### 5.13 Python SDK
- Thin client over the REST API for tracing and prediction logging (later milestone).

## 6. Functional Requirements

| ID | Requirement |
|---|---|
| FR-1 | Signed-in users can create projects; all data is scoped to a project; access is governed by project role (Owner, Admin, Member). |
| FR-2 | Ingestion is authenticated with per-project API keys; UI and management endpoints require a valid JWT access token. |
| FR-3 | Ingestion API validates payloads and rejects malformed data with clear errors. |
| FR-4 | Traces are viewable within seconds of ingestion. |
| FR-5 | Cost is computed from token counts and configured model pricing. |
| FR-6 | Evaluations and drift calculations run asynchronously in a worker process, report job status, and retry on failure. |
| FR-7 | Evaluation and drift results are persisted and comparable over time. |
| FR-8 | All UI data is served from the versioned REST API. |
| FR-9 | Retention is configurable per project, using the defaults in §5.12. |
| FR-10 | LLM/RAG problems are detected automatically and grouped into incidents with a tracked lifecycle. |
| FR-11 | Investigation reports preserve an evidence snapshot independently of source-data retention. |
| FR-13 | Reports separate observed facts, hypotheses, and recommended actions, and label AI-generated content. |
| FR-14 | Every AI-generated item cites recorded evidence; only users can confirm a hypothesis as a root cause. |
| FR-15 | Users can edit and approve reports; every generation and update is recorded with actor, time, and AI model metadata. |
| FR-12 | Every project-scoped action is authorized by project role (§5.11). |

## 7. Non-Functional Requirements

- **Deployment:** one backend codebase run as an API process and a worker process, single PostgreSQL database, frontend static build; runnable locally with Docker. No Redis or Celery.
- **Performance:** ingestion requests respond without waiting on evaluation or analytics work.
- **Security:** passwords hashed with Argon2id; API keys and refresh tokens stored hashed; access token held in browser memory and refresh token in an HttpOnly, Secure, SameSite=Lax cookie with a restricted path; signed double-submit CSRF token and Origin validation on state-changing requests; secrets supplied via environment variables; credentials and provider keys never logged.
- **Data privacy:** configurable retention (§5.12); configurable redaction of prompt/response content (planned).
- **Storage:** files stored on the local filesystem behind a storage abstraction, so other backends can be added later.
- **Quality:** automated tests run in GitHub Actions on every pull request.
- **Maintainability:** strict module boundaries (see [ARCHITECTURE.md](ARCHITECTURE.md)).

## 8. Success Criteria

- An LLM application can send its first trace with a single HTTP request.
- A RAG evaluation run produces per-item and aggregate scores for a dataset.
- An injected error-rate spike produces a single grouped incident, and an AI-drafted, evidence-cited report for it can be edited and approved.
- A drift report identifies a deliberately shifted feature in a test dataset.
- The full stack starts locally with one Docker command.

## 9. Decisions

Architecture decisions are recorded in [ARCHITECTURE.md §12](ARCHITECTURE.md#12-decisions).
