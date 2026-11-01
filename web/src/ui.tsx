import type { ReactNode } from 'react'
import { Link, NavLink } from 'react-router-dom'
import { to } from './api'

export const FOOTER_TEXT = 'Prototype prepared for IN DEMAND Recruitment by NeuralicAI.'
export const NOT_AVAILABLE = 'This page isn’t available.'

export function Shell({ children, wide = false, flush = false }: { children: ReactNode; wide?: boolean; flush?: boolean }) {
  const nav = ({ isActive }: { isActive: boolean }) =>
    `px-1 py-2 min-h-11 inline-flex items-center text-sm font-medium border-b-2 ${isActive ? 'border-navy-900 text-navy-900' : 'border-transparent text-muted hover:text-navy-900'}`
  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-line">
        <div className="mx-auto max-w-5xl px-4 py-2 flex items-center justify-between gap-4">
          <Link to={to('/interview')} className="leading-tight">
            <span className="serif block text-[15px] font-semibold tracking-[0.14em] text-navy-900">IN DEMAND</span>
            <span className="block text-xs text-muted">Recruitment &amp; Consulting</span>
          </Link>
          <nav className="flex gap-5" aria-label="Main">
            <NavLink to={to('/interview')} className={nav}>Interview</NavLink>
            <NavLink to={to('/dashboard')} className={nav}>Dashboard</NavLink>
          </nav>
        </div>
      </header>
      <main className={`flex-1 w-full mx-auto px-4 ${flush ? 'pt-4 pb-6' : 'py-8'} ${wide ? 'max-w-5xl' : 'max-w-2xl'}`}>{children}</main>
      <Footer />
    </div>
  )
}

export function Footer() {
  return (
    <footer className="border-t border-line text-muted text-xs">
      <div className="mx-auto max-w-5xl px-4 py-4 text-center">{FOOTER_TEXT}</div>
    </footer>
  )
}

export function Generic() {
  return (
    <div className="min-h-screen flex flex-col">
      <main className="flex-1 grid place-items-center px-4 text-center">
        <div>
          <h1 className="text-xl font-semibold text-navy-900">{NOT_AVAILABLE}</h1>
          <p className="mt-2 text-muted">The link may be incomplete or no longer active.</p>
        </div>
      </main>
      <Footer />
    </div>
  )
}

export function Badge({ tone, children }: { tone: 'good' | 'bad' | 'warn' | 'neutral' | 'accent'; children: ReactNode }) {
  const c = {
    good: 'bg-good-soft text-good',
    bad: 'bg-bad-soft text-bad',
    warn: 'bg-warn-soft text-warn',
    neutral: 'bg-navy-50 text-navy-800 border border-line',
    accent: 'bg-accent-soft text-accent',
  }[tone]
  return <span className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap ${c}`}>{children}</span>
}

export const recTone = (r: string | null) => (r === 'Submit' ? 'good' : r === 'Hold' ? 'warn' : r === 'Pass' ? 'bad' : 'neutral')

export function SourceBadge({ label, sample }: { label: string; sample: boolean }) {
  return <Badge tone={sample ? 'warn' : 'accent'}>{label}</Badge>
}

export const mmss = (t: number) => `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(Math.floor(t % 60)).padStart(2, '0')}`

export function Button({
  children, onClick, kind = 'primary', disabled, href, type = 'button', className = '',
}: {
  children: ReactNode; onClick?: () => void; kind?: 'primary' | 'secondary' | 'danger'; disabled?: boolean; href?: string
  type?: 'button' | 'submit'; className?: string
}) {
  const base = 'inline-flex items-center justify-center rounded-xl px-5 py-3 text-base font-semibold min-h-12 transition-colors'
  const k = {
    primary: 'bg-navy-900 text-white hover:bg-navy-800 disabled:bg-navy-100 disabled:text-muted',
    secondary: 'bg-white text-navy-900 border border-navy-700 hover:bg-paper-2 disabled:text-muted disabled:border-line',
    danger: 'bg-bad text-white hover:opacity-90',
  }[kind]
  if (href) return <a href={href} className={`${base} ${k} ${className}`}>{children}</a>
  return <button type={type} onClick={onClick} disabled={disabled} className={`${base} ${k} ${className}`}>{children}</button>
}

const ROADMAP = [
  { title: 'Re-engage your candidate database', body: 'Reach out to past candidates when a matching role opens.', subject: 'Yes, build: candidate database re-engagement' },
  { title: 'Client-ready search reports', body: 'Turn a shortlist into a polished report you can send to a client.', subject: 'Yes, build: client reports' },
  { title: 'ATS sync', body: 'Keep candidates and notes in step with your applicant tracking system.', subject: 'Yes, build: ATS sync', ask: 'Which ATS do you use?' },
  { title: 'Joins your Meet, Zoom or Teams calls', body: 'Screen candidates inside the video calls you already run.', subject: 'Yes, build: joins Meet, Zoom or Teams calls' },
  { title: 'Interview integrity checks', body: 'ID and fraud detection during screening.', subject: 'Yes, build: interview integrity checks' },
]

export function Roadmap({ mailto }: { mailto: string }) {
  // mailto is "mailto:addr?subject=..." — swap the subject per card.
  const addr = mailto.startsWith('mailto:') ? mailto.slice(7).split('?')[0] : 'neuralicstudio@gmail.com'
  return (
    <section aria-labelledby="roadmap" className="mt-10">
      <h2 id="roadmap" className="text-lg font-semibold text-navy-900">What could come next</h2>
      <p className="text-sm text-muted mt-1">None of these exist yet. Tell us which ones matter to you.</p>
      <ul className="mt-4 grid gap-3 sm:grid-cols-2">
        {ROADMAP.map((r) => (
          <li key={r.title} className="rounded-xl border border-line p-4 flex flex-col gap-2">
            <span className="self-start"><Badge tone="warn">Planned, not built yet</Badge></span>
            <h3 className="font-semibold text-navy-900">{r.title}</h3>
            <p className="text-sm text-muted flex-1">{r.body}</p>
            {r.ask && <p className="text-sm text-ink">{r.ask}</p>}
            <a
              className="self-start text-sm font-semibold text-accent underline underline-offset-2 min-h-11 inline-flex items-center"
              href={`mailto:${addr}?subject=${encodeURIComponent(r.subject)}`}
            >
              Yes, build this
            </a>
          </li>
        ))}
      </ul>
    </section>
  )
}
