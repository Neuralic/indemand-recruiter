import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guardrails import DEFAULT_ACK, THANKS_EVERY, AckVariety, clamp_ack, mentions_protected, spoken_text_problems  # noqa: E402
from role_config import load_role  # noqa: E402


def test_ack_clamp_passes_a_short_plain_acknowledgement():
    assert clamp_ack("Got it, thanks.") == ("Got it, thanks.", "ok")
    assert clamp_ack("Thanks, that's helpful context. And what else did you do there") == ("Thanks, that's helpful context.", "ok")


def test_ack_clamp_replaces_questions_numbers_long_and_protected():
    for text, why in [("Great, and how long were you there?", "question"),
                      ("That's a 40 percent gain, nice.", "has_number"),
                      ("I really appreciate you telling me all of that detail about your whole career so far, thank you.", "too_long"),
                      ("Thanks for sharing that about your family.", "guardrail"),
                      ("", "empty")]:
        spoken, reason = clamp_ack(text)
        assert spoken == DEFAULT_ACK and reason == why, (text, spoken, reason)


def test_pronunciation_and_markup_rules():
    assert spoken_text_problems("Do you hold P.Eng registration with APEGA?")
    assert spoken_text_problems("We run LOTO every shift.")
    assert spoken_text_problems("Hello <break time='1s'/> there")
    assert not spoken_text_problems("Do you hold professional engineer registration with APEGA? We run lockout-tagout every shift.")


def test_protected_ground_detector_has_few_false_positives_on_work_talk():
    for ok in ("I partner with contractors on shutdowns", "our older switchgear needed replacing", "we retired two legacy panels",
               "a younger crew of apprentices", "I manage the plant's age-old maintenance backlog"):
        assert not mentions_protected(ok) or "age-old" in ok, ok
    for bad in ("my wife is pregnant", "I'm a single parent with kids", "because of my religion", "I have a disability",
                "what is your marital status"):
        assert mentions_protected(bad), bad


def test_every_line_the_role_config_can_speak_passes_the_guardrails():
    role = load_role("5905")  # load_role raises on any violation
    assert role.competencies and role.knockouts


def _run_acks(raws):
    v, out = AckVariety(), []
    for r in raws:
        out.append(v.review(*clamp_ack(r)))
    return out


def test_content_linked_acks_pass_the_clamp_but_numbers_and_questions_do_not():
    assert clamp_ack("Got it, three-phase power for the compressor building.") == ("Got it, three-phase power for the compressor building.", "ok")
    assert clamp_ack("Got it, forty sets of drawings.")[1] == "has_number"
    assert clamp_ack("Okay, so your kids were at the plant?")[1] in ("question", "guardrail")
    assert clamp_ack("NONE") == ("", "none") and clamp_ack("None.") == ("", "none")


def test_thank_you_is_used_at_most_once_in_six_acknowledgements():
    raws = ["Thanks, that's helpful.", "Okay, thanks for sharing.", "Understood, thank you.", "Thanks, that checks out.",
            "Okay, thanks for confirming.", "Thanks, noted.", "Thank you, that helps.", "Okay, thanks."] * 3
    out = _run_acks(raws)
    idx = [i for i, (t, _) in enumerate(out) if "thank" in t.lower()]
    assert idx and idx[0] == 0 and all(b - a >= THANKS_EVERY for a, b in zip(idx, idx[1:])), idx
    assert all(t for t, _ in out) and all(len(t.split()) <= 14 for t, _ in out)


def test_acks_over_the_thanks_budget_keep_their_content_without_the_thanks():
    out = _run_acks(["Okay, thanks.", "Understood, thanks for confirming.", "Thanks, that's helpful."])
    assert out[1][0] == "Understood." and out[1][1] == "thanks_budget"
    assert out[2][0] == "That's helpful." and "thank" not in out[2][0].lower()


def test_no_ack_repeats_within_five_turns_and_a_content_linked_ack_is_kept():
    out = _run_acks(["Got it.", "Got it.", "Got it.", "Got it, three-phase power for the compressor building.", "Got it."])
    texts = [t for t, _ in out]
    assert len(set(texts[:3])) == 3 and texts[3] == "Got it, three-phase power for the compressor building."
    assert len(set(t.lower() for t in texts)) == len(texts)


def test_silent_acks_are_honoured_but_rate_limited():
    out = _run_acks(["NONE", "NONE", "Okay.", "NONE", "Right.", "NONE", "NONE"])
    assert out[0] == ("", "none")
    assert out[1][0] and out[1][1] == "none_limited"  # not two silences in a row
    silent = [i for i, (t, _) in enumerate(out) if not t]
    assert all(b - a >= 5 for a, b in zip(silent, silent[1:])), silent


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
