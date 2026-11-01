import { useCallback, useEffect, useRef, useState } from 'react'

export type MicStatus = 'idle' | 'asking' | 'ok' | 'denied' | 'nomic' | 'error'

const HEARD_LEVEL = 0.035 // RMS on a 0..1 scale: speech at normal distance is well above this, an unplugged mic is ~0
const HINT_AFTER_MS = 6000

/** Mic permission + live level meter. start() must be called from a click so iOS Safari lets the audio context run. */
export function useMicCheck() {
  const [status, setStatus] = useState<MicStatus>('idle')
  const [label, setLabel] = useState('')
  const [heard, setHeard] = useState(false)
  const [silentTooLong, setSilentTooLong] = useState(false)
  const barRef = useRef<HTMLDivElement | null>(null)
  const heardRef = useRef(false)
  const stopRef = useRef<() => void>(() => {})

  const stop = useCallback(() => {
    stopRef.current()
    stopRef.current = () => {}
  }, [])

  const start = useCallback(async () => {
    stop()
    heardRef.current = false
    setHeard(false)
    setSilentTooLong(false)
    setStatus('asking')
    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
    } catch (e) {
      const name = (e as DOMException)?.name
      setStatus(name === 'NotAllowedError' || name === 'SecurityError' ? 'denied' : name === 'NotFoundError' || name === 'OverconstrainedError' ? 'nomic' : 'error')
      return
    }
    const track = stream.getAudioTracks()[0]
    setLabel(track?.label ?? '')
    const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    const ctx = new Ctx()
    void ctx.resume()
    const analyser = ctx.createAnalyser()
    analyser.fftSize = 1024
    ctx.createMediaStreamSource(stream).connect(analyser)
    const buf = new Uint8Array(analyser.fftSize)
    let raf = 0
    let loud = 0
    const t0 = performance.now()
    const tick = () => {
      analyser.getByteTimeDomainData(buf)
      let sum = 0
      for (const v of buf) sum += ((v - 128) / 128) ** 2
      const rms = Math.sqrt(sum / buf.length)
      if (barRef.current) barRef.current.style.width = `${Math.min(100, Math.round(rms * 400))}%`
      loud = rms > HEARD_LEVEL ? loud + 1 : 0
      if (loud >= 4 && !heardRef.current) {
        heardRef.current = true
        setHeard(true)
        setSilentTooLong(false)
      }
      if (!heardRef.current && performance.now() - t0 > HINT_AFTER_MS) setSilentTooLong(true)
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    track?.addEventListener('ended', () => setStatus('error'))
    stopRef.current = () => {
      cancelAnimationFrame(raf)
      stream.getTracks().forEach((t) => t.stop())
      void ctx.close().catch(() => {})
    }
    setStatus('ok')
  }, [stop])

  useEffect(() => stop, [stop])
  return { status, label, heard, silentTooLong, barRef, start, stop }
}

/** Advisory only: can this network gather a public (server-reflexive) WebRTC address? None often means a VPN or strict firewall. */
export async function webrtcLooksBlocked(): Promise<boolean> {
  if (typeof RTCPeerConnection === 'undefined') return true
  const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] })
  try {
    pc.createDataChannel('probe')
    let srflx = false
    pc.onicecandidate = (e) => {
      if (e.candidate?.candidate.includes(' typ srflx')) srflx = true
    }
    await pc.setLocalDescription(await pc.createOffer())
    await new Promise<void>((resolve) => {
      const done = () => resolve()
      pc.onicegatheringstatechange = () => pc.iceGatheringState === 'complete' && done()
      setTimeout(done, 4000)
    })
    return !srflx
  } catch {
    return false // can't tell: say nothing
  } finally {
    pc.close()
  }
}
