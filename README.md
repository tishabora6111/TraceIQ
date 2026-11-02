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

Setup instructions will be added with milestone **M1 – Foundation**. The planned local workflow is a single Docker Compose command that starts the API, worker, frontend, and PostgreSQL.

## Documentation

| Document | Contents |
|---|---|
| [docs/PRD.md](docs/PRD.md) | Product requirements and core features |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design and data model |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Milestones and status |
| [CLAUDE.md](CLAUDE.md) | Development rules and coding standards |

## License

Released under the [MIT License](LICENSE).
