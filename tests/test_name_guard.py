"""The user's name is said at most once, in the first words (voice/name_guard.py). The Korean sentences are the call
AI's own, from calls in text against Claude Haiku 5.5 after the instructions asked it not to use the name."""

import pytest

from voice.name_guard import NameGuard, user_names

NAMES = ["Minseung", "민승"]


def said(text: str, names=NAMES) -> str:
    guard = NameGuard(names)
    return guard.feed(text) + guard.flush()


@pytest.mark.parametrize("reply, heard", [
    # The name as "you", with 님 or 씨 and the particles fixed to them.
    ("민승 님은 어떻게 보세요?", "어떻게 보세요?"),
    ("민승님은 추위 타는 편이세요, 아니면 이런 날씨가 오히려 좋으세요?", "추위 타는 편이세요, 아니면 이런 날씨가 오히려 좋으세요?"),
    ("저는 맥런치 하면 빅맥 세트가 떠오르는데, 민승 님은 늘 드시던 게 있어요?",
     "저는 맥런치 하면 빅맥 세트가 떠오르는데, 늘 드시던 게 있어요?"),
    ("그 부분은 민승 님한테 기회로 보이기도 하나요?", "그 부분은 기회로 보이기도 하나요?"),
    ("그래서 민승 씨처럼 여러 분야를 두루 알고 있는 사람이 오히려 유리할 수도 있다고 봐요.",
     "그래서 여러 분야를 두루 알고 있는 사람이 오히려 유리할 수도 있다고 봐요."),
    ("그런 쪽에서 민승 씨만의 강점이 뭐라고 생각하세요?", "그런 쪽에서 강점이 뭐라고 생각하세요?"),
    ("민승 님 주변에도 그런 역할 하는 사람이 떠오르세요?", "주변에도 그런 역할 하는 사람이 떠오르세요?"),
    ("좋아요. 민승 님이 직접 써보신 것 중에 떠오르는 사례가 있으세요?", "좋아요. 직접 써보신 것 중에 떠오르는 사례가 있으세요?"),
    # The name said to the user, set off by a comma.
    ("알겠어요, 민승 님. 오늘 푹 쉬세요.", "알겠어요. 오늘 푹 쉬세요."),
    ("알겠습니다, 민승님!", "알겠습니다!"),
    ("민승 님, 내일 등산은 Dana 님과 다시 한번 확인해 보시는 게 좋겠어요.",
     "내일 등산은 Dana 님과 다시 한번 확인해 보시는 게 좋겠어요."),
    ("Take care, Minseung. Bye!", "Take care. Bye!"),
    ("Okay, take care, Minseung.", "Okay, take care."),
    ("Minseung, how was the hike?", "How was the hike?"),
    ("Sounds good. Minseung, how was the hike?", "Sounds good. How was the hike?"),
    # Everything else stays: another person's name, the name inside a Bee item, the name as a word in a sentence.
    ("Dana 님과 약속은 정하셨어요?", "Dana 님과 약속은 정하셨어요?"),
    ("Bee에 '민승 생일 선물 사기'를 할 일로 넣을까요?", "Bee에 '민승 생일 선물 사기'를 할 일로 넣을까요?"),
    ("Minseung's idea sounds good.", "Minseung's idea sounds good."),
    ("Yes, I think so, and it helps.", "Yes, I think so, and it helps."),
    ("Hi Minseung, how was it?", "Hi Minseung, how was it?"),  # not the first words of a sentence: only openings say it
    ("민망하지만, 맞아요.", "민망하지만, 맞아요."),
])
def test_the_name_is_taken_out_of_a_reply(reply, heard):
    assert said(reply) == heard


@pytest.mark.parametrize("reply", [
    "알겠어요, 민승 님. 오늘 푹 쉬세요.",
    "저는 맥런치 하면 빅맥 세트가 떠오르는데, 민승 님은 늘 드시던 게 있어요?",
    "Take care, Minseung. Bye!",
    "Sounds good. Minseung, how was the hike?",
    "민승 님, 내일 등산은 Dana 님과 다시 한번 확인해 보시는 게 좋겠어요.",
    "민망하지만, 맞아요. 민승씨는 어때요?",
    "Bee에 '민승 생일 선물 사기'를 할 일로 넣을까요?",
])
def test_streamed_in_any_pieces_a_reply_comes_out_the_same(reply):
    """A reply streams in pieces that can split a name, its 님 or the comma before it anywhere."""
    whole = said(reply)
    for cut in range(1, len(reply)):
        guard = NameGuard(NAMES)
        assert guard.feed(reply[:cut]) + guard.feed(reply[cut:]) + guard.flush() == whole, cut
    guard = NameGuard(NAMES)
    assert "".join(guard.feed(char) for char in reply) + guard.flush() == whole


def test_text_is_held_back_only_while_a_name_may_follow():
    guard = NameGuard(NAMES)
    assert guard.feed("오늘은 날씨가 좋네요. 혹시 ") == "오늘은 날씨가 좋네요. 혹시 "
    assert guard.feed("주말에") == "주말에"
    assert guard.feed(" 민") == " "  # could be 민승
    assert guard.feed("승 님은") == ""
    assert guard.feed(" 뭐 하세요?") == "뭐 하세요?"
    assert guard.flush() == "" and guard.left_out == 1


def test_without_a_name_everything_passes_at_once():
    guard = NameGuard([])
    assert guard.feed("알겠어요, ") == "알겠어요, " and guard.flush() == ""


def test_the_names_a_call_knows():
    assert user_names("Minseung", "민승 님, 안녕하세요. 무슨 일로 전화 주셨어요?") == ["Minseung", "민승"]
    assert user_names("Minseung", "Hey Minseung, good to hear from you!") == ["Minseung"]
    assert user_names(None, "민승 씨, 안녕하세요!") == ["민승"]
    assert user_names(None, "안녕하세요, 민승 님. 무슨 일로 전화 주셨어요?") == ["민승"]
    assert user_names(None, "안녕하세요, Bee Call입니다.") == [] and user_names(" ", None) == []


def test_only_the_name_the_opening_greets_the_user_with_counts():
    """Another person named with 님 in the opening (an AI call about a conversation with one's team lead) is no form of
    the user's name: taking it out of every reply would lose who the call is about."""
    opening = "안녕하세요, 민승 님. 아까 팀장님과 나눈 이야기 때문에 전화드렸어요."
    assert user_names("Minseung", opening) == ["Minseung", "민승"]
    assert user_names(None, "안녕하세요. 어머님은 좀 괜찮아지셨어요?") == []
    assert said("팀장님과 다시 얘기해 보시는 건 어때요?", user_names("Minseung", opening)) == \
        "팀장님과 다시 얘기해 보시는 건 어때요?"
