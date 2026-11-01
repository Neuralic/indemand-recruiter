"""Deterministic transcript hygiene: strip transcription-prompt echo before a user turn reaches storage, the evaluator or
quote validation. gpt-4o-transcribe sometimes returns its own prompt (alone, or prefixed to real speech) on noise/silence.

Two rules, both against the known prompt sentences:
  1. Verbatim: a run of >= MIN_RUN consecutive transcript tokens that also appears, in order, in a prompt is echo. Real speech that
     merely uses a few of the same domain words never reproduces a 6-token stretch of the prompt.
  2. Paraphrase (the model rewords its prompt: "An electrical or maintenance engineer is being interviewed about plant
     reliability, APEGA and ..."): in-order prompt matches of >= 2 tokens, bridged across gaps of <= PARA_MAX_GAP tokens, that
     span >= PARA_MIN_SPAN tokens with >= PARA_MIN_OVERLAP token overlap, and contain no first-person word, are echo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

MIN_RUN = 6  # tokens (5 flagged real speech: "the Canadian Electrical Code and ..." shares 5 tokens with the #5905 context sentence)
LONG_AUDIO_S = 2.5  # audio at least this long, spoken at <= MAX_WORDS_PER_S, is real speech: never stripped
MAX_WORDS_PER_S = 5.0  # fast human speech is ~3-4 words/s; a prompt echoed over a noise burst is far above this
MIN_REAL_TOKENS = 2 # fewer real words left after stripping => the turn was only echo / noise
PARA_MIN_BLOCK = 2  # shortest in-order prompt match that can seed a paraphrase run
PARA_MAX_GAP = 3  # reworded tokens tolerated between matched blocks
PARA_EDGE = 4  # reworded tokens at the start/end of the turn that are absorbed into a paraphrase run
PARA_MIN_SPAN = 8  # a paraphrase run must be at least this many tokens
PARA_MIN_OVERLAP = 0.6  # ... and at least this fraction of it must be prompt tokens
_FIRST_PERSON = {"i", "my", "me", "we", "our"}  # tokens come from _norm (apostrophes split words); real speech, not a prompt echo

_TOKEN = re.compile(r"[a-z0-9]+")


def _norm(text: str) -> list[tuple[str, int, int]]:
    """(token, start, end) in the ORIGINAL text. Dots inside a word are removed so 'P.Eng' == 'peng'."""
    out = []
    for m in re.finditer(r"[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*", text):
        out.append((m.group(0).replace(".", "").lower(), m.start(), m.end()))
    return out


@dataclass
class FilterResult:
    text: str  # cleaned text ("" if dropped)
    dropped: bool  # True => the whole turn was echo/noise and must not count as a candidate turn
    echo_segments: list[str] = field(default_factory=list)


class PromptEchoFilter:
    def __init__(self, prompts: list[str], min_run: int = MIN_RUN):
        self.min_run = min_run
        self._prompts = [[t for t, _, _ in _norm(p)] for p in prompts if p]

    def clean(self, text: str, audio_s: float | None = None) -> FilterResult:
        """`audio_s` = how long the audio behind this transcript really was. The transcriber only returns its own prompt on short
        noise bursts, where the transcript is physically too long for the audio. A turn that is long enough AND spoken at a plausible
        pace is real speech and is never stripped (a candidate listing "the Canadian Electrical Code and the National Electrical Code,
        API 500 and 505" repeats the prompt's words legitimately; live session 021104 lost that answer to the paraphrase rule)."""
        toks = _norm(text)
        words = [t for t, _, _ in toks]
        if audio_s is not None and audio_s >= LONG_AUDIO_S and len(words) / audio_s <= MAX_WORDS_PER_S:
            stripped = text.strip()
            return FilterResult(stripped, dropped=not toks)
        echo = [False] * len(toks)
        for p in self._prompts:
            sm = SequenceMatcher(None, words, p, autojunk=False)
            blocks = [b for b in sm.get_matching_blocks() if b.size]
            for blk in blocks:
                if blk.size >= self.min_run:
                    for i in range(blk.a, blk.a + blk.size):
                        echo[i] = True
            self._mark_paraphrase(words, blocks, echo)
        if not any(echo):
            stripped = text.strip()
            return FilterResult(stripped, dropped=not _norm(stripped))
        segs, parts, i = [], [], 0
        while i < len(toks):
            j = i
            while j < len(toks) and echo[j] == echo[i]:
                j += 1
            seg = text[toks[i][1]:(toks[j][1] if j < len(toks) else len(text))].strip()  # keeps trailing punctuation
            (segs if echo[i] else parts).append(seg)
            i = j
        real = " ".join(parts).strip()
        if len(_norm(real)) < MIN_REAL_TOKENS:
            return FilterResult("", True, segs)
        return FilterResult(real, False, segs)

    @staticmethod
    def _mark_paraphrase(words: list[str], blocks, echo: list[bool]) -> None:
        seeds = [b for b in blocks if b.size >= PARA_MIN_BLOCK]
        i = 0
        while i < len(seeds):
            j, covered = i, seeds[i].size
            while j + 1 < len(seeds) and seeds[j + 1].a - (seeds[j].a + seeds[j].size) <= PARA_MAX_GAP:
                j += 1
                covered += seeds[j].size
            start, end = seeds[i].a, seeds[j].a + seeds[j].size
            span = end - start
            if span >= PARA_MIN_SPAN and covered / span >= PARA_MIN_OVERLAP:
                lo = 0 if start <= PARA_EDGE else start  # reworded lead-in/tail at the edges of the turn ("You are ...")
                hi = len(words) if len(words) - end <= PARA_EDGE else end
                if not any(w in _FIRST_PERSON for w in words[lo:hi]):  # "My grounding work ..." is the candidate talking, not an echo
                    for k in range(lo, hi):
                        echo[k] = True
            i = j + 1
