import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluator import Extraction, Verdict  # noqa: E402
from guardrails import spoken_text_problems  # noqa: E402
from interview import Interview, StageGateError  # noqa: E402
from role_config import load_role  # noqa: E402

ROLE = load_role("5905")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(clock=None) -> tuple[Interview, Clock]:
    c = clock or Clock()
    iv = Interview(ROLE, "David", clock=c)
    iv.opening()
    return iv, c


def ans(spec="specific", answers=True, **kw) -> Verdict:
    return Verdict(turn_kind="answer", answers_question=answers, specificity=spec, **kw)


def turn(iv, v, text="some answer text from the candidate"):
    iv.on_user_turn(text)
    return iv.decide(v)


def run_to_competencies(iv):
    turn(iv, Verdict(turn_kind="answer", consent="yes"), "Yeah")
    turn(iv, ans(), "I want more leadership support and a bigger team")
    return turn(iv, ans(), "I have been an electrical supervisor for ten years")  # background -> first competency question


def run_to_logistics(iv):
    run_to_competencies(iv)
    for _ in ROLE.competencies:
        a = turn(iv, ans())
    return a


def ko_verdict(iv, status="pass"):
    return ans(knockout={"knockout_id": iv.step.id, "status": status})


def run_logistics(iv, status="pass"):
    a = None
    while iv.step.kind == "knockout":
        a = turn(iv, ko_verdict(iv, status), "yes")
    return a


def test_happy_path_order_and_gate():
    iv, _ = make()
    a = run_to_competencies(iv)
    assert iv.step.id == ROLE.competencies[0].id and a.speak == ROLE.competencies[0].primary_question
    seen = [s["stage"] for s in iv.stage_log]
    run_to_logistics(iv)
    assert iv.stage == "logistics"
    a = run_logistics(iv)
    assert iv.stage == "candidate_questions" and ROLE.script["candidate_questions_prompt"] in a.speak
    a = turn(iv, ans(), "No, that's all, thanks")
    assert a.end and iv.ended == "completed" and iv.stage == "close"
    order = [s["stage"] for s in iv.stage_log]
    dedup = [x for i, x in enumerate(order) if i == 0 or x != order[i - 1]]
    assert dedup == ["welcome", "rapport", "background", "competencies", "logistics", "candidate_questions", "close"], dedup
    assert not iv.probe_used and not seen[1:] == []


def test_every_step_moves_exactly_one():
    iv, _ = make()
    idxs = [iv.idx]
    turn(iv, Verdict(turn_kind="answer", consent="yes")); idxs.append(iv.idx)
    turn(iv, ans()); idxs.append(iv.idx)
    turn(iv, ans()); idxs.append(iv.idx)
    assert idxs == [0, 1, 2, 3], idxs


def test_close_is_unreachable_before_required_knockouts():
    iv, _ = make()
    run_to_logistics(iv)
    for _ in range(len(iv.steps)):  # try to push through logistics without recording a thing: re-asks only, never close
        pass
    while iv.step.kind != "candidate_questions":
        iv.idx += 1  # simulate a buggy caller jumping ahead without recording knock-outs
    try:
        iv._advance()
        raise AssertionError("gate did not fire")
    except StageGateError:
        pass
    a = iv.force_close("stop_request")  # a named exit is allowed, and flags the incomplete logistics
    assert a.end and any(f["topic"] == "logistics incomplete" for f in iv.flags)
    try:
        iv.force_close("because I said so")
        raise AssertionError("unknown exit accepted")
    except StageGateError:
        pass


def test_probe_exactly_once_then_weak_and_move_on():
    iv, _ = make()
    run_to_competencies(iv)
    first = iv.comp(iv.step.id)
    a = turn(iv, ans("vague", answers=False), "Yo, so I clean up our CMMS so work orders were accurate.")
    assert a.kind == "probe" and a.speak == first.probe_if_vague and iv.post_probe
    nxt_id = ROLE.competencies[1].id
    a = turn(iv, ans("vague", answers=False), "I've done a lot of projects actually, things like that.")
    assert a.kind == "question" and iv.step.id == nxt_id, (a, iv.step.id)  # NO second probe
    assert a.extract and a.extract.final_specific is False
    assert iv.probe_used == {first.id}
    # the evaluator's own label is overridden to "weak" in code
    iv.apply_extraction(a.extract, Extraction(evidence=[{"competency_id": first.id, "quote": "I've done a lot of projects actually",
                                                         "assessment": "strong", "note": "generic"}]))
    ev = [e for e in iv.evidence if e["competency_id"] == first.id]
    assert ev and ev[-1]["assessment"] == "weak" and ev[-1]["assessment_forced_weak"], ev


def test_specific_answer_is_not_probed():
    iv, _ = make()
    run_to_competencies(iv)
    a = turn(iv, ans("specific"))
    assert a.kind == "question" and not iv.probe_used and a.extract and a.extract.final_specific


def test_probe_never_uses_accusatory_phrasing_and_all_lines_pass_guardrails():
    iv, _ = make()
    lines = [iv.history[0][1]]
    run_to_competencies(iv)
    for _ in ROLE.competencies:
        lines.append(turn(iv, ans("vague", answers=False)).speak)
        lines.append(turn(iv, ans("vague", answers=False)).speak)
    lines += [turn(iv, ans(knockout={"knockout_id": iv.step.id, "status": "pass"})).speak for _ in ROLE.knockouts]
    lines.append(turn(iv, Verdict(turn_kind="candidate_question", candidate_question={"text": "pay?", "fact_key": "compensation"})).speak)
    lines.append(turn(iv, ans()).speak)
    for ln in lines:
        low = ln.lower()
        assert "still not hearing" not in low and "not hearing" not in low, ln
        assert not spoken_text_problems(ln), (ln, spoken_text_problems(ln))
    assert iv.ended == "completed"


def test_knockout_unclear_reasked_once_then_recorded_unclear():
    iv, _ = make()
    run_to_logistics(iv)
    first = iv.step.id
    a = turn(iv, ans(knockout={"knockout_id": first, "status": "unclear"}), "um, maybe")
    assert a.kind == "reask" and iv.step.id == first
    a = turn(iv, ans(knockout={"knockout_id": first, "status": "unclear"}), "dunno")
    assert iv.knockouts[first]["status"] == "unclear" and iv.step.id != first and a.extract


def test_required_knockouts_gate_candidate_questions():
    iv, _ = make()
    run_to_logistics(iv)
    run_logistics(iv, "pass")
    assert iv.required_knockouts_done() and iv.stage == "candidate_questions"


def test_salary_question_is_deferred_not_invented():
    iv, _ = make()
    run_to_logistics(iv)
    run_logistics(iv)
    a = turn(iv, Verdict(turn_kind="candidate_question", candidate_question={"text": "What is the salary range and bonus?",
                                                                             "fact_key": "compensation"}))
    assert "Stefanie will confirm that" in a.speak and not any(ch.isdigit() for ch in a.speak) and "$" not in a.speak, a.speak
    assert any("salary" in f["reason"].lower() for f in iv.flags)
    assert iv.stage == "candidate_questions" and not a.end
    # a fact that IS in the config is answered from the config
    a = turn(iv, Verdict(turn_kind="candidate_question", candidate_question={"text": "Where is the job?", "fact_key": "location"}))
    assert "Edmonton" in a.speak
    # a PLACEHOLDER fact is treated as unknown
    ROLE.facts["benefits"] = "PLACEHOLDER dental"
    try:
        a = turn(iv, Verdict(turn_kind="candidate_question", candidate_question={"text": "benefits?", "fact_key": "benefits"}))
        assert a.end and "Stefanie will confirm that" in a.speak  # third question closes
    finally:
        ROLE.facts["benefits"] = ""


def test_candidate_question_mid_interview_returns_to_pending_question():
    iv, _ = make()
    run_to_competencies(iv)
    q = iv.current_question
    a = turn(iv, Verdict(turn_kind="candidate_question", candidate_question={"text": "pay?", "fact_key": "compensation"}))
    assert a.speak.endswith(iv.short_of(q)) and len(iv.short_of(q)) < len(q) and iv.step.kind == "competency"  # a SHORT repeat, never the full read


def test_offtopic_three_in_a_row_closes_and_resets():
    iv, _ = make()
    run_to_competencies(iv)
    q = iv.current_question
    a = turn(iv, Verdict(turn_kind="off_topic"))
    assert a.kind == "redirect" and a.speak.endswith(iv.short_of(q))
    turn(iv, Verdict(turn_kind="off_topic"))
    turn(iv, ans())  # on-topic answer resets the counter
    assert iv.offtopic_run == 0
    iv2, _ = make()
    run_to_competencies(iv2)
    for _ in range(2):
        turn(iv2, Verdict(turn_kind="off_topic"))
    a = turn(iv2, Verdict(turn_kind="off_topic"))
    assert a.end and iv2.ended == "off_topic"


def test_wants_human_and_decline_close_politely_with_flag():
    iv, _ = make()
    a = turn(iv, Verdict(turn_kind="answer", consent="no"), "No thanks")
    assert a.end and iv.ended == "declined" and any("declined" in f["topic"] for f in iv.flags)
    iv, _ = make()
    run_to_competencies(iv)
    a = turn(iv, Verdict(turn_kind="wants_human"))
    assert a.end and iv.ended == "wants_human" and "person" in a.speak


def test_time_budget_soft_wrap_skips_competencies_hard_closes():
    iv, clock = make()
    run_to_competencies(iv)
    clock.t = 13 * 60 + 1
    a = turn(iv, ans())
    assert iv.step.kind == "knockout" and "logistics" in a.speak.lower(), (iv.step, a.speak)
    assert any(f["topic"] == "time budget" for f in iv.flags)
    clock.t = 16 * 60 + 1
    a = turn(iv, ko_verdict(iv))
    assert a.end and iv.ended == "time_limit"


def test_evaluator_failure_takes_neutral_path_never_double_probes():
    iv, _ = make()
    run_to_competencies(iv)
    a = turn(iv, Verdict(failed="TimeoutError"))
    assert a.kind == "question" and not iv.probe_used and any(f["topic"] == "evaluator unavailable" for f in iv.flags)


def test_silence_ladder():
    iv, _ = make()
    run_to_competencies(iv)
    assert iv.silence_action(1).speak.startswith("Take your time")
    assert "still there" in iv.silence_action(2).speak
    a = iv.silence_action(3)
    assert a.end and iv.ended == "silence"


def test_extraction_rejects_fabricated_and_protected_quotes_keeps_tidied_mishears():
    iv, _ = make()
    run_to_competencies(iv)
    s = iv.step
    iv.on_user_turn("Alright, so my plan has been solved and I want more leadership support. I led fourteen electricians on shutdowns.")
    job = iv.decide(ans()).extract
    x = Extraction(evidence=[
        {"competency_id": s.id, "quote": "I led fourteen electricians on shutdowns", "assessment": "strong", "note": "team size"},
        {"competency_id": s.id, "quote": "my plant has been sold and I want more leadership support", "assessment": "adequate", "note": "motive"},
        {"competency_id": s.id, "quote": "I cut downtime by forty percent across three sites", "assessment": "strong", "note": "invented"},
        {"competency_id": s.id, "quote": "my wife is pregnant and I want more leadership support", "assessment": "weak", "note": "x"},
    ], protected_info_volunteered=True)
    iv.apply_extraction(job, x)
    quotes = [e["quote"] for e in iv.evidence]
    assert "my plan has been solved and I want more leadership support" in quotes  # transcript's words, not the evaluator's
    assert any("fourteen electricians" in q for q in quotes)
    assert not any("forty percent" in q for q in quotes)
    assert len(iv.rejected_quotes) == 2 and {r["reason"] for r in iv.rejected_quotes} >= {"protected ground"}


def test_asr_mishear_that_trips_the_protected_filter_redacts_only_the_span_and_still_rates():
    iv, _ = make()
    run_to_competencies(iv)
    s = iv.step
    iv.on_user_turn("I've programmed PLC and built the HMI screens on last kid. I commissioned the instrumentation loops and cut start-up faults by half.")
    job = iv.decide(ans()).extract
    quote = "I've programmed PLC and built the HMI screens on last kid. I commissioned the instrumentation loops"
    iv.apply_extraction(job, Extraction(evidence=[{"competency_id": s.id, "quote": quote, "assessment": "strong", "note": "hands-on, last kid"}]))
    assert len(iv.evidence) == 1 and not iv.rejected_quotes
    e = iv.evidence[0]
    assert e["assessment"] == "strong" and "[unclear]" in e["quote"] and "kid" not in e["quote"].lower()
    assert e["quote"].startswith("I've programmed PLC and built the HMI screens on last [unclear]") and "kid" not in e["note"].lower()
    assert any(f["topic"] == "transcript unclear" and "verify this answer" in f["reason"] for f in iv.flags)
    assert "kid" not in str(iv.summary()["evidence"]).lower() and "kid" not in str(iv.flags).lower()
    # a quote the evaluator marked as genuinely volunteered protected info is still withheld entirely
    iv2, _ = make()
    run_to_competencies(iv2)
    iv2.on_user_turn("I can only travel little because my kids are small. I led fourteen electricians on shutdowns.")
    job2 = iv2.decide(ans()).extract
    iv2.apply_extraction(job2, Extraction(protected_info_volunteered=True, evidence=[
        {"competency_id": iv2.step.id, "quote": "I can only travel little because my kids are small", "assessment": "weak", "note": "x"}]))
    assert not any("kids" in e["quote"] for e in iv2.evidence) and iv2.rejected_quotes[0]["reason"] == "protected ground"


def test_competency_always_leaves_a_record_even_if_extraction_fails():
    iv, _ = make()
    run_to_competencies(iv)
    s = iv.step
    iv.on_user_turn("I do the usual things at the plant")
    job = iv.decide(ans()).extract
    iv.apply_extraction(job, Extraction(failed="TimeoutError"))
    rec = [e for e in iv.evidence if e["competency_id"] == s.id]
    assert rec and rec[0]["quote"] == "I do the usual things at the plant"
    assert any(f["topic"] == "evidence extraction failed" for f in iv.flags)


def test_knockout_quote_verified_or_flagged():
    iv, _ = make()
    run_to_logistics(iv)
    s = iv.step
    iv.on_user_turn("Yes, I'm legally allowed to work in Canada")
    job = iv.decide(ko_verdict(iv, "pass")).extract
    iv.apply_extraction(job, Extraction(knockout_answer="eligible", knockout_quote="Yes I'm legally allowed to work in Canada"))
    assert iv.knockouts[s.id]["quote_verified"] and iv.knockouts[s.id]["status"] == "pass"
    s2 = iv.step
    iv.on_user_turn("Yes, I hold the registration")
    job = iv.decide(ko_verdict(iv, "pass")).extract
    iv.apply_extraction(job, Extraction(knockout_answer="x", knockout_quote="I have been registered for twenty years"))
    assert not iv.knockouts[s2.id]["quote_verified"] and any(f["topic"] == "verify knock-out" for f in iv.flags)


def test_client_question_is_answered_only_from_the_posting_and_flagged():
    iv, _ = make()
    run_to_logistics(iv)
    run_logistics(iv)
    a = turn(iv, Verdict(turn_kind="candidate_question", candidate_question={"text": "Who is the client?", "fact_key": "client"}))
    assert "established engineering organization" in a.speak and "Stefanie will confirm anything beyond that" in a.speak, a.speak
    assert any(f["topic"] == "candidate asked about the client" for f in iv.flags)


def test_posting_facts_reporting_schedule_and_unstated_pay():
    iv, _ = make()
    run_to_logistics(iv)
    run_logistics(iv)
    a = turn(iv, Verdict(turn_kind="candidate_question", candidate_question={"text": "Who do I report to?", "fact_key": "reporting_to"}))
    assert "Operations Manager" in a.speak
    a = turn(iv, Verdict(turn_kind="candidate_question", candidate_question={"text": "How much travel?", "fact_key": "schedule"}))
    assert "twenty percent" in a.speak and "two consecutive weeks" in a.speak
    a = turn(iv, Verdict(turn_kind="candidate_question", candidate_question={"text": "benefits?", "fact_key": "benefits"}))
    assert a.end and "Stefanie will confirm that" in a.speak  # third question closes; benefits are not in the posting


def test_role_matches_the_posting_brief():
    assert [c.id for c in ROLE.competencies] == ["technical_electrical", "controls_automation", "hazardous_locations_codes",
                                                 "pe_stamping", "team_leadership", "project_delivery"]
    assert [round(c.weight, 2) for c in ROLE.competencies] == [0.25, 0.15, 0.15, 0.15, 0.20, 0.10]
    assert [k.id for k in ROLE.knockouts][:4] == ["work_auth", "pe_apega", "degree", "travel"]
    assert [k.id for k in ROLE.knockouts][4:] == ["location", "salary", "notice_period"]
    assert "eligible" in next(k for k in ROLE.knockouts if k.id == "pe_apega").fail_if
    assert ROLE.known_fact("compensation") is None and ROLE.known_fact("benefits") is None
    spoken = " ".join([c.primary_question + c.probe_if_vague for c in ROLE.competencies] + [k.question for k in ROLE.knockouts])
    for gone in ("Maximo", "CMMS", "MTBF", "RCM", "LOTO", "Red Seal"):
        assert gone not in spoken and gone not in " ".join(ROLE.domain_terms), gone
    for full in ("professional engineer", "programmable logic controllers", "motor control centres", "variable frequency drives"):
        assert full in spoken, full
    from interview import CLOSE
    assert "within one business day" not in CLOSE and "Stefanie's team reviews every interview personally" in CLOSE


def cq(text, key="other"):
    return Verdict(turn_kind="candidate_question", candidate_question={"text": text, "fact_key": key})


def test_can_i_ask_a_question_gets_one_short_line_then_a_short_repeat_and_never_loops():
    iv, _ = make()
    run_to_competencies(iv)
    q = iv.current_question
    a = turn(iv, cq("Can I ask a question?"), "Can I ask a question?")
    assert a.speak == "Of course, we'll make time for your questions at the end. " + iv.short_of(q), a.speak
    assert len(a.speak.split()) < 30 and not iv.flags  # nothing to cover later
    a = turn(iv, cq("Can I ask a question?"), "Can I ask a question?")
    assert a.speak == "We'll get to your questions at the end." and iv.step.kind == "competency"
    a = turn(iv, cq("Can I ask a question?"), "Can I ask a question?")
    assert a.speak == "" and a.kind == "defer"  # third time: silence, no loop


def test_permission_request_at_the_candidate_questions_step_just_says_go_ahead():
    iv, _ = make()
    run_to_logistics(iv)
    run_logistics(iv)
    assert iv.step.kind == "candidate_questions" and iv.cq_count == 0
    a = turn(iv, cq("Can I ask a question?"), "Can I ask a question?")
    assert a.speak == "Of course, go ahead." and iv.cq_count == 0 and not a.end


def test_glossary_what_is_x_is_one_fixed_sentence_then_short_repeat():
    iv, _ = make()
    run_to_competencies(iv)
    q = iv.current_question
    a = turn(iv, cq("What is P.Eng?"), "What is P.Eng?")
    assert a.speak.startswith("That's the professional engineer designation, and the posting requires it to be in good standing with APEGA.")
    assert a.speak.endswith(iv.short_of(q)) and not iv.flags and not spoken_text_problems(a.speak)
    a = turn(iv, cq("What is APEGA?"), "What is APEGA?")
    assert a.speak.startswith("APEGA is the regulator that registers professional engineers in Alberta.") and not iv.flags
    # not in the glossary: deferred to Stefanie and flagged, never invented
    a = turn(iv, cq("What is the dress code?"), "What is the dress code?")
    assert a.speak.startswith("Stefanie will confirm that with you.") and iv.flags


def test_glossary_at_the_candidate_questions_step_counts_as_a_question():
    iv, _ = make()
    run_to_logistics(iv)
    run_logistics(iv)
    a = turn(iv, cq("What is an EIT?"), "What is an EIT?")
    assert "engineer-in-training" in a.speak and a.speak.endswith(ROLE.script["more_questions_prompt"]) and iv.cq_count == 1


def test_probe_answer_is_judged_together_with_the_answer_that_triggered_it():
    iv, _ = make()
    run_to_competencies(iv)
    a = turn(iv, ans("vague"), "I've worked on a lot of electrical systems.")
    assert a.kind == "probe"
    iv.on_user_turn("At a gas plant I designed the three-phase power for a new compressor building.")
    iv.on_user_turn("It had an MCC and VFDs and I specified the grounding and bonding.")  # the second half of a split answer
    ctx = iv.eval_context()
    assert ctx.post_probe and ctx.answer_turns == ["I've worked on a lot of electrical systems.",
                                                    "At a gas plant I designed the three-phase power for a new compressor building.",
                                                    "It had an MCC and VFDs and I specified the grounding and bonding."]


def test_every_short_question_is_a_real_shortening_and_passes_the_spoken_rules():
    for c in ROLE.competencies:
        assert c.short_question and len(c.short_question.split()) < len(c.primary_question.split()) + 6 and len(c.short_question.split()) <= 22
        assert not spoken_text_problems(c.short_question)
    t = next(k for k in ROLE.knockouts if k.id == "travel")
    assert len(t.short_question.split()) <= 16
    iv, _ = make()
    assert iv.short_of(t.question) == t.short_question and iv.short_of("Anything else you'd like to ask?") == "Anything else you'd like to ask?"


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
