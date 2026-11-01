import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError, api, recordingUrl, to, type Competency, type Config, type Knockout, type ScorecardResponse, type Turn } from '../api'
import { Badge, Button, Roadmap, Shell, SourceBadge, mmss } from '../ui'

const KO_TONE = { pass: 'good', fail: 'bad', unclear: 'warn' } as const
const KO_WORD = { pass: 'Pass', fail: 'Fail', unclear: 'Unclear' } as const

export default function ScorecardPage({ cfg }: { cfg: Config }) {
  const { id = '' } = useParams()
  const [data, setData] = useState<ScorecardResponse | null>(null)
  const [missing, setMissing] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const load = useCallback(async () => {
    try {
      setData(await api<ScorecardResponse>(`/interviews/${id}/scorecard`))
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setMissing(true)
    }
  }, [id])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (data && !['waiting', 'generating'].includes(data.status)) return
    const poll = window.setInterval(() => void load(), 2500)
    const clock = window.setInterval(() => setElapsed((s) => s + 1), 1000)
    return () => { clearInterval(poll); clearInterval(clock) }
  }, [data, load])

  const retry = async () => {
    await api(`/interviews/${id}/scorecard/retry`, { method: 'POST' }).catch(() => {})
    setElapsed(0)
    setData((d) => (d ? { ...d, status: 'generating' } : d))
  }

  const play = (t: number) => {
    const a = audioRef.current
    if (!a) return
    a.currentTime = Math.max(0, t - 0.5)
    void a.play()
  }

  if (missing) {
    return <Shell><h1 className="text-xl font-semibold text-navy-900">Scorecard not found</h1><p className="mt-2 text-muted">This scorecard isn’t available. <Link className="text-accent underline" to={to('/dashboard')}>Back to the dashboard</Link></p></Shell>
  }
  if (!data) return <Shell><p className="text-muted" role="status">Loading…</p></Shell>

  if (data.status === 'waiting' || data.status === 'generating') {
    return (
      <Shell>
        <h1 className="text-2xl font-semibold text-navy-900">Thanks.</h1>
        <p className="mt-2 text-lg">Stefanie’s team reviews every interview personally, and if it’s a fit they’ll be in touch.</p>
        <div className="mt-8 rounded-xl border border-line p-5" role="status" aria-live="polite">
          <p className="font-semibold text-navy-900">{data.status === 'waiting' ? 'Waiting for the call to finish…' : 'Preparing the recruiter scorecard…'}</p>
          <p className="mt-1 text-sm text-muted">This is what Stefanie would see next. It usually takes under a minute.{elapsed > 5 ? ` (${elapsed}s)` : ''}</p>
          <div className="mt-4 h-2 rounded-full bg-navy-50 overflow-hidden"><div className="h-full w-1/3 bg-accent animate-pulse motion-reduce:animate-none" /></div>
        </div>
        {elapsed > 90 && <p className="mt-4 text-sm text-muted">Taking longer than usual. <button className="text-accent underline" onClick={() => void retry()}>Try again</button></p>}
      </Shell>
    )
  }

  if (data.status === 'failed' || data.status === 'unusable') {
    return (
      <Shell>
        <h1 className="text-xl font-semibold text-navy-900">Scorecard unavailable</h1>
        <p className="mt-2" role="alert">{data.status === 'unusable' ? data.message : 'We couldn’t build the scorecard just now. Your interview is saved.'}</p>
        <div className="mt-5 flex flex-col sm:flex-row gap-3">
          {data.status === 'failed' && <Button onClick={() => void retry()}>Scorecard unavailable, retry</Button>}
          <Button kind="secondary" href={to('/interview')}>Run the interview again</Button>
        </div>
      </Shell>
    )
  }

  const sc = data.scorecard!
  const canPlay = data.has_recording
  return (
    <Shell wide>
      <div className="pb-24">
        <div className="flex flex-wrap items-center gap-2">
          <SourceBadge label={data.label} sample={data.is_sample} />
          <span className="text-sm text-muted">{data.candidate_name !== data.label ? data.candidate_name : ''}</span>
        </div>

        <section className="mt-3 rounded-2xl bg-navy-900 text-white p-5 sm:p-6">
          <p className="text-xs uppercase tracking-wide text-navy-100">{sc.summary_label}</p>
          <h1 className="mt-2 text-xl sm:text-2xl font-semibold leading-snug">{sc.headline}</h1>
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <span className={`rounded-full px-4 py-1.5 font-bold ${sc.recommendation === 'Submit' ? 'bg-good-soft text-good' : sc.recommendation === 'Hold' ? 'bg-warn-soft text-warn' : 'bg-bad-soft text-bad'}`}>{sc.recommendation}</span>
            <span className="text-3xl font-bold">{sc.overall_score}<span className="text-base font-normal text-navy-100"> / 100</span></span>
            <span className="text-sm text-navy-100">Required checks passed: {sc.knockouts_required_passed} of {sc.knockouts_required}</span>
          </div>
          {sc.recommendation_note && <p className="mt-3 text-sm text-navy-100">{sc.recommendation_note}</p>}
        </section>

        {data.rebuilt && (
          <p className="mt-3 rounded-xl bg-warn-soft px-4 py-3 text-sm text-warn" role="note">
            This scorecard was rebuilt from the recording after a server restart, so the score can differ slightly from the first version.
          </p>
        )}

        {canPlay && <audio ref={audioRef} src={recordingUrl(id)} preload="none" controls className="mt-4 w-full" aria-label="Interview recording" />}
        {!canPlay && data.is_sample && <p className="mt-3 text-sm text-muted">This is a simulated interview, so there is no audio recording.</p>}

        <h2 className="mt-8 text-lg font-semibold text-navy-900">Requirements</h2>
        <ul className="mt-3 grid gap-3 sm:grid-cols-2">{sc.knockouts.map((k) => <KnockoutCard key={k.id} k={k} />)}</ul>

        <h2 className="mt-8 text-lg font-semibold text-navy-900">Competencies</h2>
        <ul className="mt-3 grid gap-4 md:grid-cols-2">
          {sc.competencies.map((c) => <CompetencyCard key={c.id} c={c} canPlay={canPlay} onPlay={play} />)}
        </ul>

        <div className="mt-8 grid gap-4 md:grid-cols-2">
          <ListCard title="Strengths" items={sc.strengths} tone="good" />
          <ListCard title="Risks" items={sc.risks} tone="warn" />
        </div>
        <div className="mt-4 rounded-xl border-2 border-accent p-4 bg-accent-soft/40">
          <h2 className="font-semibold text-navy-900">Verify on your call</h2>
          <ol className="mt-2 list-decimal pl-5 space-y-1.5">{sc.verify_on_call.map((v) => <li key={v}>{v}</li>)}</ol>
        </div>

        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <div className="rounded-xl border border-line p-4">
            <h2 className="font-semibold text-navy-900">Logistics</h2>
            <dl className="mt-2 space-y-2 text-sm">
              {Object.entries(sc.logistics).map(([k, v]) => (
                <div key={k}><dt className="text-muted capitalize">{k.replace('_', ' ')}</dt><dd>{v.answer || 'Not discussed'}</dd></div>
              ))}
            </dl>
          </div>
          <div className="rounded-xl border border-line p-4">
            <h2 className="font-semibold text-navy-900">For Stefanie to cover</h2>
            {sc.candidate_questions_to_answer.length > 0 && (
              <><p className="mt-2 text-sm text-muted">Questions the candidate asked:</p>
              <ul className="list-disc pl-5 text-sm space-y-1">{sc.candidate_questions_to_answer.map((q) => <li key={q}>{q}</li>)}</ul></>
            )}
            <p className="mt-3 text-sm text-muted">Not stated in the posting, so Avery doesn’t answer these: {sc.not_in_posting.join(', ')}.</p>
          </div>
        </div>

        <Transcript id={id} canPlay={canPlay} onPlay={play} />

        <p className="mt-6 text-xs text-muted">
          Scorecard written by {sc.versions.scorecard_model ?? 'Claude'} ({sc.versions.scorecard_prompt}). Every quote above was checked against the transcript;
          {` ${sc.quote_check.dropped} of ${sc.quote_check.proposed}`} proposed quotes could not be verified and were left out. Interview length {data.duration_s ? mmss(data.duration_s) : 'n/a'}.
        </p>

        <Roadmap mailto={cfg.booking_url} />
      </div>

      <div className="fixed inset-x-0 bottom-0 border-t border-line bg-white/95 backdrop-blur px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
        <div className="mx-auto max-w-5xl flex items-center justify-between gap-3">
          <Link className="text-sm text-accent underline min-h-11 inline-flex items-center" to={to('/dashboard')}>All candidates</Link>
          <Button href={cfg.booking_url}>Book a 15-min call with NeuralicAI</Button>
        </div>
      </div>
    </Shell>
  )
}

function KnockoutCard({ k }: { k: Knockout }) {
  return (
    <li className="rounded-xl border border-line p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-navy-900">{k.question}</span>
        <Badge tone={KO_TONE[k.status]}>{KO_WORD[k.status]}</Badge>
      </div>
      <p className="mt-1 text-sm text-muted">{k.required ? 'Required' : 'Logistics'}{k.answer ? ` · ${k.answer}` : ''}</p>
      {(k.quote || k.note) && (
        <details className="mt-1">
          <summary className="text-sm text-accent cursor-pointer min-h-8">Their words</summary>
          {k.quote ? <blockquote className="mt-1 border-l-4 border-line pl-3 text-sm italic">“{k.quote}”{k.t != null && <span className="not-italic text-muted"> at {mmss(k.t)}</span>}</blockquote> : <p className="text-sm text-muted">{k.note}</p>}
        </details>
      )}
    </li>
  )
}

function CompetencyCard({ c, canPlay, onPlay }: { c: Competency; canPlay: boolean; onPlay: (t: number) => void }) {
  return (
    <li className="rounded-xl border border-line p-4">
      <div className="flex items-start justify-between gap-3">
        <h3 className="font-semibold text-navy-900">{c.name}</h3>
        <div className="text-right shrink-0">
          <div className="text-2xl font-bold text-navy-900" aria-label={`Score ${c.score} out of 5`}>{c.score}<span className="text-sm font-normal text-muted"> / 5</span></div>
          <div className="text-xs text-muted">{{ high: 'High', med: 'Medium', low: 'Low' }[c.confidence]} confidence</div>
        </div>
      </div>
      <p className="mt-2 text-sm">{c.rationale}</p>
      {c.evidence.length === 0 && <p className="mt-2 text-sm text-warn">{c.evidence_note}</p>}
      {c.evidence.map((e) => (
        <blockquote key={e.quote} className="mt-2 border-l-4 border-accent pl-3 text-sm">
          <span className="italic">“{e.quote}”</span>
          <div className="mt-1 flex items-center gap-3 text-xs text-muted">
            <span>at {mmss(e.t)}</span>
            {canPlay && <button className="text-accent font-semibold underline min-h-8" onClick={() => onPlay(e.t)}>Play from {mmss(e.t)}</button>}
          </div>
        </blockquote>
      ))}
    </li>
  )
}

function ListCard({ title, items, tone }: { title: string; items: string[]; tone: 'good' | 'warn' }) {
  return (
    <div className="rounded-xl border border-line p-4">
      <h2 className="font-semibold text-navy-900 flex items-center gap-2">{title} <Badge tone={tone}>{items.length}</Badge></h2>
      <ul className="mt-2 list-disc pl-5 space-y-1.5 text-sm">{items.map((i) => <li key={i}>{i}</li>)}</ul>
    </div>
  )
}

function Transcript({ id, canPlay, onPlay }: { id: string; canPlay: boolean; onPlay: (t: number) => void }) {
  const [turns, setTurns] = useState<Turn[] | null>(null)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    if (open && !turns) void api<Turn[]>(`/interviews/${id}/transcript`).then(setTurns).catch(() => setTurns([]))
  }, [open, turns, id])
  return (
    <details className="mt-6 rounded-xl border border-line p-4" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="font-semibold text-navy-900 cursor-pointer min-h-8">Transcript</summary>
      <div className="mt-3 space-y-2 text-sm">
        {turns === null && <p className="text-muted">Loading…</p>}
        {turns?.map((t, i) => (
          <p key={i}>
            <button className={`font-mono text-xs mr-2 ${canPlay ? 'text-accent underline' : 'text-muted cursor-default'}`} onClick={() => canPlay && onPlay(t.t_start)}>{mmss(t.t_start)}</button>
            <strong>{t.speaker === 'candidate' ? 'Candidate' : 'Avery'}:</strong> {t.text}
          </p>
        ))}
      </div>
    </details>
  )
}

