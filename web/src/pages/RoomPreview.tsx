// Development-only: renders the in-call view with canned data so every state can be reviewed (and screenshotted) without a real call.
// Reached at /preview/room?state=speaking|listening|thinking|muted&confirm=1&noaudio=1&empty=1. Not included in the production build.
import { useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Banner, InCall, type Line, type RingState } from '../room'
import { Shell } from '../ui'
import { NO_AUDIO_FIXES } from './Interview'

const LINES: Line[] = [
  { role: 'assistant', text: 'Hi, I’m Avery, IN DEMAND’s AI screening assistant. Shall we start?' },
  { role: 'user', text: 'Yes, ready when you are.' },
  { role: 'assistant', text: 'Great. What’s prompting you to look at a move right now, and what are you hoping this next role gives you?' },
  { role: 'user', text: 'I’ve been at the same plant for eleven years and I’d like to lead a team and own more of the project side.' },
  { role: 'assistant', text: 'That makes sense. Tell me about the industrial electrical power systems you’ve been responsible for, things like motor control centres and variable frequency drives. What was your role?' },
]

export default function RoomPreview() {
  const [q] = useSearchParams()
  const state = (q.get('state') ?? 'speaking') as RingState
  const logRef = useRef<HTMLDivElement | null>(null)
  const lines = q.get('empty') ? [] : LINES
  const partial = state === 'listening' ? { user: 'So on the last shutdown I redesigned the' } : {}
  return (
    <Shell wide flush>
      <Banner />
      <InCall
        ringState={state} lines={lines} partial={partial} secs={Number(q.get('secs') ?? 272)} maxSecs={960} muted={state === 'muted'} noAudio={!!q.get('noaudio')}
        askedPerson={!!q.get('person')} confirmOpen={!!q.get('confirm')} fixes={NO_AUDIO_FIXES} logRef={logRef}
        onMute={() => {}} onAskPerson={() => {}} onRequestEnd={() => {}} onKeep={() => {}} onEnd={() => {}}
      />
    </Shell>
  )
}
