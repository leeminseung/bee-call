"""Tool-call markup in a reply's text never reaches the voice; the words around it do."""

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
