import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quote_validation import THRESHOLD, validate_quote  # noqa: E402

# Real transcript turns from live session 173852 (David, ASR output including its mishears) plus two constructed turns.
DOCS = [
    "Yeah",
    "Alright, so my plan has been solved and I want more leadership support",
    "Here I worked as an engineer registered with APEGA in petrochemical. Lately, I've been an electrical supervisor leading 14 electricians and technical IoI voltage distribution maintenance, and many things like that.",
    "Yo, so I clean up our CMMS so work orders were accurate.",
    "I've done a lot of projects actually, dealing with substation upgrade of time, insulation on doublets, things like that.",
    "All right, actually, there has not been any experience like that before, so I don't know what to say on that.",
    "I led the reliability program and cut downtime from nine to six percent over two years.",
    "We did not have a formal lockout-tagout audit before I joined.",
]

ACCEPT = {
    "exact": "I clean up our CMMS so work orders were accurate",
    "case_punct": "Yo so I clean up our CMMS, so work orders were accurate!",
    "short_exact_turn": "Yeah",
    "number_as_digits": "leading 14 electricians",
    "number_as_word": "leading fourteen electricians",
    "evaluator_fixed_mishear": "my plant has been sold and I want more leadership support",
    "evaluator_fixed_mishear_2": "technical high voltage distribution maintenance",
    "dropped_filler": "I've been an electrical supervisor leading 14 electricians",
    "curly_apostrophe": "I’ve done a lot of projects actually",
}
REJECT = {
    "fabricated_outright": "I led a team of forty engineers across three sites and delivered the project early",
    "fabricated_plausible": "I implemented a predictive maintenance program that eliminated unplanned outages",
    "half_real_half_invented": "I clean up our CMMS and then saved the company two million dollars in downtime",
    "number_swapped": "I led the reliability program and cut downtime from nine to four percent over two years",
    "number_swapped_word": "leading forty electricians",
    "negation_dropped": "We did have a formal lockout-tagout audit before I joined",
    "negation_added": "I have not led the reliability program and cut downtime from nine to six percent",
    "bot_words_not_in_candidate": "Thanks, that's helpful. Could you walk me through a time you had to enforce lockout-tagout",
    "short_not_present": "Absolutely yes",
    "empty": "",
}


def test_accepts_and_rejects():
    rows = []
    for k, q in ACCEPT.items():
        m = validate_quote(q, DOCS)
        rows.append((k, True, m))
        assert m.ok, (k, m)
    for k, q in REJECT.items():
        m = validate_quote(q, DOCS)
        rows.append((k, False, m))
        assert not m.ok, (k, m)
    return rows


def test_margin_between_accepts_and_rejects():
    """THRESHOLD sits between the lowest accepted score and the highest score of a quote rejected on similarity alone."""
    lo_ok = min(validate_quote(q, DOCS).score for q in ACCEPT.values())
    hi_bad = max(validate_quote(q, DOCS).score for k, q in REJECT.items() if "number" not in k and "negation" not in k)
    assert hi_bad < THRESHOLD <= lo_ok, (hi_bad, THRESHOLD, lo_ok)
    return lo_ok, hi_bad


def test_returns_transcript_words_not_evaluator_words():
    m = validate_quote("my plant has been sold and I want more leadership support", DOCS)
    assert m.text == "my plan has been solved and I want more leadership support", m.text


def test_short_quotes_must_be_exact():
    assert not validate_quote("No", DOCS).ok
    assert validate_quote("any experience like that", DOCS).ok


if __name__ == "__main__":
    for k, expect, m in test_accepts_and_rejects():
        print(f"{'ACCEPT' if expect else 'REJECT'} {k:28s} score={m.score:.3f} ok={m.ok} {m.reason}")
    print("margin (lowest accepted, highest rejected-by-similarity):", test_margin_between_accepts_and_rejects())
    test_returns_transcript_words_not_evaluator_words()
    test_short_quotes_must_be_exact()
    print("THRESHOLD =", THRESHOLD, "- all ok")
