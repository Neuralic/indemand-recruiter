import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guardrails import spoken_text_problems  # noqa: E402
from turn_hygiene import GLOSSARY, glossary_answer, is_incomplete, is_permission_request  # noqa: E402


def test_fragments_from_the_live_session_are_incomplete():
    for t in ("All right, yeah so", "First of all,", "All right, so", "Yeah, so", "APEGA", "So, as a programmer, I work with designers,",
              "I paired a struggling EIT with a senior engineer and", "Well, like", "I worked on the", "Um,"):
        assert is_incomplete(t), t


def test_finished_answers_are_complete():
    for t in ("Yes.", "No.", "Four weeks.", "Yes, I'm legally eligible to work in Canada.", "Before I answer, what is the salary for this role?",
              "Can I ask a question?", "Like I said, we follow the code and we take it seriously.",
              "At a gas plant, I designed a three-phase power for a new compressor building.",
              "I took a $3 million MCC replacement from estimate through commissioning and delivered two weeks early."):
        assert not is_incomplete(t), t


def test_permission_requests():
    for t in ("Can I ask a question?", "Can I ask you a question?", "Could I ask a quick question?", "Sorry, can I ask something?",
              "May I ask a question", "I have a question.", "Can I just ask a quick question first?"):
        assert is_permission_request(t), t
    for t in ("Can I ask what the salary is?", "What is P.Eng?", "Can I ask who the client is?", "Yes, let's go.",
              "I have a question about the salary range and the benefits for this role."):
        assert not is_permission_request(t), t


def test_glossary_answers_are_one_sentence_and_pass_the_spoken_text_rules():
    for q, needle in (("What is P.Eng?", "professional engineer"), ("What's an EIT?", "engineer-in-training"),
                      ("what does MCC stand for", "motor control centre"), ("What is a VFD?", "variable frequency drive"),
                      ("What is a PLC?", "programmable logic controller"), ("What's HMI?", "human-machine interface"),
                      ("What is API 505?", "API 500 and 505")):
        a = glossary_answer(q)
        assert a and needle in a, (q, a)
    for _, _, sentence in GLOSSARY:
        assert sentence.count(". ") == 0 and sentence.endswith(".")  # one sentence
        assert not spoken_text_problems(sentence), sentence


def test_glossary_does_not_answer_other_questions():
    for q in ("What is the salary?", "Who is the client?", "What is the weather?", "Can I ask a question?", "Do you use PLCs at the client?",
              "What does the day to day look like for the electrical engineers and programmers on the team in the Edmonton office"):
        assert glossary_answer(q) is None, q
    assert glossary_answer("What is APEGA?") == "APEGA is the regulator that registers professional engineers in Alberta."
    assert glossary_answer("hmm", "What is P.Eng?")  # falls back to the raw turn when the classifier's paraphrase misses


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
