import { useEffect, useState } from 'react'

import { API_BASE_URL } from './api/client.ts'
import { checkApiStatus, type ApiStatus } from './api/status.ts'
import { AppShell } from './components/AppShell.tsx'

const PLANNED_AREAS = [
  { title: 'LLM tracing', description: 'Nested traces and spans for LLM calls, retrieval, and tools.' },
  { title: 'RAG evaluation', description: 'Context relevance, faithfulness, and answer relevance.' },
  { title: 'Incidents', description: 'Automatic detection and evidence-grounded investigations.' },
  { title: 'ML monitoring', description: 'Drift, performance, and SHAP explainability.' },
]

function ApiStatusBadge({ status }: { status: ApiStatus }) {
  const styles = {
    checking: 'bg-slate-100 text-slate-600',
    online: 'bg-emerald-100 text-emerald-800',
    unavailable: 'bg-rose-100 text-rose-800',
    unexpected: 'bg-amber-100 text-amber-800',
  }[status.state]
  return <span className={`rounded-full px-3 py-1 text-sm font-medium ${styles}`}>{statusLabel(status)}</span>
}

function statusLabel(status: ApiStatus): string {
  switch (status.state) {
    case 'checking':
      return 'Checking API…'
    case 'online':
      return `API online · v${status.health.version} · ${status.health.environment}`
    case 'unavailable':
      return 'API unavailable'
    case 'unexpected':
      return 'Unexpected API response'
  }
}

export default function App() {
  const [status, setStatus] = useState<ApiStatus>({ state: 'checking' })

  useEffect(() => {
    const controller = new AbortController()
    checkApiStatus(controller.signal)
      .then(setStatus)
      .catch(() => {
        // Aborted on unmount; nothing to update.
      })
    return () => controller.abort()
  }, [])

  return (
    <AppShell>
      <section className="space-y-4">
        <h1 className="text-3xl font-bold tracking-tight">AI model and LLM observability</h1>
        <p className="max-w-2xl text-slate-600">
          TraceIQ is in early development. The application foundation is in place; product features are
          planned and listed below.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <ApiStatusBadge status={status} />
          <code className="text-xs text-slate-500">{API_BASE_URL}</code>
        </div>
      </section>

      <section className="mt-10 grid gap-4 sm:grid-cols-2">
        {PLANNED_AREAS.map((area) => (
          <article key={area.title} className="rounded-lg border border-slate-200 bg-white p-5">
            <div className="flex items-center justify-between">
              <h2 className="font-semibold">{area.title}</h2>
              <span className="text-xs font-medium uppercase tracking-wide text-slate-400">Planned</span>
            </div>
            <p className="mt-2 text-sm text-slate-600">{area.description}</p>
          </article>
        ))}
      </section>
    </AppShell>
  )
}
