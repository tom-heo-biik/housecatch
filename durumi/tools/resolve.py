"""Find the brand named in a request. Importing this module has no side effects.

Matching rules:
- terms are each brand's id, name and aliases
- Latin letters match case-insensitively; Hangul and other scripts match exactly
- a space inside a term matches any run of whitespace in the request
- a term that begins or ends with an ASCII letter or digit must not touch another ASCII
  letter or digit on that side ("durumi" does not match inside "durumis"); Hangul particles
  may follow directly ("두루미의")
- overlapping matches are resolved longest first, so "두루미 블로그" beats a shorter term inside it
"""
from __future__ import annotations

import re
from bisect import bisect_left

_ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")


def _terms(brand) -> list[str]:
    seen, out = set(), []
    for term in (brand.id, brand.name, *brand.aliases):
        term = " ".join(str(term).split())
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            out.append(term)
    return out


def _pattern(term: str) -> re.Pattern:
    body = r"\s+".join(re.escape(word) for word in term.split(" "))
    if _ASCII_ALNUM.match(term[0]):
        body = r"(?<![A-Za-z0-9])" + body
    if _ASCII_ALNUM.match(term[-1]):
        body = body + r"(?![A-Za-z0-9])"
    return re.compile(body, re.IGNORECASE)


def _matches(text: str, brands) -> list[tuple[int, int, object]]:
    """Non-overlapping (start, end, brand) spans, longest first, returned in text order."""
    found = []
    for order, brand in enumerate(brands):
        for term in _terms(brand):
            for m in _pattern(term).finditer(text):
                found.append((m.start(), m.end(), order, brand))
    found.sort(key=lambda f: (-(f[1] - f[0]), f[0], f[2]))
    kept = []
    for start, end, order, brand in found:
        if all(end <= s or start >= e for s, e, _, _ in kept):
            kept.append((start, end, order, brand))
    kept.sort(key=lambda f: f[0])
    return [(s, e, b) for s, e, _, b in kept]


def resolve_brand(request_text: str, brands) -> tuple[object | None, list]:
    """(brand, [brand]) for one brand, (None, candidates) for several, (None, []) for none.

    Candidates are listed in order of first mention.
    """
    candidates, ids = [], set()
    for _, _, brand in _matches(request_text or "", list(brands)):
        if brand.id not in ids:
            ids.add(brand.id)
            candidates.append(brand)
    if len(candidates) == 1:
        return candidates[0], candidates
    if not candidates:
        return None, []
    return None, candidates


def without_brand(request_text: str, brand) -> str:
    """The request with the brand's mentions removed. Everything else is kept as written.

    For topic_kind alone: with the name left in, a request that only asks for a post ('두루미 블로그
    써 줘') would seem to name a topic. The name is taken out nowhere else: the node reads the request
    as written, and the topic that check shows and meta.json keeps is the request as written too
    (Tom, 2026-10-02). Returns an empty string when nothing but whitespace remains.
    """
    text = request_text or ""
    if brand is not None:
        spans = _matches(text, [brand])
        for start, end, _ in reversed(spans):
            text = text[:start] + text[end:]
    return text if text.strip() else ""


# Verb forms are a stem and an ending ('뽑아' and '서', '만들어' and '줘') rather than a list of their
# own. The auxiliaries may also stand apart, as standard spacing writes them ('만들어 줘', '써 주실래요');
# the connective '서' never does.
AUXILIARIES = ("줘", "줘요", "주세요", "줄래", "줄래요", "주라", "줄", "주실", "주실래", "주실래요", "주시겠어",
               "주시겠어요", "봐", "봐요", "볼래")
ENDINGS = ("", *AUXILIARIES, "서")
REQUEST_STEMS = ("써", "작성해", "만들어", "올려", "적어", "해", "부탁해")
HANDOVER_STEMS = ("골라", "정해", "뽑아", "추천해", "맡겨")


def _verb_forms(stems: tuple[str, ...]) -> set[str]:
    return {stem + ending for stem in stems for ending in ENDINGS}


# The words that only asked for a post until 2026-10-05, as written out then: those that are not verb
# forms (not all of them nouns: '새로', '좀', '요'), and with them the verb forms.
_EARLIER_NOUNS = frozenset({"블로그", "포스팅", "포스트", "글", "게시글", "원고", "콘텐츠", "네이버", "하나", "한", "편",
                            "개", "좀", "새", "새로", "오늘", "이번", "작성", "부탁", "요", "주제"})
_EARLIER_REQUEST_WORDS = _EARLIER_NOUNS | {"써", "써줘", "써주세요", "써줄래", "줘", "주세요", "줄래", "작성해", "작성해줘",
                                          "작성해주세요", "해", "해줘", "해주세요", "부탁해", "부탁해요", "부탁합니다"}
# The words that left the choice of topic to the writer until 2026-10-05, as written out then.
_EARLIER_HANDOVER_WORDS = frozenset({"알아서", "아무거나", "아무", "마음대로", "자유", "자유롭게", "추천", "추천해", "추천해줘",
                                     "정해", "정해줘", "정해주세요", "골라", "골라줘", "골라주세요", "맡길게", "맡길게요", "맡겨"})

# Words that only ask for a post, and words that leave the choice of topic to the writer. Only words
# that cannot name a topic on their own belong here (topic_kind says why). '니가', '네가', '너가' and
# '당신' only address the writer ('니가 알아서 써줘'); '당신이' and '당신께서' are listed whole, since a case
# particle stays on '당신' ('당신을 주제로' names a topic). '주제카드' is what the tool calls the cards it
# hands out ('주제 카드에서 하나 뽑아줘'); '카드' alone may be a topic and is not listed.
REQUEST_WORDS = frozenset({
    *_EARLIER_REQUEST_WORDS, "그냥", "잘", "제발", "니가", "네가", "너가", "당신", "당신이", "당신께서",
    "부탁드려", "부탁드려요", "부탁드립니다", "부탁드릴게요", *AUXILIARIES, *_verb_forms(REQUEST_STEMS),
})
HANDOVER_WORDS = frozenset({
    "알아서", "아무거나", "아무꺼나", "암거나", "아무", "아무주제", "마음대로", "맘대로", "자유", "자유롭게", "추천",
    "맡길게", "맡길게요", "랜덤", "랜덤하게", "무작위", "임의", "아무렇게나", "뭐든", "뭐든지", "뭐라도",
    "상관없이", "상관없어", "무관", "다른", "주제카드",
    *_verb_forms(HANDOVER_STEMS),
})
# A count ('3편', '1개', '2건') says how many posts, not what they are about, so it only asks for them.
_COUNT = re.compile(r"[0-9]+[편개건]")
_VERB_WORDS = frozenset({*_verb_forms(REQUEST_STEMS), *_verb_forms(HANDOVER_STEMS), *AUXILIARIES})

# A particle comes off a listed word only as the lines below allow (_takes). Kept, it makes the word
# one outside the lists, and a topic named that way stays given ('해를', '랜덤을', '새로나').
# A verb form or an auxiliary gives up '만' and '도' alone ('써만 줘'; '정해나' and '해를' stay whole).
_VERB_PARTICLES = ("만", "도")
# A case particle comes off the earlier request nouns and '주제카드' only. On a hand-over word or on a
# word added on 2026-10-05 it is taken to mark what the post is about ('랜덤을 주제로', '당신을 주제로').
_CASE_PARTICLES = ("에서", "을", "를", "이", "가", "은", "는", "에")
_CASE_STEMS = _EARLIER_NOUNS | {"주제카드"}
# '으로' and '로' come off any listed word that is not a verb form ('아무거나로', '랜덤으로 써줘'). '도' and '만'
# come off only the words listed before 2026-10-05 ('한 편만'), as they did then, and a count ('3편만'): on a
# word added since, they mark it as the topic ('랜덤만 주제로', '당신만 주제로'; review, 2026-10-05).
_PLAIN_PARTICLES = ("으로", "로", "도", "만")
_EARLIER_ONLY_PARTICLES = ("도", "만")
_EARLIER_WORDS = _EARLIER_REQUEST_WORDS | _EARLIER_HANDOVER_WORDS
# The particles added on 2026-10-05 also end names and words of their own ('한나', '새로나'), so they come
# off these words alone, and off a one-letter one only when it is '글' ('글이라도').
_LATER_PARTICLES = ("이라도", "이나", "이든", "든지", "라도", "나", "든", "엔", "에는", "로나", "으로나")
_LATER_STEMS = frozenset({"블로그", "포스팅", "포스트", "글", "게시글", "원고", "콘텐츠", "주제", "하나", "편", "이번", "오늘",
                          "아무거나", "아무꺼나", "암거나", "아무주제", "랜덤", "무작위", "임의", "자유", "주제카드"})
# Of these, '나', '든', '든지' and '라도' follow a vowel, '이나', '이든' and '이라도' a final consonant
# (받침), as in '주제나' and '글이나'.
_AFTER_VOWEL = frozenset({"나", "든", "든지", "라도"})
_AFTER_CONSONANT = frozenset({"이나", "이든", "이라도"})
# '요', added on 2026-10-05 as well, comes off any listed word of two letters or more that is not a verb
# form ('아무거나요', '알아서요'; '개요' and '정해요' stay whole).
_POLITE = "요"
# Longest first, so '이나' is tried before '나'.
PARTICLES = tuple(sorted((*_CASE_PARTICLES, *_PLAIN_PARTICLES, *_LATER_PARTICLES, _POLITE), key=len, reverse=True))
# Letters and digits of every script make words ('旅行' names a topic as '여행' does). Hangul
# compatibility jamo (U+3131 to U+318E, 'ㅋㅋ', 'ㅠㅠ') are chat filler, passed over like punctuation.
_WORD = re.compile(r"[^\W_\u3131-\u318e]+")
# Listed words written apart, joined before the lists judge them. '주제 카드' is joined within a line only:
# the answer to a question about the topic is added on the next line (SKILL.md), and a '카드' there may be it.
_JOINS = ((re.compile(r"아무\s+([거꺼])나"), r"아무\1나"),  # '아무 거나', '아무 꺼나'
          (re.compile(r"상관\s+없([이어])"), r"상관없\1"),  # '상관 없이', '상관 없어'
          (re.compile(r"주제[^\S\r\n]+카드"), "주제카드"))  # '주제 카드'
# A polite question asks for a post as '써 줘' does, so its '수 있…' goes, but only right after '줄' or
# '주실', spaced or not ('써줄 수 있어?', '써줄수있어?', '만들어 주실 수 있나요'), only with one of these
# endings ('줄 수 있는 자유' keeps it), and only when no Hangul follows ('써줄수있어곰팡이로' keeps it). '수'
# and '있어' are not request words: anywhere else they stay, and the topic is given.
_CAN_YOU = re.compile(r"(줄|주실)\s*수\s*있(?:어요|어|나요|니|을까요|을까|으세요|으신가요|겠어요|겠어|습니까)(?![가-힣])")
# Quotation and title marks: ‘ ’ “ ” 「 」 『 』 《 》 〈 〉 « » ‹ ›, each opening mark closed by its own,
# and ' " ` ＂ ＇, each closed by the same mark.
_PAIRED_MARKS = {"\u2018": "\u2019", "\u201c": "\u201d", "\u300c": "\u300d", "\u300e": "\u300f", "\u300a": "\u300b",
                 "\u3008": "\u3009", "\u00ab": "\u00bb", "\u2039": "\u203a"}
_SAME_MARKS = "'\"`\uff02\uff07"
_QUOTES = re.compile("[" + re.escape("".join(_PAIRED_MARKS) + "".join(_PAIRED_MARKS.values()) + _SAME_MARKS) + "]")

TOPIC_GIVEN = "given"
TOPIC_MISSING = "missing"
TOPIC_HANDED_OVER = "handed_over"


def _asks(word: str) -> bool:
    """True for a word that only asks for a post."""
    return word in REQUEST_WORDS or _COUNT.fullmatch(word) is not None


def _listed(word: str) -> bool:
    """True for a word that only asks for a post or leaves the choice of topic to the writer."""
    return _asks(word) or word in HANDOVER_WORDS


def _final_consonant(letter: str) -> bool | None:
    """Whether a Hangul syllable ends in a consonant (받침); None for any other letter."""
    if "가" <= letter <= "힣":
        return (ord(letter) - ord("가")) % 28 != 0
    return None


def _takes(stem: str, particle: str) -> bool:
    """True when the particle may come off a word, leaving this stem (the rules above PARTICLES)."""
    if not _listed(stem):
        return False
    if stem in _VERB_WORDS:
        return particle in _VERB_PARTICLES
    if particle in _CASE_PARTICLES:
        return stem in _CASE_STEMS
    if particle in _PLAIN_PARTICLES:
        return (particle not in _EARLIER_ONLY_PARTICLES or stem in _EARLIER_WORDS
                or _COUNT.fullmatch(stem) is not None)
    if particle == _POLITE:
        return len(stem) > 1
    if stem not in _LATER_STEMS or (len(stem) == 1 and stem != "글"):
        return False
    if particle in _AFTER_VOWEL:
        return _final_consonant(stem[-1]) is False
    if particle in _AFTER_CONSONANT:
        return _final_consonant(stem[-1]) is True
    return True


def _bare(word: str) -> str:
    for particle in PARTICLES:
        if word.endswith(particle) and _takes(word[: -len(particle)], particle):
            return word[: -len(particle)]
    return word


def _words(text: str) -> list[str]:
    """The words topic_kind judges, particles off, after the words written apart are joined (_JOINS)
    and a polite question's '수 있…' is dropped."""
    for spaced, joined in _JOINS:
        text = spaced.sub(joined, text)
    text = _CAN_YOU.sub(r"\1", text)
    return [_bare(word) for word in _WORD.findall(text)]


def _held(text: str) -> list[tuple[int, int]] | None:
    """Where what each pair of marks holds begins and ends, in text order and nested pairs too; None when a
    mark pairs with none."""
    open_marks, held = [], []
    for found in _QUOTES.finditer(text):
        mark = found.group()
        if open_marks and mark == open_marks[-1][0]:
            held.append((open_marks.pop()[1], found.start()))
        elif mark in _PAIRED_MARKS or mark in _SAME_MARKS:
            open_marks.append((_PAIRED_MARKS.get(mark, mark), found.end()))
        else:
            return None  # a closing mark with nothing open
    return None if open_marks else sorted(held)


def _quoted_words(text: str) -> list[str]:
    """The words that quotation and title marks hold in text, as written: no particle comes off and no
    words are joined, so a quoted name stays whole ('‘요가’' is not '요' and '가', '‘하나’' stays '하나').

    A pair that holds no word holds nothing, as the marks around the brand's name once without_brand
    has taken the name out ('‘하우스캐치’ 블로그 아무거나 써줘'). Nor does one pair that holds every word
    when no other pair holds a word: the whole request was quoted ('"하우스캐치 블로그 아무거나 써줘"'), and
    it is read without its marks. When a mark pairs with none ('^^'' or a mark left open), what it
    holds cannot be told, and every word counts as held.
    """
    held = _held(text)
    if held is None:
        return _WORD.findall(text)
    # Marks are not letters, so a word a pair holds begins between its marks.
    starts = [found.start() for found in _WORD.finditer(text)]
    holding = [(start, end) for start, end in held if bisect_left(starts, start) < bisect_left(starts, end)]
    if len(holding) == 1 and starts[0] >= holding[0][0] and starts[-1] < holding[0][1]:
        return []
    outermost, last = [], -1
    for start, end in holding:  # in text order, so a pair nested in another comes after it
        if start > last:
            outermost.append((start, end))
            last = end
    return [word for start, end in outermost for word in _WORD.findall(text[start:end])]


def topic_kind(request_text: str, brand=None) -> str:
    """Whether what is left of a request without its brand (without_brand) names a topic.

    TOPIC_MISSING when every word only asks for a post ('블로그 써 줘') or nothing is left, and any quoted
    word is, as written, one that asked for a post until 2026-10-05 ('"블로그" 써줘'): the crane original
    made the topic a required input never to be guessed, so the person is asked.
    Otherwise TOPIC_GIVEN when a quotation or title mark (_QUOTES) holds a word (_quoted_words; marks
    that hold none or wrap the whole request are passed over): what a person quotes is a name they
    chose, even a word of the lists, and it is compared as written ('‘요가’', '‘하나’', '‘랜덤’ 주제로',
    '‘당신’ 주제로'). A hand-over word quoted alone ('블로그 '아무거나' 써줘') is taken the same way and
    gets no card, the cheaper of the two mistakes below.
    TOPIC_HANDED_OVER when the words besides those that ask leave the choice to the writer ('주제는
    알아서 정해 줘'): the node picks it from the brand's documents (Tom, 2026-09-30), or the tool
    assigns one of the brand's topic cards when it has them (MGM, 2026-10-05). TOPIC_GIVEN otherwise.

    The two mistakes do not cost the same: a topic taken for a hand-over is replaced by a card, a
    hand-over taken for a topic only goes without one. Hence the lists hold only words that cannot
    name a topic on their own, and Tom's rule stands: one word outside the lists makes the topic
    given ('곰팡이 말고 아무거나'). The words are judged as _words leaves them: verb forms are stems
    and endings, a count ('3편') only asks, words written apart ('아무 거나', '상관 없이', '주제 카드')
    are joined, and a polite '써줄 수 있어?' is '써줄'. A particle comes off a listed word only as the
    rules above PARTICLES allow; a word that keeps one is outside the lists ('해를', '랜덤을', '새로나').
    Widened on 2026-10-05 (MGM) after '주제 아무거나 뽑아서 만들어줘.' was taken for a topic, and
    narrowed the same day where review found topics taken for hand-overs ('정해나', '‘랜덤’', '랜덤을
    주제로', '‘요가’') or requests to be asked about ('‘당신’ 주제로').
    """
    text = without_brand(request_text, brand)
    quoted = _quoted_words(text)
    words = _words(text)
    if all(_asks(word) for word in words) and all(word in _EARLIER_REQUEST_WORDS for word in quoted):
        return TOPIC_MISSING
    if quoted:
        return TOPIC_GIVEN
    if all(_listed(word) for word in words):
        return TOPIC_HANDED_OVER
    return TOPIC_GIVEN
