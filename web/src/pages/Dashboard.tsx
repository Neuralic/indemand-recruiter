import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, knownCalls, to, type Config, type RoleCard, type Row } from '../api'
import { Badge, Roadmap, Shell, SourceBadge, recTone } from '../ui'

const KO_TONE: Record<string, 'good' | 'bad' | 'warn'> = { pass: 'good', fail: 'bad', unclear: 'warn' }
const fmtDate = (t: number) => (t ? new Date(t * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '')

export default function Dashboard({ cfg }: { cfg: Config }) {
  const [role, setRole] = useState<RoleCard | null>(null)
  const [rows, setRows] = useState<Row[] | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let live = true
    const load = () => {
      const known = knownCalls().join(',')
      api<{ role: RoleCard; candidates: Row[] }>(`/dashboard${known ? `?known=${known}` : ''}`)
        .then((d) => { if (live) { setRole(d.role); setRows(d.candidates) } })
        .catch(() => live && setFailed(true))
    }
    load()
    const id = window.setInterval(() => { if (rows?.some((r) => r.status === 'processing')) load() }, 3000)
    return () => { live = false; clearInterval(id) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows?.some((r) => r.status === 'processing')])

  if (failed) return <Shell><p role="alert">The dashboard couldn’t be loaded. Please reload.</p></Shell>
  if (!rows || !role) return <Shell><p className="text-muted" role="status">Loading…</p></Shell>

  return (
    <Shell wide>
      <p className="text-sm text-muted">Role #{role.id}</p>
      <h1 className="text-2xl font-semibold text-navy-900">{role.title}</h1>
      <p className="text-muted">{role.location}</p>
      <p className="mt-3 text-sm max-w-2xl">Every candidate below was interviewed by Avery. You would call the top few. Simulated candidates are labelled, and your own interview appears as “{cfg.live_label}”.</p>

      <div className="mt-6 hidden md:block overflow-x-auto rounded-xl border border-line">
        <table className="w-full text-left text-sm">
          <thead className="bg-navy-50 text-navy-800">
            <tr>{['#', 'Candidate', 'Score', 'Recommendation', 'Required checks', 'Top flag', 'Date'].map((h) => <th key={h} className="px-3 py-2 font-semibold">{h}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-t border-line align-top">
                <td className="px-3 py-3 font-semibold">{r.rank ?? '–'}</td>
                <td className="px-3 py-3">
                  <Link className="font-semibold text-navy-900 underline underline-offset-2" to={to(`/scorecard/${r.id}`)}>{r.candidate_name}</Link>
                  <div className="mt-1"><SourceBadge label={r.label} sample={r.is_sample} /></div>
                </td>
                <td className="px-3 py-3 text-lg font-bold">{r.score ?? '–'}</td>
                <td className="px-3 py-3">{r.recommendation ? <Badge tone={recTone(r.recommendation)}>{r.recommendation}</Badge> : <span className="text-muted">{statusText(r.status)}</span>}</td>
                <td className="px-3 py-3"><Marks ko={r.knockouts} /></td>
                <td className="px-3 py-3 max-w-xs">{r.top_flag ?? <span className="text-muted">–</span>}</td>
                <td className="px-3 py-3 whitespace-nowrap">{fmtDate(r.date)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ul className="mt-6 space-y-3 md:hidden">
        {rows.map((r) => (
          <li key={r.id} className="rounded-xl border border-line p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <span className="text-sm text-muted">{r.rank ? `#${r.rank}` : ''}</span>
                <Link className="block font-semibold text-navy-900 underline underline-offset-2" to={to(`/scorecard/${r.id}`)}>{r.candidate_name}</Link>
                <div className="mt-1"><SourceBadge label={r.label} sample={r.is_sample} /></div>
              </div>
              <div className="text-right">
                <div className="text-2xl font-bold">{r.score ?? '–'}</div>
                {r.recommendation ? <Badge tone={recTone(r.recommendation)}>{r.recommendation}</Badge> : <span className="text-xs text-muted">{statusText(r.status)}</span>}
              </div>
            </div>
            <div className="mt-2"><Marks ko={r.knockouts} /></div>
            {r.top_flag && <p className="mt-2 text-sm">{r.top_flag}</p>}
          </li>
        ))}
      </ul>

      <Roadmap mailto={cfg.booking_url} />
    </Shell>
  )
}

const statusText = (s: string) => (s === 'processing' ? 'Scoring…' : s === 'in-progress' ? 'In progress' : s === 'failed' ? 'Scorecard unavailable' : s === 'unusable' ? 'Too short to score' : s)

function Marks({ ko }: { ko: { id: string; status: string }[] }) {
  if (!ko.length) return <span className="text-muted">–</span>
  return (
    <span className="inline-flex gap-1" aria-label={ko.map((k) => `${k.id}: ${k.status}`).join(', ')}>
      {ko.map((k) => <Badge key={k.id} tone={KO_TONE[k.status] ?? 'neutral'}>{k.status === 'pass' ? '✓' : k.status === 'fail' ? '✗' : '?'}</Badge>)}
    </span>
  )
}
