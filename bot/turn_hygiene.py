"""Deterministic turn hygiene (David 2026-10-25, after the first real-voice session). Pure functions, no I/O.

  * is_incomplete(): a transcript that is probably the first piece of a longer answer ("All right, yeah so", "First of all,").
  * is_permission_request(): "Can I ask a question?" - asks for permission, carries no question to answer.
  * glossary_answer(): one fixed sentence for "what is X?" about a term the posting itself uses. Nothing is invented: each sentence
    only expands the abbreviation and points at where the posting uses it. Anything else is deferred to Stefanie by the caller.
"""

from __future__ import annotations

import re

# ---- incomplete turns --------------------------------------------------------------------------------------------------------
_TERMINAL = re.compile(r"[.!?][\"')\]]*\s*$")
_TRAILING_CONNECTIVE = re.compile(
    r"(?:^|[\s,;:-])(?:so|and|but|or|like|well|um+|uh+|er|hmm+|because|cause|then|also|plus|that|which|the|a|an|to|of|in|on|at|for|with|"
    r"as|if|when|where|while|yeah so|all right so|first of all|first|second|third|you know|i mean|i think|i guess|kind of|sort of|"
    r"basically|actually|honestly|let me think|let's see|how do i say|what else|and then|so then)\s*[,;:.…-]*\s*$", re.I)
_TOKEN = re.compile(r"[A-Za-z0-9']+")
SHORT_WORDS = 2  # a turn this short without a finished sentence is a fragment; "Yes." / "No." end in a full stop and are complete


def is_incomplete(text: str) -> bool:
    """True when the newest transcript looks like the start of a longer answer, so the director should wait a little longer and
    merge whatever follows before it judges anything."""
    t = (text or "").strip()
    if not t:
        return False
    words = _TOKEN.findall(t)
    if _TRAILING_CONNECTIVE.search(t):
        return True
    if not _TERMINAL.search(t):  # no finished sentence at the end (gpt-4o-transcribe punctuates finished sentences)
        return True
    return len(words) <= SHORT_WORDS and t.endswith(",")


# ---- "can I ask a question?" -------------------------------------------------------------------------------------------------
_PERMISSION = re.compile(
    r"^\W*(?:(?:sorry|excuse me|actually|um+|uh+|oh|okay|ok|so|well|yeah|hey)[\s,]+)*"
    r"(?:(?:can|could|may|might|do you mind if|is it (?:ok|okay|alright|all right) if|would it be (?:ok|okay|alright) if)\s+i\s+"
    r"(?:just\s+|quickly\s+)?(?:ask|have)|i (?:just )?(?:have|got|had)|i('d| would) like to ask|one)\s*"
    r"(?:you\s+)?(?:a|another|one|an|some|my|the)?\s*(?:quick\s+|little\s+|small\s+|brief\s+|short\s+)?(?:question|questions|something)?"
    r"(?:\s+(?:first|before (?:i|we) (?:continue|go on|start|answer)|if that's (?:ok|okay|alright)))?\s*\W*$", re.I)


def is_permission_request(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and len(_TOKEN.findall(t)) <= 12 and bool(_PERMISSION.match(t))


# ---- glossary ----------------------------------------------------------------------------------------------------------------
# Terms the posting itself uses. Spoken text, so it follows the pronunciation rules (no P.Eng / PLC / MCC / VFD / HMI / EIT).
GLOSSARY: list[tuple[str, re.Pattern, str]] = [
    ("peng", re.compile(r"\bp\.?\s?eng\b|\bpe\b|\bprofessional engineer\b", re.I),
     "That's the professional engineer designation, and the posting requires it to be in good standing with APEGA."),
    ("apega", re.compile(r"\bapega\b", re.I), "APEGA is the regulator that registers professional engineers in Alberta."),
    ("eit", re.compile(r"\be\.?\s?i\.?\s?ts?\b|\bengineers?[- ]in[- ]training\b", re.I),
     "That means engineer-in-training, and this role leads a team that includes them."),
    ("mcc", re.compile(r"\bm\.?\s?c\.?\s?cs?\b|\bmotor control cent(?:er|re)s?\b", re.I),
     "That's a motor control centre, one of the technical areas the posting lists."),
    ("vfd", re.compile(r"\bv\.?\s?f\.?\s?ds?\b|\bvariable (?:frequency|speed) drives?\b", re.I),
     "That's a variable frequency drive, which the posting lists alongside motor control centres."),
    ("plc", re.compile(r"\bp\.?\s?l\.?\s?cs?\b|\bprogrammable logic controllers?\b", re.I),
     "That's a programmable logic controller, which the posting lists together with human-machine interfaces."),
    ("hmi", re.compile(r"\bh\.?\s?m\.?\s?is?\b|\bhuman[- ]machine interfaces?\b", re.I),
     "That's a human-machine interface, which the posting lists together with programmable logic controllers."),
    ("api", re.compile(r"\bapi\b", re.I),
     "The posting names API 500 and 505 among the codes and standards for hazardous location work, along with the Canadian "
     "Electrical Code and the National Electrical Code."),
]
_WHAT_IS = re.compile(r"\b(?:what(?:'s| is| are| does)|what do you mean by|can you (?:explain|define)|could you (?:explain|define)|"
                      r"explain|define|meaning of|stands? for|mean by)\b", re.I)


def glossary_answer(*texts: str) -> str | None:
    """A fixed one-sentence answer when the candidate asks what a posting term means, else None (caller defers to Stefanie)."""
    for text in texts:
        t = (text or "").strip()
        if not t or not _WHAT_IS.search(t) or len(_TOKEN.findall(t)) > 14:
            continue
        for key, pat, sentence in GLOSSARY:
            m = pat.search(t)
            if not m:
                continue
            if key == "peng" and m.group(0).lower() == "pe":  # bare "PE" is ambiguous; only the explicit spellings count
                continue
            return sentence
    return None
