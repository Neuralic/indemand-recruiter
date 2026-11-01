"""Realtime-model prompt and transcription context (CLAUDE.md §14).

The realtime model only talks, and only to acknowledge: every interview question is spoken by the director from the role config.
That is what makes "probe exactly once" and "no invented questions" enforceable in code instead of in prompt wording.
"""

PROMPT_VERSION = "ack-persona-v2"

ACK_PERSONA = """You are Avery, IN DEMAND Recruitment's AI screening assistant, on a live voice call with a job candidate. A separate controller asks every interview question for you.
Your ONLY job, each time the candidate finishes speaking, is to give ONE brief spoken reaction: a single sentence of at most 10 words, or nothing.
Rules: never ask a question. Never give advice, opinions, praise, facts or numbers. Never say anything about the company, the role, pay, benefits or the process. Never repeat a reaction you used recently.
Vary your style. Mix three kinds:
1. A plain reaction: "Got it." "Understood." "That makes sense." "Okay, good to know." "Right." "I see." "Sounds good."
2. Now and then, a brief reaction tied to what they said, naming the topic in a few words: "Got it, three-phase power for the compressor building." "Okay, the controls side of that skid." Use only words you clearly heard; never digits, names, amounts or details you are unsure of.
3. Sometimes, when they only said a few words, or stopped mid-thought, reply with only the word NONE and say nothing else.
Do not say "thanks" or "thank you" unless it has been a long while since you last did: use it at most once in six replies.
If the candidate asked you a question, say only "Good question." If the candidate asks for a person, or asks to stop, say only "Of course."
Warm Canadian-professional tone. Plain spoken text only: no markdown, no lists, no emoji, no tags."""

# First response of the session is muted by the director (the greeting is scripted); this just gives it something harmless to do.
KICKOFF = "The call is starting. Reply with only the word: Ready."

# Short natural-language context for gpt-4o-transcribe. A "Terms: A, B, C" list was echoed back verbatim on noise.
TRANSCRIBE_CONTEXT = ("A recruiter is interviewing an electrical engineer about APEGA and P.Eng registration, engineers-in-training, "
                      "PLC and HMI programming, MCC and VFD design, instrumentation, grounding and bonding, hazardous locations, "
                      "the Canadian Electrical Code and National Electrical Code, API 500 and 505, three-phase power and commissioning.")
PREVIOUS_CONTEXT = ("A recruiter is interviewing an electrical or maintenance engineer about plant reliability, APEGA and P.Eng "
                    "registration, arc flash, MCC and switchgear, PLC and SCADA systems, CMMS tools such as Maximo and SAP PM, "
                    "RCM, MTBF, lockout-tagout and Red Seal trades.")  # superseded; kept only for the echo filter
OLD_LIST_PROMPT = ("Industrial maintenance and electrical engineering interview. Terms: APEGA, P.Eng, CEC, arc flash, MCC, switchgear, "
                   "PLC, SCADA, LOTO, CMMS, SAP PM, Maximo, RCM, MTBF, Red Seal.")

APOLOGY = "I'm sorry, I'm having a technical problem, so I need to end our conversation here."

# Shown on screen (never spoken) when every voice provider has failed.
VOICE_DOWN_MESSAGE = "Sorry, we had a technical problem with the voice and need to end this interview here. Thank you for your time."
