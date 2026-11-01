// Access is one unguessable token in the URL (?k=...). It is remembered for the tab and sent on every API call.
const KEY = 'indemand.k'

function readToken(): string {
  try {
    const fromUrl = new URLSearchParams(location.search).get('k')
    if (fromUrl) {
      sessionStorage.setItem(KEY, fromUrl)
      return fromUrl
    }
    return sessionStorage.getItem(KEY) ?? ''
  } catch {
    return new URLSearchParams(location.search).get('k') ?? ''
  }
}

export const token = readToken()

/** Internal link that keeps the token, so a copied or reloaded URL still works. */
export const to = (path: string) => `${path}${path.includes('?') ? '&' : '?'}k=${encodeURIComponent(token)}`

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { 'x-access-token': token, ...(init.body ? { 'content-type': 'application/json' } : {}), ...init.headers },
  })
  if (!res.ok) {
    let detail = ''
    try {
      detail = (await res.json()).detail ?? ''
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail || res.statusText)
  }
  return res.json() as Promise<T>
}

export const recordingUrl = (id: string) => `/api/interviews/${id}/recording?k=${encodeURIComponent(token)}`

// Call ids this browser ran, so the dashboard can rebuild them from Vapi if the host lost its disk.
const CALLS = 'indemand.calls'
export function rememberCall(id: string) {
  try {
    const all = knownCalls().filter((c) => c !== id)
    localStorage.setItem(CALLS, JSON.stringify([id, ...all].slice(0, 5)))
  } catch {
    /* storage blocked: fine */
  }
}
export function knownCalls(): string[] {
  try {
    return JSON.parse(localStorage.getItem(CALLS) ?? '[]')
  } catch {
    return []
  }
}

export interface RoleCard {
  id: string
  title: string
  location: string
  summary: string
  selling_points: string[]
  ontario: boolean
}
export interface Config {
  role: RoleCard
  booking_url: string
  sample_label: string
  live_label: string
  vapi_public_key: string
  vapi_assistant_id: string
  voice_configured: boolean
  paused: boolean
  max_call_seconds: number
}
export interface Evidence {
  quote: string
  t: number
  verified: boolean
}
export interface Competency {
  id: string
  name: string
  weight: number
  score: number
  confidence: 'high' | 'med' | 'low'
  evidence: Evidence[]
  rationale: string
  evidence_note: string
}
export interface Knockout {
  id: string
  question: string
  required: boolean
  status: 'pass' | 'fail' | 'unclear'
  answer: string
  quote: string
  quote_verified: boolean
  t: number | null
  note: string
}
export interface Scorecard {
  overall_score: number
  recommendation: 'Submit' | 'Hold' | 'Pass'
  recommendation_note: string
  headline: string
  summary_label: string
  competencies: Competency[]
  knockouts: Knockout[]
  knockouts_required_passed: number
  knockouts_required: number
  strengths: string[]
  risks: string[]
  verify_on_call: string[]
  candidate_questions_to_answer: string[]
  logistics: Record<string, { answer: string; quote: string; status: string }>
  not_in_posting: string[]
  quote_check: { proposed: number; dropped: number }
  versions: { scorecard_model?: string; scorecard_prompt?: string }
}
export interface ScorecardResponse {
  id: string
  status: 'waiting' | 'generating' | 'ready' | 'failed' | 'unusable'
  label: string
  is_sample: boolean
  candidate_name: string
  has_recording: boolean
  duration_s: number | null
  rebuilt?: boolean
  message?: string
  scorecard?: Scorecard
  generated_in_s?: number
}
export interface Row {
  rank: number | null
  id: string
  candidate_name: string
  label: string
  is_sample: boolean
  sample_kind: string | null
  status: string
  score: number | null
  recommendation: string | null
  knockouts: { id: string; status: string }[]
  top_flag: string | null
  headline: string | null
  date: number
}
export interface Turn {
  speaker: 'candidate' | 'interviewer'
  text: string
  t_start: number
  t_end: number
}
