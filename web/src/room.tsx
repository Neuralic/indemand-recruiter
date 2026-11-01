import { useEffect, useRef, type ReactNode, type RefObject } from 'react'
import { Button, mmss } from './ui'

export type RingState = 'speaking' | 'listening' | 'thinking' | 'muted'
export interface Line { role: 'user' | 'assistant'; text: string }
export type Partials = Partial<Record<'user' | 'assistant', string>>

/** The state, in words. Always shown next to the ring, so the animation is never the only signal. */
export const STATE_TEXT: Record<RingState, string> = {
  speaking: 'Avery is speaking',
  listening: 'Your turn',
  thinking: 'Avery is thinking',
  muted: 'You’re muted',
}
/** Optional second line under the state label (plain case, not spaced capitals). */
export const STATE_SUB: Partial<Record<RingState, string>> = { muted: 'Avery can’t hear you.' }

export const prefersReducedMotion = () => typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches

// ---- small pieces -------------------------------------------------------------------------------------------------------------------
export function Banner() {
  return (
    <p role="note" className="mb-6 flex items-start gap-3 rounded-xl border border-line bg-white px-4 py-2.5 text-sm text-ink">
      <span className="mt-px shrink-0 rounded-full bg-warn-soft px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-warn">Prototype</span>
      <span>Prototype for IN DEMAND to evaluate. Please don’t enter real candidate details.</span>
    </p>
  )
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return <p className="text-xs font-semibold uppercase tracking-[0.16em] text-accent">{children}</p>
}

export function FixList({ items }: { items: string[] }) {
  return (
    <ul className="mt-2 list-disc space-y-1.5 pl-5 text-sm text-ink">
      {items.map((i, n) => <li key={i} className={n === 0 ? 'font-semibold' : ''}>{i}</li>)}
    </ul>
  )
}

function Glyph({ state }: { state: RingState }) {
  if (state === 'speaking') return <svg className="eq" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="8" width="3" height="12" rx="1.5" /><rect x="8.5" y="3" width="3" height="17" rx="1.5" /><rect x="14" y="6" width="3" height="14" rx="1.5" /><rect x="19.5" y="10" width="3" height="10" rx="1.5" /></svg>
  if (state === 'thinking') return <svg className="dots" viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="2.4" /><circle cx="12" cy="12" r="2.4" /><circle cx="19" cy="12" r="2.4" /></svg>
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
      {state === 'muted' && <path d="M4 4l16 16" />}
    </svg>
  )
}

export const Ring = ({ state, size = 120, ringRef }: { state: RingState; size?: number; ringRef?: RefObject<HTMLDivElement | null> }) => (
  <div ref={ringRef} className="state-ring" data-state={state} style={{ ['--size' as string]: `${size}px` }} aria-hidden="true"><Glyph state={state} /></div>
)

/**
 * What to show large: the last question Avery asked. Short questions ("What was your role?") borrow the sentence before them for
 * context; a lead-in acknowledgement ("That makes sense.") is dropped unless it is needed.
 */
export function currentPrompt(lines: Line[], partial: Partials): string {
  const live = partial.assistant
  const last = live ?? [...lines].reverse().find((l) => l.role === 'assistant')?.text ?? ''
  const text = last.replace(/\s+/g, ' ').trim()
  if (!text) return ''
  const sentences = text.match(/[^.!?]+[.!?]+["”’)]*|[^.!?]+$/g)?.map((x) => x.trim()).filter(Boolean) ?? [text]
  let end = sentences.length - 1
  for (let i = sentences.length - 1; i >= 0; i--) if (sentences[i].includes('?')) { end = i; break }
  let start = end
  while (start > 0 && sentences.slice(start, end + 1).join(' ').length < 70) start--
  const out = sentences.slice(start, end + 1).join(' ')
  return out.length > 300 ? '…' + out.slice(-297) : out
}

const promptSize = (s: string) => (s.length <= 90 ? 'text-[1.7rem] sm:text-[2.1rem]' : s.length <= 180 ? 'text-[1.4rem] sm:text-[1.75rem]' : 'text-[1.2rem] sm:text-[1.45rem]')

export function Progress({ secs, maxSecs }: { secs: number; maxSecs: number }) {
  const pct = Math.min(100, Math.round((secs / maxSecs) * 100))
  return (
    <div>
      <div className="flex items-baseline justify-between text-sm text-muted">
        <span className="tabular-nums font-medium text-ink">{mmss(secs)}</span>
        <span>About 10 to 15 minutes</span>
      </div>
      <div className="mt-2 h-[3px] w-full overflow-hidden rounded-full bg-line" role="progressbar" aria-label="Interview time"
        aria-valuemin={0} aria-valuemax={maxSecs} aria-valuenow={Math.min(secs, maxSecs)} aria-valuetext={`${mmss(secs)} elapsed, up to ${mmss(maxSecs)}`}>
        <div className="h-full bg-navy-900 transition-[width] duration-1000 ease-linear motion-reduce:transition-none" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

export function RecordingBadge() {
  return (
    <span className="inline-flex items-center gap-2 text-sm font-medium text-ink">
      <span className="rec-dot" aria-hidden="true" />
      Recording
    </span>
  )
}

function Captions({ lines, partial, logRef }: { lines: Line[]; partial: Partials; logRef: RefObject<HTMLDivElement | null> }) {
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
  }, [lines, partial, logRef])
  const empty = lines.length === 0 && !partial.user && !partial.assistant
  const row = (who: 'user' | 'assistant', text: string, faint = false, key?: string | number) => (
    <p key={key} className={`leading-relaxed ${faint ? 'text-muted italic' : ''}`}>
      <span className={`mr-2 text-[11px] font-semibold uppercase tracking-[0.12em] ${who === 'user' ? 'text-navy-700' : 'text-accent'}`}>{who === 'user' ? 'You' : 'Avery'}</span>
      {text}
    </p>
  )
  return (
    <aside aria-labelledby="cap-h" className="flex min-h-0 flex-col rounded-2xl border border-line bg-white">
      <h2 id="cap-h" className="border-b border-line px-4 py-3 text-xs font-semibold uppercase tracking-[0.16em] text-muted">Live captions</h2>
      <div ref={logRef} role="log" aria-live="polite" aria-label="Live captions" className="h-60 space-y-3 overflow-y-auto px-4 py-3 text-[15px] lg:h-[26rem]">
        {empty && <p className="text-muted">Captions will appear here as you talk.</p>}
        {lines.map((l, i) => row(l.role, l.text, false, i))}
        {partial.assistant && row('assistant', partial.assistant, true, 'pa')}
        {partial.user && row('user', partial.user, true, 'pu')}
      </div>
    </aside>
  )
}

/** Native <dialog>: focus trap, Escape and backdrop come from the browser. */
export function EndDialog({ open, onKeep, onEnd }: { open: boolean; onKeep: () => void; onEnd: () => void }) {
  const ref = useRef<HTMLDialogElement | null>(null)
  useEffect(() => {
    const d = ref.current
    if (!d) return
    if (open && !d.open) {
      d.showModal()
      d.querySelectorAll('button')[d.querySelectorAll('button').length - 1]?.focus() // the safe choice, "Keep going", gets focus
    }
    if (!open && d.open) d.close()
  }, [open])
  return (
    <dialog ref={ref} onCancel={(e) => { e.preventDefault(); onKeep() }} aria-labelledby="end-h" aria-describedby="end-p"
      className="m-auto w-[min(92vw,30rem)] rounded-2xl border border-line bg-paper p-6 text-ink shadow-xl">
      <h2 id="end-h" className="serif text-2xl text-navy-900">End the interview now?</h2>
      <p id="end-p" className="mt-3 leading-relaxed">
        Ending early gives a thin scorecard. Stefanie’s team can only judge what was said, so a short conversation leaves most of the competencies unscored.
      </p>
      <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
        <Button kind="secondary" onClick={onEnd}>End now</Button>
        <Button onClick={onKeep}>Keep going</Button>
      </div>
    </dialog>
  )
}

// ---- the in-call view (pure: all state comes in as props, so it can be previewed without a real call) -------------------------------
export interface InCallProps {
  ringState: RingState
  lines: Line[]
  partial: Partials
  secs: number
  maxSecs: number
  muted: boolean
  noAudio: boolean
  askedPerson: boolean
  confirmOpen: boolean
  fixes: string[]
  ringRef?: RefObject<HTMLDivElement | null>
  meterRef?: RefObject<HTMLDivElement | null>
  logRef: RefObject<HTMLDivElement | null>
  onMute: () => void
  onAskPerson: () => void
  onRequestEnd: () => void
  onKeep: () => void
  onEnd: () => void
}

export function InCall(p: InCallProps) {
  const prompt = currentPrompt(p.lines, p.partial)
  return (
    <>
      <div className="grid gap-6 pb-28 lg:grid-cols-[minmax(0,1fr)_21rem] lg:pb-0">
        <section aria-label="Interview" className="rounded-2xl border border-line bg-white px-5 py-5 sm:px-8">
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted">AI interviewer <span className="text-ink">· Avery</span></p>
            <RecordingBadge />
          </div>

          {p.noAudio && (
            <div className="mt-4 rounded-xl bg-warn-soft px-4 py-3 text-warn" role="alert">
              <p className="font-semibold">We can’t hear Avery. No audio has arrived yet.</p>
              <FixList items={p.fixes} />
              <div className="mt-3"><Button kind="secondary" onClick={p.onEnd}>End and try again</Button></div>
            </div>
          )}

          <div className="flex flex-col items-center pt-8 text-center">
            <Ring state={p.ringState} ringRef={p.ringRef} />
            <p className="mt-6 text-sm font-semibold uppercase tracking-[0.14em] text-ink" role="status" aria-live="polite">{STATE_TEXT[p.ringState]}</p>
            {STATE_SUB[p.ringState] && <p className="mt-1 text-sm text-ink">{STATE_SUB[p.ringState]}</p>}
            <div className="mt-2 flex items-center gap-2 text-xs text-muted" aria-hidden="true">
              <span>Your microphone</span>
              <span className="h-[3px] w-20 overflow-hidden rounded-full bg-line"><span ref={p.meterRef} className="block h-full w-0 bg-accent" /></span>
            </div>

            <div className="flex min-h-[8.5rem] w-full max-w-2xl items-center justify-center py-6 sm:min-h-[10.5rem] sm:py-8" aria-label="Current question">
              {prompt
                ? <p key={prompt.slice(0, 24)} className={`serif fade-in leading-snug text-navy-900 ${promptSize(prompt)} ${p.ringState === 'thinking' || p.ringState === 'muted' ? 'opacity-70' : ''}`}>{prompt}</p>
                : <p className="serif text-xl text-muted">Avery is about to say hello.</p>}
            </div>
          </div>

          <Progress secs={p.secs} maxSecs={p.maxSecs} />
          <div className="mt-4 flex items-center justify-between gap-3">
            <button className="min-h-11 text-sm font-medium text-accent underline underline-offset-4 disabled:text-muted disabled:no-underline" onClick={p.onAskPerson} disabled={p.askedPerson}>
              Ask for a person
            </button>
            {p.askedPerson && <p className="text-sm text-ink" role="status">In the live product this reaches Stefanie’s team.</p>}
          </div>
        </section>

        <Captions lines={p.lines} partial={p.partial} logRef={p.logRef} />
      </div>

      <div className="fixed inset-x-0 bottom-0 z-10 border-t border-line bg-paper/95 px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur lg:static lg:mt-6 lg:border-0 lg:bg-transparent lg:p-0 lg:backdrop-blur-none">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-3">
          <Button kind="secondary" onClick={p.onMute} className="min-w-28">{p.muted ? 'Unmute' : 'Mute'}</Button>
          <Button kind="secondary" onClick={p.onRequestEnd} className="min-w-36">End interview</Button>
        </div>
      </div>

      <EndDialog open={p.confirmOpen} onKeep={p.onKeep} onEnd={p.onEnd} />
    </>
  )
}
