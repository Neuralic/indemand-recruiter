"""Deterministic guardrails (CLAUDE.md §7, §14). Pure functions, no I/O."""

from __future__ import annotations

import re

# Protected grounds (Alberta Human Rights Act + general Canadian practice). Used three ways: reject role-config spoken text that
# touches them, reject a model acknowledgement that mentions them, and drop evidence quotes/notes that contain them.
_PROTECTED_TERMS = [
    r"\bage\b", r"\brace\b", r"\bracial\b", r"\bethnic\w*",
    r"\bcolou?r\b", r"\bancestry\b", r"\bnationality\b", r"\bcitizen\w*", r"\bborn\b", r"\bwhere (?:are )?you(?:'re| are)? from\b",
    r"\breligio\w*", r"\bchurch\b", r"\bmosque\b", r"\btemple\b", r"\bgender\b", r"\btransgender\b", r"\bsexual\w*", r"\bgay\b",
    r"\blesbian\b", r"\bmarri\w*", r"\bmarital\b", r"\bspouse\b", r"\bwife\b", r"\bhusband\b", r"\bkids?\b",
    r"\bchild(?:ren)?\b", r"\bfamily\b", r"\bpregnan\w*", r"\bmaternity\b", r"\bdisabilit\w*", r"\bdisabled\b",
    r"\bmedical condition\b", r"\bmental health\b", r"\bdepress\w*", r"\bsource of income\b", r"\bwelfare\b",
]
PROTECTED_RE = re.compile("|".join(_PROTECTED_TERMS), re.I)

# Pronunciation rules (§14): spoken text must spell these in full.
BANNED_SPOKEN = re.compile(r"P\.Eng|\bPEng\b|\bLOTO\b|\b(?:PLC|MCC|VFD|HMI|EIT)s?\b")
MARKUP = re.compile(r"[<>\[\]{}*_#`]|\bssml\b|\bemotion\b", re.I)  # nothing that could be read as a tag or markdown


def mentions_protected(text: str) -> bool:
    return bool(PROTECTED_RE.search(text or ""))


REDACTED = "[unclear]"


def redact_protected(text: str) -> tuple[str, bool]:
    """Replace only the flagged words (an ASR mishear such as "skid" -> "last kid") with a neutral marker. Returns (text, changed)."""
    out, n = PROTECTED_RE.subn(REDACTED, text or "")
    return out, bool(n)


def spoken_text_problems(text: str) -> list[str]:
    """Why a line must not be sent to TTS as-is (empty list = fine)."""
    out = []
    if BANNED_SPOKEN.search(text):
        out.append("uses an abbreviation that must be spoken in full (P.Eng, PLC, MCC, VFD, HMI, EIT, LOTO)")
    if MARKUP.search(text):
        out.append("contains markup-like characters")
    if mentions_protected(text):
        out.append("mentions a protected ground")
    return out


ACK_MAX_WORDS = 14
# A content-linked ack may say "three-phase" but must not repeat amounts: "forty sets", "two million" ("one" is left alone, it is a pronoun).
_SPOKEN_NUMBER = re.compile(r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|(?:thir|four|fif|six|seven|eigh|nine)teen|"
                            r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|dozen|half)\b(?![- ]phase)", re.I)
DEFAULT_ACK = "Thanks, that's helpful."


def clamp_ack(text: str) -> tuple[str, str]:
    """Reduce the realtime model's reply to one safe short acknowledgement. Returns (text_to_speak, reason_or_'ok').
    The model is only supposed to acknowledge; any question, number, protected-ground word or long reply is replaced."""
    t = " ".join((text or "").split())
    if re.fullmatch(r"\W*none\W*", t, re.I):  # the model chose to say nothing (AckVariety limits how often that is honoured)
        return "", "none"
    m = re.search(r"[.!?](?:\s|$)", t)
    first = t[: m.end()].strip() if m else t
    if not first:
        return DEFAULT_ACK, "empty"
    if "?" in first:
        return DEFAULT_ACK, "question"
    if len(first.split()) > ACK_MAX_WORDS:
        return DEFAULT_ACK, "too_long"
    if re.search(r"\d|\$|%", first) or _SPOKEN_NUMBER.search(first):
        return DEFAULT_ACK, "has_number"
    if mentions_protected(first) or spoken_text_problems(first):
        return DEFAULT_ACK, "guardrail"
    return first, "ok"


THANKS_EVERY = 6  # "thank you" / "thanks" at most once in this many acknowledgements
SILENT_EVERY = 5  # a silent acknowledgement at most once in this many
_THANKS = re.compile(r"\b(?:thanks|thank you|thank|appreciate)\b", re.I)
_THANKS_CLAUSE = re.compile(r"[,;]?\s*\b(?:thanks|thank you|thank|appreciate)\b[^,.;!?]*", re.I)
PLAIN_ACKS = ["Got it.", "Okay.", "Understood.", "That makes sense.", "Okay, good to know.", "I see.", "Right.", "Sounds good.",
              "Okay, that's clear.", "Mm-hm, understood."]  # no thanks, no content: the fallback when the model's ack is over budget


class AckVariety:
    """Keeps acknowledgements from sounding canned: 'thank you' at most once in THANKS_EVERY, no repeat of a recent ack, and the
    model's own choice to say nothing honoured at most once in SILENT_EVERY. Applied to the already-clamped ack text."""

    def __init__(self) -> None:
        self.n = 0
        self.last_thanks: int | None = None
        self.last_silent: int | None = None
        self.recent: list[str] = []
        self._rot = 0

    @staticmethod
    def _norm(t: str) -> str:
        return re.sub(r"[^a-z ]", "", t.lower()).strip()

    def _plain(self) -> str:
        for i in range(len(PLAIN_ACKS)):
            c = PLAIN_ACKS[(self._rot + i) % len(PLAIN_ACKS)]
            if self._norm(c) not in self.recent:
                self._rot = (self._rot + i + 1) % len(PLAIN_ACKS)
                return c
        return PLAIN_ACKS[self._rot % len(PLAIN_ACKS)]

    def review(self, text: str, reason: str) -> tuple[str, str]:
        """(text, reason) from clamp_ack -> the line to speak ('' = say nothing) and a reason for the log."""
        self.n += 1
        if not text:
            if reason == "none" and (self.last_silent is None or self.n - self.last_silent >= SILENT_EVERY):
                self.last_silent = self.n
                return "", "none"
            text, reason = self._plain(), "none_limited"
        elif reason != "ok":  # the clamp replaced the model's line (question, number, banned abbreviation...): a plain ack, not DEFAULT_ACK's thanks
            text = self._plain()
        elif _THANKS.search(text):
            if self.last_thanks is not None and self.n - self.last_thanks < THANKS_EVERY:
                rest = _THANKS_CLAUSE.sub("", text).strip(" ,;")
                rest = (rest[:1].upper() + rest[1:]).rstrip(".!") + "." if rest else ""
                text, reason = rest or self._plain(), "thanks_budget"
        if _THANKS.search(text):
            self.last_thanks = self.n
        if self._norm(text) in self.recent and self._norm(text) not in ("good question", "of course"):  # those two are functional replies
            text, reason = self._plain(), "repeat"
        self.recent = (self.recent + [self._norm(text)])[-5:]
        return text, reason
