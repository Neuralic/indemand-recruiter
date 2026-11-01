import { StrictMode, Suspense, lazy, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import './index.css'
import { api, to, token, type Config } from './api'
import { Generic } from './ui'
import Interview from './pages/Interview'
import ScorecardPage from './pages/Scorecard'
import Dashboard from './pages/Dashboard'

// Review-only route; `import.meta.env.DEV` is false in the production build, so this is removed from it.
const RoomPreview = import.meta.env.DEV ? lazy(() => import('./pages/RoomPreview')) : null

function Gate() {
  const [cfg, setCfg] = useState<Config | null>(null)
  const [denied, setDenied] = useState(!token)
  useEffect(() => {
    if (!token) return
    api<Config>('/config').then(setCfg).catch(() => setDenied(true))
  }, [])
  if (denied) return <Generic />
  if (!cfg) return <div className="min-h-screen grid place-items-center text-muted" role="status">Loading…</div>
  return (
    <Routes>
      <Route path="/" element={<Navigate to={to('/interview')} replace />} />
      <Route path="/interview" element={<Interview cfg={cfg} />} />
      <Route path="/scorecard/:id" element={<ScorecardPage cfg={cfg} />} />
      <Route path="/dashboard" element={<Dashboard cfg={cfg} />} />
      {RoomPreview && <Route path="/preview/room" element={<Suspense fallback={null}><RoomPreview /></Suspense>} />}
      <Route path="*" element={<Generic />} />
    </Routes>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Gate />
    </BrowserRouter>
  </StrictMode>,
)
