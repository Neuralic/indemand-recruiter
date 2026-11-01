# IN DEMAND AI Recruiter — Build Brief

> Drop this file in the repo root as `CLAUDE.md`. Claude Code: read it fully before every phase. Work **one phase at a time**, then stop and report (format in §12).

> **§14 (Decisions after Phase 0b) overrides §4, §5, §6 and §7 wherever they conflict.** Read §14 first.

---

## 1. Mission

Build a demo good enough that **Stefanie Howse** (Principal, IN DEMAND Recruitment & Consulting, Edmonton) opens one link, plays a candidate for 5 minutes, and books a call.

- **Client:** boutique executive search firm, 25+ years. They place Plant Managers, Maintenance/Reliability leaders, Ops execs, and Engineering Managers in manufacturing, oil & gas, petrochem, mining, utilities, and automation, across Canada and the USA.
- **Brand:** warm, fast, relationship-driven. Stefanie is a CPHR and award-winning recruiter.
- **Positioning:** *"Your first-round screener. Every inbound applicant is interviewed tonight; you call only the top 3 tomorrow."* It never replaces Stefanie. The agent explicitly hands off to her.
- **Deadline:** demo link in her inbox within ~72 hours.

## 2. Team roles and working rules

- **Architect (chat Claude):** design decisions, specs, and reviews. Owns this file.
- **Implementer (Claude Code):** writes all code, runs tests, and reports.
- **David:** relays reports between the two, tests by voice, and makes the final calls.

**Rules:**
1. **Verify before building.** If a library or model capability is uncertain, check the installed source or docs first. Never assume an API shape.
2. **Logs over vibes.** "It sounded fine" is not a pass. Every test is confirmed against logs or transcripts.
3. **Secrets:** never ask David to paste keys into chat. Keys go in `.env` (gitignored) or are set with `setx`. Never hard-code them.
4. **Deterministic guardrails over prompt wording.** If a behaviour must always happen (ending the call, never asking a protected question), enforce it in code where possible.
5. Small, reviewable commits per phase.
6. Do not start the next phase without David's go-ahead.

## 3. Product: three surfaces

| Surface | User | What it does |
|---|---|---|
| **Interview Room** `/interview/:roleId` | Candidate (Stefanie playing one) | Branded, no signup. Optional résumé upload, mic check, AI disclosure plus consent, live voice interview with a transcript and an animated agent orb. |
| **Live Recruiter View** `/live/:interviewId` | Recruiter | Streams the transcript, plus a competency map that fills in as evidence is logged, knock-out status, and an **"Ask this next"** box that pushes a question into the live interview. |
| **Dashboard** `/dashboard` and `/dashboard/:roleId` | Recruiter | Roles → ranked candidate shortlist → full scorecard with quotes, timestamps, audio clips, and a recommendation. |

Footer on every page: *"Prototype prepared for IN DEMAND Recruitment by NeuralicAI."* The dashboard sits behind a simple shared passcode, not a full auth system.

## 4. Architecture

```
Browser (React)
  ├─ Interview Room ──WebRTC (Daily)──► Pipecat bot (Python)
  │                                        ├─ OpenAIRealtimeLLMService (gpt-realtime-2.1)
  │                                        ├─ tool calls ─► Supabase (evidence, stage, knockouts)
  │                                        └─ subscribes to recruiter_questions (Supabase Realtime)
  ├─ Live View ◄── Supabase Realtime (transcript_turns, evidence)
  └─ Dashboard ◄── Supabase (scorecards)

Post-call worker: full transcript + evidence ─► OpenAI reasoning model (structured output) ─► scorecard
                                            └─► Resend: candidate thank-you + recruiter report email
```

**Stack:** Python 3.12 + uv, Pipecat (latest stable), Daily transport, OpenAI Realtime, Supabase (Postgres, Realtime, Storage), Resend, React + Vite + Tailwind. Deploy target is decided in Phase 0.

**Fallback voice path (only if Phase 0 says so):** a cascaded pipeline of streaming STT with keyword boosting → OpenAI LLM → Cartesia TTS, using the same tools and prompts.

## 5. Core design: the interview is driven by tool calls

The realtime model conducts the conversation. It must **report what it learns through tool calls**, which power the live view and the scorecard.

| Tool | Args | Effect |
|---|---|---|
| `advance_stage` | `stage` | Updates the current stage (enforces order, see §6) |
| `log_evidence` | `competency_id, quote, assessment (strong/adequate/weak), note` | Writes an evidence row with a server timestamp → live competency map |
| `record_knockout` | `knockout_id, answer, status (pass/fail/unclear)` | Knock-out panel |
| `flag_for_recruiter` | `topic, reason` | Feeds the "Verify on your call" list |
| `end_interview` | `reason` | Plays the closing, then a deterministic shutdown |

**Code-level enforcement:**
- **Stage gate:** the bot can't jump to `close` before `logistics` is done (unless the candidate asks to stop).
- **Time budget:** soft wrap at 13 minutes, hard close at 16.
- **Silence:** a gentle check-in after 8 seconds, a second after 20, and a graceful end after 45.
- **Protected-grounds filter** (see §7) runs on every agent utterance before it's committed. If it trips, regenerate.
- **Recruiter injection:** when `recruiter_questions` gets a row, append a system message: *"The recruiter would like you to naturally work in: <q>. Ask it at the next natural turn."* Mark the row as consumed.

> Phase 0 must confirm that function calling works reliably with `OpenAIRealtimeLLMService` on the chosen model, and whether Pipecat Flows supports the realtime service. If Flows does not, the tool-driven stage machine above is the design; don't force Flows.

## 6. Interview flow

1. **Welcome and disclosure (≤30s).** Something like: "Hi {first_name}, I'm Avery, IN DEMAND's AI screening assistant. This is a short first conversation about the {role} role. It's recorded so Stefanie's team can review it personally. A human reviews every interview, and no decision is made by AI. If you'd prefer to speak with a person instead, just say so. Shall we start?" If they decline → `flag_for_recruiter` and end politely.
2. **Rapport and motivation.** Why open to a move now? What do they know about the role?
3. **Background, résumé-aware.** **Never** ask them to walk through the résumé. Ask about one or two specific claims, gaps, or transitions from the parsed CV. If there's no CV, ask for a two-minute career snapshot instead.
4. **Competency deep-dive.** The role config lists 4–6 competencies. For each, ask one primary question. **If the answer is vague, probe once** for a specific example: situation, what *they* did, and a number. Refer back to earlier answers naturally. Call `log_evidence` after each substantive answer.
5. **Logistics and knock-outs.** Licences/tickets, work authorization, location/relocation, salary expectations, notice period, travel or shift requirements. Call `record_knockout` for each.
6. **Candidate questions.** Answer **only** from the role config. If unknown: "That's a great one for Stefanie. I'll make sure she covers it." → `flag_for_recruiter`.
7. **Close.** Explain next steps ("Stefanie's team reviews every interview within one business day"), thank them warmly, then `end_interview`.

**Voice style:** warm Canadian-professional, conversational, short turns (1–3 sentences), uses the candidate's name sparingly. Brief acknowledgements are fine ("That's helpful, thanks"). It doesn't over-praise and never repeats a question that was already answered.

## 7. Guardrails (non-negotiable)

- **Protected grounds (Alberta Human Rights Act + general Canadian practice):** never ask about age, race, colour, ancestry, place of origin, religious beliefs, gender, gender identity/expression, sexual orientation, marital or family status, pregnancy, disability (physical or mental), or source of income. Work authorization is asked **only** as "Are you legally eligible to work in {country}?" If a candidate volunteers protected info, don't react to it, don't log it, and don't let it reach the scorecard.
- **No fabrication** about the client company, salary, or benefits beyond the role config.
- **Scope:** off-topic or jailbreak attempts get a polite redirect. After three in a row, close gracefully. Reuse the PakBot ScopeGate pattern.
- **Disclosure:** AI disclosure and the "talk to a person" option are always offered first, and are not skippable.
- **Audit:** every interview stores the full transcript, all tool calls, and model and prompt versions.
- **Ontario note:** roles located in Ontario show a small line on the role page: "AI-assisted screening is used for this posting." (Ontario ESA AI disclosure, effective Jan 1 2026.)

## 8. Role configs

`roles/<id>.yaml`. Source text goes in `roles/raw/<id>.txt` (David pastes it in, because indemand.ca blocks automated fetching). Build configs for **#5905 Electrical Engineering Supervisor (Edmonton)**, **#5904 Operations Manager – Industrial Automation (North GTA)**, and **#5907 HSE Manager – Solar & BESS (Alamogordo, NM)**.

```yaml
id: "5905"
title: "Electrical Engineering Supervisor"
location: "Edmonton, AB, Canada"
country: "Canada"
ontario: false
summary: "<2-3 sentences, from posting>"
selling_points: ["<only from posting>"]
facts_for_candidate_questions: { reporting_to: "", schedule: "", compensation: "", benefits: "" }  # blank = unknown → defer to Stefanie
competencies:
  - id: tech_leadership
    name: "Electrical engineering leadership"
    primary_question: "..."
    probe_if_vague: "..."
    strong_signals: ["..."]
    weak_signals: ["..."]
    weight: 0.25
knockouts:
  - { id: work_auth, question: "Are you legally eligible to work in Canada?", fail_if: "no" }
  - { id: licence, question: "Do you hold, or are you eligible for, P.Eng registration with APEGA?", fail_if: "no and not eligible" }
domain_terms: ["APEGA", "P.Eng", "CEC", "arc flash", "MCC", "switchgear", "PLC", "SCADA", "LOTO", "CMMS", "SAP PM", "Maximo", "RCM", "MTBF"]
```

`domain_terms` feed transcription prompting/keyword boosting and the persona's vocabulary. The persona must sound like it has 20 years in heavy industry.

## 9. Data model (Supabase)

`roles`, `candidates` (name, email, resume_path, parsed_resume jsonb), `interviews` (role_id, candidate_id, status, stage, started_at, ended_at, recording_path, model, prompt_version, is_sample bool), `transcript_turns` (interview_id, speaker, text, t_start, t_end), `evidence` (interview_id, competency_id, quote, assessment, note, t), `knockouts`, `recruiter_flags`, `recruiter_questions` (interview_id, text, consumed_at), `scorecards` (interview_id, json, overall_score, recommendation).

Enable Realtime on `transcript_turns`, `evidence`, `knockouts`, and `interviews`.

## 10. Scorecard (post-call)

Use OpenAI's strongest available reasoning model with **structured output** (verify the current model name in Phase 0). Inputs: the role config, the full transcript, the logged evidence, and knock-outs.

```json
{
  "overall_score": 0-100,
  "recommendation": "Submit | Hold | Pass",
  "headline": "one-line summary in Stefanie's voice",
  "competencies": [{ "id": "", "score": 1-5, "confidence": "high|med|low",
                     "evidence": [{ "quote": "", "t": 123.4 }], "rationale": "" }],
  "knockouts": [{ "id": "", "status": "pass|fail|unclear", "answer": "" }],
  "strengths": [""], "risks": [""],
  "verify_on_call": ["3 specific things Stefanie should confirm"],
  "candidate_questions_to_answer": [""]
}
```

**Rules:**
- Every score must cite at least one verbatim quote that exists in the transcript. Validate this in code and drop or mark any quote that isn't found.
- Protected-ground content is stripped before scoring.
- Clicking a quote plays the recording from that timestamp.

## 11. Phases and acceptance criteria

**Phase 0 — Verification spike (no product code).** Report findings and a recommendation on each:
1. Latest stable Pipecat version. Does `OpenAIRealtimeLLMService` accept `gpt-realtime-2.1` (model set via settings)? Run a one-minute live test.
2. Is function calling reliable on that model through Pipecat? Run 10 scripted turns and count tool calls fired versus expected.
3. Do Pipecat Flows support the realtime service? Yes/no, with evidence.
4. **Jargon test:** read the `domain_terms` list aloud in natural sentences and measure transcription accuracy. If more than 2 terms are consistently wrong after prompting, recommend the cascaded fallback.
5. Available realtime voices. Shortlist 2 warm, neutral-North-American options for David to pick.
6. Daily transport works end-to-end locally. Deploy recommendation for the bot (Render with Daily vs Pipecat Cloud vs Fly), with cost and the known PakBot Render dropout issue in mind.
7. Current name of OpenAI's best reasoning model for the scorecard step.

**Phase 1 — Voice core.** The bot runs a full #5905 interview end-to-end by voice through all §6 stages, with all §5 tools firing, plus the stage gate, time budget, silence handling, and clean shutdown.
*Accept:* 3 full test interviews (strong, vague, off-topic candidates). Logs show every tool call, no protected questions, a probe fired on vague answers, and a clean end with no manual disconnect.

**Phase 2 — Data and Live View.** Supabase schema, transcript and evidence streaming, recruiter question injection.
*Accept:* David runs an interview on his laptop and watches the live view on his phone. The competency map fills in, and an injected question gets asked within two turns.

**Phase 3 — Scorecard and Dashboard.** Post-call worker, quote validation, recording playback from timestamp, ranked shortlist.
*Accept:* the scorecard appears within 60 seconds of hangup, and every quote is verified to exist in the transcript.

**Phase 4 — Polish and demo data.**
- IN DEMAND-style branding: navy/white, their tagline "Our Talent Is Finding Yours," and the footer from §3.
- Résumé upload and parsing.
- Resend emails.
- Run **3 sample candidates** per role by having an LLM play scripted personas through the same interview logic (strong, borderline, weak). These must be clearly labelled **"Sample candidate (simulated)"** everywhere. They are never presented as real people.
*Accept:* the dashboard opens to a populated, ranked #5905 shortlist, and sample labels are visible.

**Phase 5 — Hardening and deploy.** Run the test matrix: interruptions/barge-in, long pauses, a candidate asking for a human, jailbreak, protected-info volunteering, salary question with no data, a 16-minute rambler, a bad network, and mobile Safari plus Chrome.
*Accept:* every case passes against logs. The public URL loads in under 3 seconds, and the agent speaks first in under 2 seconds.

**Stretch (only after Phase 5):** a Recall.ai Google Meet bot, recorded as a 30-second clip for the email, not shown live.

## 12. Report format (Claude Code → David → Architect)

```
PHASE: <n> — <done | blocked>
DONE: <bullets>
VERIFIED (how): <log/transcript evidence>
DEVIATIONS FROM BRIEF: <what + why>
OPEN QUESTIONS / DECISIONS NEEDED: <numbered>
NEXT: <proposed next step>
```

## 13. Out of scope for the demo

ATS integrations, multi-tenant auth, billing, phone/SIP dial-in, languages other than English, and live Google Meet.

---

## 14. Decisions after Phase 0b (David, 2026-10-17). These override §4, §5, §6, §7 where they conflict.

**Architecture (Arm D):**
- `gpt-realtime-2.1` is the brain: audio in, **text out** (`output_modalities=["text"]`). **Cartesia `sonic-3.6`** is the voice. Default voice **Harlow**; **Max** switchable via an env var.
- **TTS fallback:** if Cartesia errors (402, disconnect), fall back to the native `marin` voice mid-session without ending the interview.
- **Turn detection:** `server_vad`, ~600 ms silence, configurable. Tune with David's real speech.
- **Transcription:** `gpt-4o-transcribe` with the `domain_terms` prompt.
- **Flows are out.** Use the tool-driven stage machine; stage instructions change via `LLMUpdateSettingsFrame`.
- **Pre-warm:** open the OpenAI connection before the user clicks Start.
- **Pronunciation is fixed in the script.** The agent says "professional engineer" and "lockout-tagout" in full, never "P.Eng" or "LOTO". APEGA, SCADA and Maximo stay as words.
- **Never** put markup or direction tags (SSML, emotion tags, etc.) in text sent to Cartesia unless verified safe.

**Phase 1 changes to §5/§6 (supersedes the per-turn tool design):**
- The realtime model **only talks**. It has no per-turn evidence tools.
- A **background evaluator** (text LLM; `terra` at low reasoning first) watches transcript turns and writes evidence, knock-outs, stage advances and recruiter flags. The realtime model never waits on it.
- **Quote validation:** every evidence quote is normalized and fuzzy-matched against the transcript with a strict threshold. A quote that is not in the transcript is rejected.
- **Stage gate in code:** one step at a time; `logistics` exits only after the required knock-outs are recorded.
- Role config #5905 is a **placeholder** until David pastes the real posting. Placeholders are marked `PLACEHOLDER` in the YAML.
- Live tests run **one at a time**. Keys are never printed or echoed.
