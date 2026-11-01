import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transcript_filter import PromptEchoFilter  # noqa: E402

OLD = "Industrial maintenance and electrical engineering interview. Terms: APEGA, P.Eng, CEC, arc flash, MCC, switchgear, PLC, SCADA, LOTO, CMMS, SAP PM, Maximo, RCM, MTBF, Red Seal."
NEW = ("A recruiter is interviewing an electrical or maintenance engineer about plant reliability, APEGA and P.Eng registration, "
       "arc flash, MCC and switchgear, PLC and SCADA systems, CMMS tools such as Maximo and SAP PM, RCM, MTBF, lockout-tagout and Red Seal trades.")
F = PromptEchoFilter([OLD, NEW])


def test_pure_echo_dropped():
    for p in (OLD, NEW, OLD.replace("Terms:", ""), "Industrial maintenance and electrical engineering interview.",
              "Terms: APEGA, P.Eng, CEC, arc flash, MCC, switchgear", "arc flash MCC and switchgear PLC and SCADA systems"):
        r = F.clean(p)
        assert r.dropped and r.text == "", (p, r)


def test_prefixed_echo_stripped_real_speech_kept():
    r = F.clean(OLD + " Yes, I'm ready, let's get started.")
    assert not r.dropped and r.text == "Yes, I'm ready, let's get started.", r
    r = F.clean("I led the arc flash program. " + NEW)
    assert not r.dropped and r.text == "I led the arc flash program.", r


def test_real_domain_speech_untouched():
    for s in ("I ran the PLC and SCADA upgrade and managed work orders in Maximo.",
              "Yes, I'm a registered P.Eng with APEGA, in good standing.",
              "We cut MTBF problems with RCM and our lockout-tagout audits improved.",
              "My Red Seal is in industrial electrician and I did arc flash studies."):
        r = F.clean(s)
        assert not r.dropped and r.text == s, (s, r)


def test_empty_and_noise():
    assert F.clean("").dropped and F.clean("  ...  ").dropped
    assert F.clean("Yes.").text == "Yes." and not F.clean("Yes.").dropped


# --- paraphrase echo (live session 173738: the transcriber reworded its prompt, so the verbatim 5-token rule only half-fired) ---
PARA_FULL = ("An electrical or maintenance engineer is being interviewed about plant reliability, APEGA and P.Eng registration, "
             "arc flash, MCC and switchgear, PLC and SCADA systems, CMMS tools such as Maximo and SAP PM, RCM, MTBF, lockout-tagout "
             "and Red Seal trades.")
PARA_CUT = "An electrical or maintenance engineer is being interviewed about plant reliability, APEGA and P.Eng registration, arc flash, MCC and switchgear, PLC"


def test_paraphrase_echo_dropped():
    for p in (PARA_FULL, PARA_CUT,
              "You are interviewing an electrical or maintenance engineer about plant reliability and arc flash, MCC and switchgear.",
              "The interview is with an electrical or maintenance engineer about plant reliability, APEGA registration, arc flash and switchgear"):
        r = F.clean(p)
        assert r.dropped and r.text == "", (p, r)


def test_paraphrase_prefix_stripped_real_speech_kept():
    r = F.clean(PARA_CUT + " Yes, I'm ready to get started.")
    assert not r.dropped and r.text == "Yes, I'm ready to get started.", r


def test_domain_heavy_first_person_speech_is_not_paraphrase():
    for s in ("I run arc flash studies, MCC and switchgear upgrades, and I handle PLC work on our plant reliability program.",
              "We manage plant reliability with RCM and MTBF tracking, and our CMMS is Maximo with SAP PM for the shutdowns.",
              "My background is maintenance engineering about plant reliability, and I hold a Red Seal and APEGA registration."):
        r = F.clean(s)
        assert not r.dropped and r.text == s, (s, r)


# --- the #5905 transcription context sentence (David 2026-10-24) and a paraphrase of it ---
from persona import PREVIOUS_CONTEXT, TRANSCRIBE_CONTEXT  # noqa: E402
F2 = PromptEchoFilter([TRANSCRIBE_CONTEXT, PREVIOUS_CONTEXT])


def test_new_context_sentence_echo_and_paraphrase_dropped_real_speech_kept():
    para = ("An electrical engineer is being interviewed about APEGA and P.Eng registration, engineers-in-training, PLC and HMI "
            "programming, MCC and VFD design, instrumentation, grounding and bonding, and hazardous locations.")
    for p in (TRANSCRIBE_CONTEXT, para, TRANSCRIBE_CONTEXT[:120]):
        r = F2.clean(p)
        assert r.dropped, (p, r)
    for s in ("I design MCC and VFD systems, and I stamp drawings as a P.Eng with APEGA.",
              "My grounding and bonding work covered hazardous locations under the Canadian Electrical Code and API 500.",
              "I led two EITs and a PLC programmer on the commissioning."):
        r = F2.clean(s)
        assert not r.dropped and r.text == s, (s, r)


# --- live session 021104: the hazardous-locations answer was cut to "All right, so we follow" by the paraphrase rule ---
HAZ = "All right, so we follow the Canadian Electrical Code and the National Electrical Code plus API 500 and 505 in classified areas."


def test_long_real_answer_is_never_cut_when_audio_length_is_known():
    r = F2.clean(HAZ, audio_s=8.0)  # 22 words over ~8 s
    assert not r.dropped and r.text == HAZ and not r.echo_segments, r
    assert F2.clean(HAZ).text != HAZ  # without the audio length the old behaviour is unchanged (documents the bug the guard fixes)


def test_noise_burst_echo_is_still_dropped_with_audio_length():
    assert F2.clean(TRANSCRIBE_CONTEXT, audio_s=0.7).dropped  # ~35 words in 0.7 s
    assert F2.clean(TRANSCRIBE_CONTEXT, audio_s=4.0).dropped  # long audio but ~9 words/s: impossible speech
    r = F2.clean(TRANSCRIBE_CONTEXT + " Yes, I am ready and happy to get started now.", audio_s=4.0)
    assert not r.dropped and r.text == "Yes, I am ready and happy to get started now.", r


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
