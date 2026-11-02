import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type Vapi from '@vapi-ai/web'
import { ApiError, api, rememberCall, to, type Config } from '../api'
import { useMicCheck, webrtcLooksBlocked } from '../mic'
import { loadVapi, describeError } from '../vapiSdk'
import { cleanText } from '../text'
import { Banner, Eyebrow, FixList, InCall, Ring, SlowConnectNote, STATE_SUB, STATE_TEXT, type Line, type Partials, type RingState } from '../room'
import { Button, Shell } from '../ui'

const DISCLOSURE =
  'This is a short first conversation (about 10 to 15 minutes) with Avery, IN DEMAND’s AI screening assistant. It’s recorded so Stefanie’s team can review it personally. AI assists the screening and doesn’t replace human judgment. You can ask for a person at any time.'

type Phase = 'landing' | 'precall' | 'connecting' | 'incall' | 'error'
type ErrorKind = 'refused' | 'connection' | 'voice' | 'dropped'

// VPN first: in our own tests a VPN was the thing that broke live audio.
export const NO_AUDIO_FIXES = [
  'Turn off any VPN, then reload this page. A VPN broke live audio in our own tests.',
  'Try headphones or earbuds. They stop echo and help your microphone pick you up.',
  'Check the browser allowed your microphone (the padlock icon next to the address bar).',
  'Close other apps or tabs that might be using the microphone (Zoom, Teams, Meet).',
  'Try a different network, for example your phone’s hotspot instead of office Wi‑Fi.',
]

// Connecting. Facts from the call logs: on this network a healthy call takes 12 to 14 s to connect, and a call that never gets audio is
// ended by Vapi after 15 s ("did not receive customer audio"). So a failed first attempt is retried quietly, in the same session, up to
// MAX_AUTO_RETRIES times, and the candidate only sees an error if every attempt fails.
const MAX_AUTO_RETRIES = 2
const RETRY_DELAY_MS = 1200
const CONNECT_FAILSAFE_MS = 25_000 // Vapi normally ends a dead attempt itself at 15 s; this covers the case where no event ever arrives

const Card = ({ children, label }: { children: React.ReactNode; label: string }) => (
  <section className="rounded-2xl border border-line bg-white p-5 sm:p-6" aria-labelledby={label}>{children}</section>
)

export default function Interview({ cfg }: { cfg: Config }) {
  const nav = useNavigate()
  const mic = useMicCheck()
  const [phase, setPhase] = useState<Phase>('landing')
  const [agreed, setAgreed] = useState(false)
  const [person, setPerson] = useState(false)
  const [netBlocked, setNetBlocked] = useState(false)
  const [errorKind, setErrorKind] = useState<ErrorKind>('connection')
  const [errorMsg, setErrorMsg] = useState('')
  const [errorDetail, setErrorDetail] = useState('')
  const [lines, setLines] = useState<Line[]>([])
  const [partial, setPartial] = useState<Partials>({})
  const [secs, setSecs] = useState(0)
  const [muted, setMuted] = useState(false)
  const [confirmEnd, setConfirmEnd] = useState(false)
  const [orbState, setOrbState] = useState<'idle' | 'listening' | 'speaking' | 'thinking'>('idle')
  const [noAudio, setNoAudio] = useState(false)
  const [askedPerson, setAskedPerson] = useState(false)

  const vapiRef = useRef<Vapi | null>(null)
  const ringRef = useRef<HTMLDivElement | null>(null)
  const meterRef = useRef<HTMLDivElement | null>(null)
  const sessionRef = useRef<string | null>(null)
  const callRef = useRef<string | null>(null)
  const finishedRef = useRef(false)
  const startedRef = useRef(false)
  const heardAveryRef = useRef(false)
  const speakingRef = useRef(false)
  const logRef = useRef<HTMLDivElement | null>(null)
  const connectedRef = useRef(false) // Vapi has said it is listening (or Avery has been heard) for the current attempt
  const attemptRef = useRef(0)
  const retriesLeftRef = useRef(MAX_AUTO_RETRIES)
  const lastPostedRef = useRef<string | null>(null) // the call currently registered on the server for this session
  const failsafeRef = useRef<number | null>(null)
  const timelineRef = useRef<string[]>([])
  const tStartRef = useRef(0)

  const setLevel = (v: number) => ringRef.current?.style.setProperty('--lvl', String(Math.max(0, Math.min(1, v))))

  // Warm up: load the voice SDK while the candidate reads the disclosure, and check the network (advisory).
  useEffect(() => {
    if (phase !== 'precall') return
    void loadVapi().catch((e) => console.error('[interview] voice SDK failed to load:', e))
    void webrtcLooksBlocked().then(setNetBlocked)
  }, [phase])

  // The microphone preview stays open while connecting (a warm device is the point) and is released once the call is live or has failed.
  useEffect(() => {
    if (phase === 'incall' || phase === 'error') mic.stop()
  }, [phase, mic.stop])

  const beginPrecall = () => {
    setPhase('precall')
    void mic.start() // inside the click so iOS Safari lets the audio context run
  }

  const finish = useCallback(async (opts?: { dropped?: boolean }) => {
    if (finishedRef.current) return
    finishedRef.current = true
    setConfirmEnd(false)
    const v = vapiRef.current
    vapiRef.current = null
    try { await v?.stop() } catch { /* already closed */ }
    const sid = sessionRef.current
    const call = callRef.current
    if (sid) await api(`/sessions/${sid}/end`, { method: 'POST' }).catch(() => {})
    if (call) nav(to(`/scorecard/${call}`), { replace: true })
    else if (opts?.dropped) { setErrorKind('connection'); setPhase('error') }
  }, [nav])

  // Clean up if the tab is left mid-call
  useEffect(() => () => { void vapiRef.current?.stop().catch(() => {}) }, [])

  // Timer, level polling and the hard length cap while in a call
  useEffect(() => {
    if (phase !== 'incall') return
    const t0 = Date.now()
    const id = window.setInterval(() => {
      const s = Math.floor((Date.now() - t0) / 1000)
      setSecs(s)
      if (s > cfg.max_call_seconds + 20) void finish()
    }, 500)
    const lv = window.setInterval(() => {
      const mine = vapiRef.current?.getLocalAudioLevel() ?? 0
      if (meterRef.current) meterRef.current.style.width = `${Math.min(100, Math.round(mine * 160))}%`
      if (!speakingRef.current) setLevel(mine * 1.5)
    }, 100)
    // If we never hear Avery, say so plainly instead of leaving the candidate in silence.
    const watchdog = window.setTimeout(() => { if (!heardAveryRef.current) setNoAudio(true) }, 12000)
    return () => { clearInterval(id); clearInterval(lv); clearTimeout(watchdog) }
  }, [phase, cfg.max_call_seconds, finish])

  // ---- connecting, with a quiet automatic retry ---------------------------------------------------------------------------------------
  const note = (m: string) => {
    timelineRef.current = [...timelineRef.current.slice(-39), `${((Date.now() - tStartRef.current) / 1000).toFixed(1)}s ${m}`]
    console.info('[interview]', m)
  }
  const clearFailsafe = () => {
    if (failsafeRef.current) window.clearTimeout(failsafeRef.current)
    failsafeRef.current = null
  }

  /** Every attempt failed (or something that must not be retried happened): show the error, forget the failed call, free the slot. */
  const abandon = async (e: unknown, kind?: ErrorKind) => {
    if (!startedRef.current) return // already handled
    startedRef.current = false
    clearFailsafe()
    console.error('[interview] voice unavailable:', e)
    setErrorDetail((d) => d || [describeError(e), ...timelineRef.current].join('\n'))
    const txt = describeError(e).toLowerCase()
    setErrorKind(kind ?? (txt.includes('permission') || txt.includes('notallowed') || txt.includes('microphone') ? 'connection' : 'voice'))
    setPhase('error')
    const v = vapiRef.current
    vapiRef.current = null
    try { await v?.stop() } catch { /* nothing to stop */ }
    if (sessionRef.current) void api(`/sessions/${sessionRef.current}/end?discard=true`, { method: 'POST' }).catch(() => {})
  }

  /** An attempt that never reached "listening": hang it up, wait a moment, and try again in the same session. */
  const connectFailed = async (vapi: Vapi, why: string) => {
    if (vapiRef.current !== vapi || finishedRef.current || connectedRef.current) return
    clearFailsafe()
    note(`attempt ${attemptRef.current} failed: ${why}`)
    vapiRef.current = null
    try { await vapi.stop() } catch { /* already closed */ }
    if (retriesLeftRef.current > 0) {
      retriesLeftRef.current -= 1
      await new Promise((r) => window.setTimeout(r, RETRY_DELAY_MS))
      if (finishedRef.current || !startedRef.current) return
      try { await connectOnce() } catch (e) { await abandon(e) }
      return
    }
    await abandon(new Error(`The call never connected (${why})`), 'connection')
  }

  const connectOnce = async (): Promise<void> => {
    const n = ++attemptRef.current
    connectedRef.current = false
    heardAveryRef.current = false
    speakingRef.current = false
    callRef.current = null
    note(`attempt ${n}: starting`)
    const VapiSdk = await loadVapi()
    const vapi = new VapiSdk(cfg.vapi_public_key)
    vapiRef.current = vapi
    const live = () => vapiRef.current === vapi && !finishedRef.current // events from an abandoned attempt are ignored
    const markConnected = () => {
      if (!live() || connectedRef.current) return
      connectedRef.current = true
      clearFailsafe()
      note(`attempt ${n}: connected`)
      setPhase('incall')
      setOrbState('listening')
    }
    vapi.on('call-start-progress', (e) => { if (live()) note(`${e.stage} ${e.status}${e.duration != null ? ` ${e.duration}ms` : ''}`) })
    vapi.on('call-start', markConnected) // Vapi's "listening": its pipeline is up and receiving our audio
    vapi.on('speech-start', () => {
      if (!live()) return
      markConnected()
      heardAveryRef.current = true; speakingRef.current = true; setNoAudio(false); setOrbState('speaking')
    })
    vapi.on('speech-end', () => { if (live()) { speakingRef.current = false; setOrbState('listening'); setLevel(0) } })
    vapi.on('volume-level', (v) => {
      if (!live()) return
      if (v > 0.02) { markConnected(); heardAveryRef.current = true; setNoAudio(false) }
      if (speakingRef.current) setLevel(v)
    })
    vapi.on('message', (m: { type?: string; role?: string; transcript?: string; transcriptType?: string }) => {
      if (!live()) return
      // SDK types: type is 'transcript' or "transcript[transcriptType='final']"; role 'assistant' | 'user'; transcriptType 'partial' | 'final'
      if (!m.type?.startsWith('transcript') || !m.transcript || (m.role !== 'user' && m.role !== 'assistant')) return
      markConnected()
      const role = m.role
      const text = cleanText(m.transcript)
      if (m.transcriptType === 'final') {
        setLines((l) => [...l, { role, text }])
        setPartial((p) => ({ ...p, [role]: undefined }))
        if (role === 'user') setOrbState((s) => (s === 'speaking' ? s : 'thinking'))
      } else {
        setPartial((p) => ({ ...p, [role]: text }))
      }
    })
    vapi.on('call-end', () => {
      if (vapiRef.current !== vapi || finishedRef.current) return
      if (connectedRef.current) void finish() // Avery hangs up by herself: straight to the scorecard
      else void connectFailed(vapi, 'the call ended before Avery could hear you')
    })
    vapi.on('error', (e: unknown) => {
      const txt = describeError(e).toLowerCase()
      console.error('[interview] voice SDK error event:', e)
      if (!live()) return
      if (!connectedRef.current) {
        // Vapi closing the room on a dead attempt can surface as an error; retry that. Anything else (permission, network to Vapi) is real.
        if (callRef.current && /ejected|meeting has ended|disconnect|ended|left/.test(txt)) void connectFailed(vapi, 'the connection dropped before Avery could hear you')
        else void abandon(e)
        return
      }
      // Mid-call error: a dropped connection ends the call; the transcript so far is still scored.
      if (txt.includes('ejected') || txt.includes('ended') || txt.includes('network') || txt.includes('disconnect')) {
        setErrorKind('dropped'); setPhase('error'); void finish({ dropped: true })
      }
    })
    const call = await vapi.start(cfg.vapi_assistant_id, undefined, undefined, undefined, undefined, { roomDeleteOnUserLeaveEnabled: true })
    if (!call?.id) throw new Error('no call id')
    if (!live()) return // this attempt was already given up on while start() was still running
    callRef.current = call.id
    rememberCall(call.id)
    await api(`/sessions/${sessionRef.current}/call`, { method: 'POST', body: JSON.stringify({ call_id: call.id, replaces: lastPostedRef.current }) })
    lastPostedRef.current = call.id
    note(`attempt ${n}: call created, waiting for Vapi to listen`)
    if (!connectedRef.current && live()) {
      failsafeRef.current = window.setTimeout(() => { void connectFailed(vapi, `no answer after ${CONNECT_FAILSAFE_MS / 1000} s`) }, CONNECT_FAILSAFE_MS)
    }
  }

  const start = async () => {
    if (startedRef.current) return
    startedRef.current = true
    finishedRef.current = false
    setLines([]); setPartial({}); setNoAudio(false); setSecs(0); setConfirmEnd(false); setAskedPerson(false); setMuted(false); setErrorDetail('')
    heardAveryRef.current = false
    connectedRef.current = false
    attemptRef.current = 0
    retriesLeftRef.current = MAX_AUTO_RETRIES
    lastPostedRef.current = null
    timelineRef.current = []
    tStartRef.current = Date.now()
    setPhase('connecting') // the microphone preview stays open until the call is live
    try {
      const { session_id } = await api<{ session_id: string }>('/sessions', { method: 'POST' })
      sessionRef.current = session_id
    } catch (e) {
      startedRef.current = false
      setErrorKind('refused')
      setErrorMsg(e instanceof ApiError ? e.message : '')
      setPhase('error')
      return
    }
    try {
      await connectOnce()
    } catch (e) {
      await abandon(e)
    }
  }

  const toggleMute = () => {
    const v = vapiRef.current
    if (!v) return
    v.setMuted(!muted)
    setMuted(!muted)
  }

  const askForPerson = () => {
    vapiRef.current?.send({ type: 'add-message', message: { role: 'user', content: 'I’d like to speak with a person, please.' }, triggerResponseEnabled: true })
    setAskedPerson(true)
  }

  // ---------------------------------------------------------------------------------------------------------------------------------
  if (phase === 'landing') {
    const r = cfg.role
    return (
      <Shell>
        <Banner />
        <article className="pt-4">
          <Eyebrow>Role #{r.id} · {r.location}</Eyebrow>
          <h1 className="mt-3 text-[2.4rem] leading-[1.08] text-navy-900 sm:text-5xl">{r.title}</h1>
          <p className="serif mt-6 text-xl leading-relaxed text-ink">{r.summary}</p>
          <ul className="mt-8 divide-y divide-line border-y border-line">
            {r.selling_points.slice(0, 3).map((s) => <li key={s} className="py-3.5 leading-relaxed">{s}</li>)}
          </ul>
          {r.ontario && <p className="mt-4 text-sm text-muted">AI-assisted screening is used for this posting.</p>}
          {cfg.paused && <p className="mt-6 rounded-xl bg-warn-soft px-4 py-3 text-warn" role="alert">Interviews are paused right now. Please try again later.</p>}
          {!cfg.voice_configured && <p className="mt-6 rounded-xl bg-warn-soft px-4 py-3 text-warn" role="alert">The voice connection isn’t configured on this server yet.</p>}
          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center">
            <Button onClick={beginPrecall} disabled={cfg.paused || !cfg.voice_configured} className="w-full sm:w-auto">Start your interview</Button>
            <p className="text-sm text-muted">You’ll need a microphone. Headphones help.</p>
          </div>
        </article>
      </Shell>
    )
  }

  if (phase === 'precall') {
    const micOk = mic.status === 'ok'
    const canStart = micOk && mic.heard && agreed
    return (
      <Shell>
        <Banner />
        <h1 className="text-4xl text-navy-900">Before we start</h1>
        <div className="mt-6 space-y-5">
          <Card label="mc">
            <Eyebrow>Step 1</Eyebrow>
            <h2 id="mc" className="serif mt-1 text-2xl text-navy-900">Check your microphone</h2>
            {mic.status === 'asking' && <p className="mt-3 text-muted" role="status">Your browser is asking for microphone access. Please choose Allow.</p>}
            {mic.status === 'denied' && (
              <div className="mt-3 rounded-xl bg-bad-soft px-4 py-3 text-bad" role="alert">
                <p className="font-semibold">Microphone access is blocked.</p>
                <FixList items={[
                  'Click the padlock icon next to the address bar and set Microphone to Allow.',
                  'On iPhone: Settings, Safari, Microphone, then choose Ask or Allow.',
                  'Then press Try again below.',
                ]} />
              </div>
            )}
            {mic.status === 'nomic' && <p className="mt-3 rounded-xl bg-bad-soft px-4 py-3 text-bad" role="alert">No microphone was found. Plug in or enable a microphone (or put in headphones with a mic), then try again.</p>}
            {mic.status === 'error' && <p className="mt-3 rounded-xl bg-bad-soft px-4 py-3 text-bad" role="alert">The microphone stopped working. Check it is connected and not in use by another app, then try again.</p>}
            {(mic.status === 'denied' || mic.status === 'nomic' || mic.status === 'error') && <div className="mt-3"><Button kind="secondary" onClick={() => void mic.start()}>Try again</Button></div>}
            {micOk && (
              <>
                <p className="mt-3 text-sm text-muted">Say “hello”. The bar should move.{mic.label ? ` Using: ${mic.label}.` : ''}</p>
                <div className="mt-3 h-3 overflow-hidden rounded-full border border-line bg-paper-2" role="meter" aria-label="Microphone level" aria-valuemin={0} aria-valuemax={100}>
                  <div ref={mic.barRef} className="h-full bg-accent transition-[width] duration-75 motion-reduce:transition-none" style={{ width: '0%' }} />
                </div>
                {mic.heard
                  ? <p className="mt-2 font-medium text-good" role="status">✓ We can hear you.</p>
                  : <p className="mt-2 text-muted" role="status">Waiting to hear you…</p>}
                {mic.silentTooLong && !mic.heard && (
                  <div className="mt-3 rounded-xl bg-warn-soft px-4 py-3 text-warn" role="alert">
                    <p className="font-semibold">We can’t hear any sound yet.</p>
                    <FixList items={NO_AUDIO_FIXES} />
                  </div>
                )}
              </>
            )}
            {netBlocked && (
              <p className="mt-3 rounded-xl bg-warn-soft px-4 py-3 text-sm text-warn" role="note">
                <strong>Your network may be blocking live audio.</strong> If you are on a VPN, turn it off before starting.
              </p>
            )}
          </Card>

          <Card label="dc">
            <Eyebrow>Step 2</Eyebrow>
            <h2 id="dc" className="serif mt-1 text-2xl text-navy-900">About this conversation</h2>
            <p className="mt-3 text-[17px] leading-relaxed">{DISCLOSURE}</p>
            <label className="mt-5 flex min-h-11 cursor-pointer items-start gap-3">
              <input type="checkbox" className="mt-1 h-5 w-5 accent-[#0f766e]" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />
              <span className="font-medium">I understand</span>
            </label>
            <button className="mt-1 min-h-11 text-sm font-medium text-accent underline underline-offset-4" onClick={() => setPerson((p) => !p)} aria-expanded={person}>
              I’d rather speak with a person
            </button>
            {person && <p className="mt-1 rounded-xl bg-paper-2 px-4 py-3 text-sm" role="status">In the live product this reaches Stefanie’s team.</p>}
          </Card>
        </div>

        <div className="mt-7 flex flex-col gap-3 sm:flex-row sm:items-center">
          <Button onClick={() => void start()} disabled={!canStart} className="sm:min-w-56">Begin interview</Button>
          {!canStart && <p className="text-sm text-muted">{!micOk ? 'Allow your microphone to continue.' : !mic.heard ? 'Say a few words so we can hear you.' : 'Tick “I understand” to continue.'}</p>}
        </div>
      </Shell>
    )
  }

  if (phase === 'connecting') {
    return (
      <Shell>
        <Banner />
        <div className="flex flex-col items-center py-16 text-center" role="status" aria-live="polite">
          <Ring state="thinking" />
          <h1 className="serif mt-8 text-3xl text-navy-900">Connecting to Avery…</h1>
          <p className="mt-2 text-muted">She will say hello as soon as you’re connected.</p>
          <SlowConnectNote />
        </div>
      </Shell>
    )
  }

  if (phase === 'error') {
    const copy: Record<ErrorKind, { h: string; p: string; fixes?: string[] }> = {
      refused: { h: 'We can’t start an interview right now', p: errorMsg || 'Please try again in a few minutes.' },
      connection: { h: 'We couldn’t connect', p: 'Check your internet connection and that your microphone is allowed, then try again.', fixes: NO_AUDIO_FIXES },
      voice: { h: 'The voice connection isn’t available', p: 'Something is blocking the voice connection. A VPN is the first thing to check. It can also be a short technical problem on our side.', fixes: NO_AUDIO_FIXES },
      dropped: { h: 'The call dropped', p: 'Your connection was interrupted. What was said before the drop is kept.', fixes: NO_AUDIO_FIXES.slice(0, 1) },
    }
    const c = copy[errorKind]
    return (
      <Shell>
        <Banner />
        <div className="rounded-2xl border border-bad/30 bg-bad-soft p-5 sm:p-6" role="alert">
          <h1 className="text-3xl text-bad">{c.h}</h1>
          <p className="mt-3 text-ink">{c.p}</p>
          {c.fixes && <FixList items={c.fixes} />}
          {errorDetail && (
            <details className="mt-4 text-sm text-ink">
              <summary className="min-h-8 cursor-pointer">Technical details</summary>
              <p className="mt-1 break-words font-mono text-xs">{errorDetail}</p>
            </details>
          )}
        </div>
        <div className="mt-6 flex flex-col gap-3 sm:flex-row">
          <Button onClick={() => { startedRef.current = false; setPhase('precall'); void mic.start() }}>Try again</Button>
          {callRef.current && <Button kind="secondary" onClick={() => nav(to(`/scorecard/${callRef.current}`))}>View what was captured</Button>}
        </div>
      </Shell>
    )
  }

  // in call
  const ringState: RingState = muted ? 'muted' : orbState === 'speaking' ? 'speaking' : orbState === 'thinking' ? 'thinking' : 'listening'
  return (
    <Shell wide flush>
      <Banner />
      <InCall
        ringState={ringState} lines={lines} partial={partial} secs={secs} maxSecs={cfg.max_call_seconds} muted={muted} noAudio={noAudio}
        askedPerson={askedPerson} confirmOpen={confirmEnd} fixes={NO_AUDIO_FIXES} ringRef={ringRef} meterRef={meterRef} logRef={logRef}
        onMute={toggleMute} onAskPerson={askForPerson} onRequestEnd={() => setConfirmEnd(true)} onKeep={() => setConfirmEnd(false)} onEnd={() => void finish()}
      />
      <span className="sr-only" aria-live="polite">{STATE_TEXT[ringState]}{STATE_SUB[ringState] ? `. ${STATE_SUB[ringState]}` : ''}</span>
    </Shell>
  )
}
