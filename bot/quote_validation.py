"""Evidence quote validation (CLAUDE.md §10, §14): every quote the evaluator logs must really be in the candidate's words.

The transcript is automatic speech recognition, so it contains mishears ("plan has been solved" for "plant has been sold"), and the
evaluator may tidy a quote slightly. So matching is fuzzy, but strict where it matters:

  1. Candidate text only (the caller passes candidate turns, never bot lines).
  2. Quotes of >= MIN_FUZZY_TOKENS words: best sliding window (length within 2 tokens of the quote) of the normalized text must
     reach THRESHOLD character similarity. Shorter quotes must match exactly (as a contiguous token run).
  3. Critical tokens must agree exactly with the matched window: numbers (digits or number words, so "40" == "forty") and
     negations. This is what separates a tidied mishear from a fabrication, because a single swapped number or a dropped "not"
     keeps character similarity very high ("from nine to four percent" vs "from nine to six percent" scores ~0.95).
  4. On success the stored quote is the transcript's own text for the matched window, not the evaluator's wording.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher

THRESHOLD = 0.90  # see bot/tests/test_quote_validation.py: tidied mishears score >= 0.90, fabrications score well below
MIN_FUZZY_TOKENS = 4

_TOKEN = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?")
_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen "
    "nineteen twenty".split())}
_NUMBER_WORDS.update({"thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80",
                      "ninety": "90", "hundred": "100", "thousand": "1000", "million": "1000000", "half": "0.5"})
_NEGATIONS = {"no", "not", "never", "cannot", "without", "none", "neither", "nor"}


@dataclass
class QuoteMatch:
    ok: bool
    score: float
    doc_index: int = -1
    text: str = ""  # the transcript's own words for the matched window ("" when rejected)
    reason: str = ""
    window: list[str] = field(default_factory=list, repr=False)


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0).lower().replace("’", "'"), m.start(), m.end()) for m in _TOKEN.finditer(text)]


def _critical(tokens: list[str]) -> Counter:
    c: Counter = Counter()
    for t in tokens:
        if t in _NEGATIONS or t.endswith("n't"):
            c["NEG"] += 1
        elif t.isdigit():
            c[t] += 1
        elif t in _NUMBER_WORDS:
            c[_NUMBER_WORDS[t]] += 1
    return c


def validate_quote(quote: str, documents: list[str], threshold: float = THRESHOLD) -> QuoteMatch:
    q = [t for t, _, _ in _tokens(unicodedata.normalize("NFKC", quote))]
    if not q:
        return QuoteMatch(False, 0.0, reason="empty quote")
    qs = " ".join(q)
    best = QuoteMatch(False, 0.0, reason="not in transcript")
    for di, raw in enumerate(documents):
        doc = unicodedata.normalize("NFKC", raw)
        toks = _tokens(doc)
        words = [t for t, _, _ in toks]
        if len(q) < MIN_FUZZY_TOKENS:  # short quotes: exact contiguous run only (number words == digits)
            cq, cw = [_NUMBER_WORDS.get(t, t) for t in q], [_NUMBER_WORDS.get(t, t) for t in words]
            for s in range(0, len(cw) - len(cq) + 1):
                if cw[s:s + len(cq)] == cq:
                    return QuoteMatch(True, 1.0, di, doc[toks[s][1]:toks[s + len(q) - 1][2]], window=q)
            continue
        for L in range(max(1, len(q) - 2), len(q) + 3):
            for s in range(0, len(words) - L + 1):
                w = words[s:s + L]
                sm = SequenceMatcher(None, qs, " ".join(w), autojunk=False)
                if sm.real_quick_ratio() <= best.score or sm.quick_ratio() <= best.score:
                    continue
                r = sm.ratio()
                if r > best.score:
                    best = QuoteMatch(False, r, di, doc[toks[s][1]:toks[s + L - 1][2]], "below threshold", w)
    if best.score >= threshold:
        if _critical(q) != _critical(best.window):
            return QuoteMatch(False, best.score, best.doc_index, "", "numbers/negations differ from transcript")
        best.ok, best.reason = True, ""
        return best
    return QuoteMatch(False, best.score, best.doc_index, "", best.reason)
