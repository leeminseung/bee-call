"""Tool-call markup and stage directions in a reply's text never reach the voice; the words around them do."""

from voice.speech_filter import SpeechFilter


def speak(chunks: list[str]) -> tuple[str, SpeechFilter]:
    f = SpeechFilter()
    said = "".join(f.feed(c) for c in chunks) + f.flush()
    return said, f


def test_markup_after_the_spoken_part_is_cut_even_across_chunks():
    said, f = speak(["You're welcome. Have a good", " one! <mini", 'max:tool_call> <invoke name="end_call">', " </invoke>"])
    assert said == "You're welcome. Have a good one! "
    assert f.dropped.startswith("<minimax:tool_call>") and "</invoke>" in f.dropped


def test_a_stray_closing_tag_before_the_sentence_drops_only_the_tag():
    # GLM 5 in a synthetic call: cutting after the first tag lost this whole sentence.
    said, f = speak(["</tool", "_call>Let me check the Seoul weather forecast for you."])
    assert said == "Let me check the Seoul weather forecast for you." and f.dropped == "</tool_call>"


def test_what_sits_inside_a_tag_pair_is_dropped_and_speech_after_it_kept():
    said, f = speak(["Sounds good. <system>end_call</system>", " Take care!"])
    assert said == "Sounds good.  Take care!" and f.dropped == "<system>end_call</system>"


def test_the_shapes_seen_in_the_bench_are_all_cut():
    for leak in ("<system>end_call</system>", "<｜DSML｜function_calls", "<function_calls>", "</invoke>",
                 "<|tool_call|>", '<invoke name="end_call"/>'):
        said, f = speak(["Enjoy the hike! ", leak])
        assert said == "Enjoy the hike! " and f.dropped, leak


def test_a_plain_less_than_sign_is_spoken():
    said, f = speak(["The rain chance is <", " 10 percent, so <3 hours outside is fine."])
    assert said == "The rain chance is < 10 percent, so <3 hours outside is fine." and not f.dropped


def test_text_without_markup_is_unchanged():
    chunks = ["Saturday looks dry. ", "Gwanaksan is the easier hike, ", "about two and a half hours."]
    said, f = speak(chunks)
    assert said == "".join(chunks) and f.dropped == ""


# ---------- stage directions (the user's 14:22 call of 2026-10-15) ----------

def test_a_reply_that_is_only_a_stage_direction_says_nothing():
    """The user said only "어"; GLM 5 wrote its whole reply as "(사용자가 잠시 멈춤)" and the voice read it out."""
    for chunks in (["(사용자가 잠시 멈춤)"], ["(사용자가", " 잠시 멈춤)"], ["(사용자가 잠시 멈춤).\n"]):
        said, f = speak(chunks)
        assert said.strip() == "" and "사용자가 잠시 멈춤" in f.dropped, chunks


def test_an_english_pause_in_brackets_is_not_read_out():
    said, _ = speak(["[pause] Take your time."])
    assert said == "Take your time."


def test_only_the_aside_sentence_goes_from_a_mixed_reply():
    said, f = speak(["Sure. ", "[pause", "] What would you like ", "to do next? *thinks* Maybe a walk."])
    assert said == "Sure. What would you like to do next? Maybe a walk."
    assert "[pause]" in f.dropped and "*thinks*" in f.dropped


def test_a_parenthesis_inside_a_sentence_stays():
    for text in ("AWS(아마존 웹 서비스)는 클라우드예요.", "It costs about 3 dollars (plus tax) a day.",
                 "(1)번이 좋아요."):
        said, f = speak([text])
        assert said == text and f.dropped == "", text


def test_the_call_ai_is_told_that_every_word_it_writes_is_spoken():
    from voice.coordinator import INSTRUCTIONS
    from voice.prep import PREP_INSTRUCTIONS, PREP_INSTRUCTIONS_WITHOUT_BEE

    for text in (INSTRUCTIONS, PREP_INSTRUCTIONS, PREP_INSTRUCTIONS_WITHOUT_BEE):
        assert "spoken aloud" in text and "stage directions" in text


def test_an_opening_with_a_stage_direction_is_said_without_it():
    from voice.prep import parse_opening

    assert parse_opening("<opening>(밝게 웃으며) 안녕하세요, 맷!</opening>") == "안녕하세요, 맷!"
    assert parse_opening("<opening>[pause]</opening>") is None  # nothing left to say: the fixed greeting

