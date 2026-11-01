# UI-SPEC.md — Phase 2: what Stefanie sees (v1)

> Claude Code: read this and CLAUDE.md before starting. The engine is frozen. Build the product around it. Report in CLAUDE.md §12 format, kept short, after each numbered build step. Stop after each step.

## 1. Goal and scope

Stefanie Howse (Principal, IN DEMAND Recruitment) opens one link, plays a candidate for about 10 minutes, then sees a recruiter scorecard and a ranked shortlist. She should think "we could use this" and book a call with NeuralicAI.

**v1 (build now):** Interview Room, Scorecard, Dashboard, Roadmap cards, mobile pass, error states.
**v1.1 (NOT now, sent later as a follow-up):** Live Recruiter View with question injection, résumé upload, emails, other roles, Meet clip.
Only role #5905 is live.

## 2. Architecture

- React + Vite + Tailwind. The Python server serves the built frontend from the same origin (no CORS), as the PakBot package did.
- Voice: Pipecat JS client + SmallWebRTC transport, as in the playground. Do NOT reuse the playground UI.
- Backend adds REST endpoints on the existing server: roles, interviews, scorecard, dashboard. Storage is SQLite plus wav files on disk (no Supabase for v1). Dashboard is behind a passcode (env var, cookie).
- Deploy is undecided (Fly + TURN vs Daily). Keep the transport swappable. Don't block on it.

## 3. Prototype guardrails (non-negotiable)

- Footer on every page: "Prototype prepared for IN DEMAND Recruitment by NeuralicAI."
- Banner on the Interview Room: "Prototype for IN DEMAND to evaluate. Please don't enter real candidate details."
- Use the firm's name as text only. No logo images, award badges, Forbes marks or testimonials. Navy and white palette with one restrained accent.
- noindex meta + robots.txt disallow. No analytics trackers. No résumé or email fields in v1.
- Starting an interview requires an unguessable token in the URL (?k=...). Invalid token shows a generic page. Add env caps: max concurrent sessions (default 1), daily session cap, and a kill switch, so credits can't drain. Max call length is already 16 min.
- Sample candidates are labelled "Sample candidate (simulated)" everywhere, never presented as real people.

## 4. Interview Room (/interview)

States, in order:
1. **Landing:** role card ("Electrical Engineering Supervisor, Edmonton, AB"), 3-line summary from roles/5905.yaml only, "Start your interview".
2. **Pre-call:** mic permission, live mic level meter, headphones tip, and the disclosure text exactly:
   "This is a short first conversation (about 10 to 15 minutes) with Avery, IN DEMAND's AI screening assistant. It's recorded so Stefanie's team can review it personally. AI assists the screening and doesn't replace human judgment. You can ask for a person at any time."
   A required checkbox "I understand". A secondary link "I'd rather speak with a person" shows: "In the live product this reaches Stefanie's team."
3. **In-call:** animated orb (listening / speaking / thinking, driven by audio level), live transcript (candidate and Avery), timer, mute, End (with confirm), "Ask for a person". Optional thin progress bar from stage name.
4. **Error states:** mic denied (how to fix), connection failed (Retry), voice unavailable (show the server's message: "We're having a technical problem. Your answers so far were saved."), dropped call.
5. **Post-call:** "Thanks. Stefanie's team reviews every interview personally, and if it's a fit they'll be in touch." (No timeline.) Then "View the recruiter scorecard" and a generating state (target 60 s).
The Avery greeting must start without waiting on UI animations. Pre-warm the connection on the pre-call screen.

## 5. Scorecard (/scorecard/:id): the closer

Top to bottom:
- One-line headline, a recommendation chip (Submit / Hold / Pass), overall score, label "AI-assisted summary for human review".
- Knockouts row: four items with pass / fail / unclear and the candidate's quote on expand.
- Six competency cards: score 1-5, confidence, verified quotes with timestamps, and "Play from 03:12" using the session recording.
- Strengths, risks, "Verify on your call" (3 items), logistics (salary, notice, location), items deferred to Stefanie (client, benefits).
- Collapsible clean transcript. Protected-ground content never appears. Small footer: model and prompt versions.
- Sticky call-to-action: "Book a 15-min call with NeuralicAI" using BOOKING_URL (env), default mailto neuralicstudio@gmail.com with a prefilled subject.

Scorecard generation (post-call worker):
- gpt-5.6-sol at medium reasoning, fallback gpt-5.6-terra, strict structured output in the CLAUDE.md §10 shape.
- Validate score ranges in code. Verify every quote with the existing quote_validation module, and drop or mark any that fail. Strip protected-ground content.
- If it fails twice, show "Scorecard unavailable, retry", never a broken page.

## 6. Dashboard (/dashboard, passcode)

Role header and a ranked table: rank, candidate, score, recommendation, knockout marks, top flag, date. Click through to the scorecard. Pre-load 3 sample candidates (strong, borderline, weak) by running the existing simulator through the same pipeline (stub voice, fictional names, labelled "Sample candidate (simulated)"). Stefanie's own session appears as "Live demo session".

## 7. Roadmap panel (dashboard and scorecard footer only, never in the candidate room)

Five cards, each with the badge "Planned, not built yet" and a "Yes, build this" button (mailto to NeuralicAI, prefilled subject like "Yes, build: client reports"). Never mock them to look live.
1. Re-engage your candidate database
2. Client-ready search reports
3. ATS sync (button asks "Which ATS do you use?")
4. Joins your Meet, Zoom or Teams calls
5. Interview integrity checks (ID and fraud detection)

## 8. Design notes

Calm, professional, not "techy". Mobile-first (360 px minimum, 16 px gutters, no horizontal scroll). Works in Chrome desktop, mobile Safari and mobile Chrome. Accessible: keyboard navigation, readable contrast, captions via the live transcript.

## 9. Build order (stop and report after each)

1. Backend: endpoints, SQLite, recording storage, scorecard worker, run the 3 simulated sessions for the shortlist.
2. Interview Room (all states, pre-warm, error states).
3. Scorecard page.
4. Dashboard and Roadmap panel.
5. Mobile and error-state pass, session caps and token gate.
Use the stub voice for all automated tests. Real voice only in my live tests. Never print keys or PIPECAT_ICE_SERVERS.

## 10. Acceptance

- Room loads under 3 s on mobile data; Avery speaks within about 2 s of pressing Start.
- A full interview completes and the scorecard appears within 60 s, with every quote verified in the transcript.
- Dashboard shows the 3 samples and the live session, correctly labelled.
- All error states are reachable and readable. No horizontal scroll at 360 px.
- Guardrails in section 3 are all in place.
