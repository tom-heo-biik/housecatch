"""Topic file: brands/{id}/topics.json, the topics a brand has listed for requests that leave the topic open.

A person or the topic step writes the file; this module only reads it. Its shape is

    {"when_used_up": "stop",
     "topics": [{"id": "h001", "topic": "...", "keyword": "...", "approved": true, "note": "..."}]}

- id names a topic for good, so that a topic whose words are changed is still that topic. It is what the
  sentences below call the topic by, in place of printing its words.
- topic is the one line handed to the node.
- keyword is the search term the post aims at. It may be left out.
- approved marks the topics that may be handed out. Left out, the topic is not approved.
- note is a person's memo.
- when_used_up, at the top, is "stop" or "reuse": what to do once every approved topic has been used.
  Left out, it is "stop".
Keys other than these are passed over, so that the topic step can add its own without this module
refusing the file. A key written twice, at the top or in a topic, is refused whichever key it is: json
would keep the last one, and a second "topics" pasted under the first would drop the first without a
word.

load reads the file. A file of another shape is a TopicFileError, whose message is one sentence for a
person. problems says what keeps the topics from being handed out: an id that cannot be used or is used
twice, and for the approved topics an empty or repeated sentence and what tools/lint.py would stop in a
post that carried their words. A topic not approved yet is not judged for its words, so that one
unfinished topic does not stop the brand. keyword_notes tells which topics aim at one keyword, which
stops nothing.

Importing this module has no side effects.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from config import Rules
from lint import META_MARKERS, Unit, _char_name, _check_markdown, _check_meta, emoji_base, find_emojis, promoted_emoji

TOPICS_FILE = "topics.json"  # in the brand folder, beside brand.toml
STOP, REUSE = "stop", "reuse"  # the values of when_used_up
# What an id may look like: at most 32 of a to z, 0 to 9 and '-', the first not a '-'. Such an id is
# safe in a sentence, where it is printed as written (LABEL). In a file name the look is not enough: the
# id must not be one of the device names below either (usable_id).
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# Names Windows gives to devices: on some versions a file named con.1.json opens the device, so
# creating it does not fail when it "exists" and two runs could hold one attempt.
RESERVED_IDS = frozenset({"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))})
KEY_HEX = 16  # hex digits of the sha256 kept in a sentence key
COLON_ALLOWED = r"(?<=\d):(?=\d)"  # lint._check_punctuation's default when form.toml has none
COLON_RULE_UNREADABLE = "규칙 파일 rules/form.toml의 colon_allowed를 읽지 못했습니다."
# The line boundaries of str.splitlines(): control characters, and U+2028 and U+2029 besides.
LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}")
# Invisible characters (_invisible) are format characters (this category, such as U+200B) and the
# combining marks below that show nothing of their own: the variation selectors, the combining grapheme
# joiner U+034F, the Khmer inherent vowels and the Mongolian variation selectors. Other combining marks
# are parts of letters (Thai, Hindi, Hebrew and Arabic vowel signs) and stay.
INVISIBLE_CATEGORIES = frozenset({"Cf"})
INVISIBLE_MARKS = frozenset(chr(code) for code in (*range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0),
                                                   0x034F, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180F))
# And letters or signs of other categories that show as an empty space: the Hangul fillers, the braille
# blank and the musical null notehead.
BLANK_LOOKING = frozenset("\N{HANGUL CHOSEONG FILLER}\N{HANGUL JUNGSEONG FILLER}\N{HANGUL FILLER}"
                          "\N{HALFWIDTH HANGUL FILLER}\N{BRAILLE PATTERN BLANK}"
                          "\N{MUSICAL SYMBOL NULL NOTEHEAD}")
MARK_CHARS = "[]{}"  # slot and color marks of the manuscript syntax; bold is '**'
BOLD_MARK = "**"

# The sentences of TopicFileError. n counts the items of topics from 1, line and column count from 1 as
# json.JSONDecodeError does, and no sentence prints what the file holds, a key's name included.
UNREADABLE = "주제 파일 topics.json을 읽지 못했습니다. "  # every TopicFileError message starts with this
NOT_OPENED = UNREADABLE + "파일을 열지 못했습니다."  # a folder in its place, no permission
NOT_JSON = UNREADABLE + "UTF-8 JSON이 아닙니다."  # bytes that are not UTF-8; json giving up without a place
NOT_JSON_AT = UNREADABLE + "JSON 문법이 맞지 않습니다. {line}번째 줄 {column}번째 칸 근처를 봐 주십시오."
NOT_OBJECT = UNREADABLE + "맨 위가 객체가 아닙니다."
SAME_KEY_TOP = UNREADABLE + "맨 위에 같은 항목 이름이 두 번 적혀 있습니다."
NOT_LIST = UNREADABLE + "topics가 목록이 아닙니다."  # also when there is no topics key
ITEM_NOT_OBJECT = UNREADABLE + "topics의 {n}번째 항목이 객체가 아닙니다."
SAME_KEY_ITEM = UNREADABLE + "topics의 {n}번째 항목에 같은 항목 이름이 두 번 적혀 있습니다."
TEXT_REQUIRED = UNREADABLE + "topics의 {n}번째 항목에 {key} 값이 없거나 문자열이 아닙니다."  # key: id, topic
TEXT_OPTIONAL = UNREADABLE + "topics의 {n}번째 항목의 {key} 값이 문자열이 아닙니다."  # key: keyword, note
NOT_BOOLEAN = UNREADABLE + "topics의 {n}번째 항목의 approved 값이 true나 false가 아닙니다."
BAD_MODE = UNREADABLE + "when_used_up 값은 stop이나 reuse여야 합니다."

# The sentences of problems about ids name a topic by its place in the file, from 1.
BAD_ID = "주제 파일의 {n}번째 id는 쓸 수 없습니다. 영문 소문자나 숫자로 시작하고 영문 소문자, 숫자, 하이픈만 32자까지 씁니다."
RESERVED_ID = "주제 파일의 {n}번째 id는 윈도우가 장치 이름으로 쓰는 낱말이라 쓸 수 없습니다."
SAME_ID = "주제 파일의 {n}번째 id가 {m}번째와 같습니다."  # m is the first place of that id
LABEL = "주제 {id}번"  # how the sentences of problems about a topic's words name it
SAME_KEYWORD = "주제 {ids}이 같은 검색어를 겨냥합니다."  # keyword_notes; ids: "h001번, h004번", in file order


class TopicFileError(Exception):
    """topics.json is there and cannot be read as a topic file. The message is one sentence for a person."""


@dataclass(frozen=True)
class Topic:
    id: str
    topic: str  # the one line handed to the node, without the whitespace around it
    keyword: str  # "" when the file gives none
    approved: bool  # False when the file gives none
    note: str  # "" when the file gives none


@dataclass(frozen=True)
class TopicFile:
    path: Path
    when_used_up: str  # STOP, also when the file gives none, or REUSE
    topics: tuple[Topic, ...]  # in the order of the file


def path_of(brand_dir: Path) -> Path:
    """Where a brand's topic file is: brand_dir/topics.json."""
    return Path(brand_dir) / TOPICS_FILE


class _KeyTwice(dict):
    """A JSON object that writes one key more than once, as _pairs reads it. It holds the last value of each key."""


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    """json's object_pairs_hook: the object as a dict, and as a _KeyTwice when a key is written more than once.

    json by itself keeps the last value of such a key without a word, so a second "topics" pasted under
    the first would drop every topic of the first. Keys are compared as json reads them, so one spelled
    with an escape is the key it spells.
    """
    read = dict(pairs)
    return _KeyTwice(read) if len(read) < len(pairs) else read


def _well_formed(text: str) -> bool:
    """False for text UTF-8 cannot hold: half of a surrogate pair, which JSON can spell alone (\\ud83d)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _text(item: dict, key: str, place: int, *, required: bool) -> str:
    """item[key] when it is a string, and "" for a key that is not required and not there.

    TopicFileError naming the place and the key otherwise: a required key that is missing, a value that
    is no string, or a string UTF-8 cannot hold (_well_formed), which could not be written out again.
    """
    if not required and key not in item:
        return ""
    value = item.get(key)
    if not isinstance(value, str) or not _well_formed(value):
        raise TopicFileError((TEXT_REQUIRED if required else TEXT_OPTIONAL).format(n=place, key=key))
    return value


def _topic(item: object, place: int) -> Topic:
    """The item at place (from 1) of topics as a Topic; TopicFileError naming the place for another shape."""
    if not isinstance(item, dict):
        raise TopicFileError(ITEM_NOT_OBJECT.format(n=place))
    if isinstance(item, _KeyTwice):
        raise TopicFileError(SAME_KEY_ITEM.format(n=place))
    topic_id = _text(item, "id", place, required=True)
    line = _text(item, "topic", place, required=True)
    keyword = _text(item, "keyword", place, required=False)
    approved = item.get("approved", False)
    if not isinstance(approved, bool):  # 1, "true" and null are not a yes or a no
        raise TopicFileError(NOT_BOOLEAN.format(n=place))
    note = _text(item, "note", place, required=False)
    return Topic(id=topic_id, topic=line.strip(), keyword=keyword, approved=approved, note=note)


def load(brand_dir: Path) -> TopicFile | None:
    """A brand's topic file as a TopicFile; None when the brand folder has no topics.json.

    id and topic must be there as strings. topic is stripped of the whitespace around it; keyword and
    note are kept as written and are "" when left out, approved is False when left out and when_used_up
    is STOP when left out. Keys this module does not know are passed over, at the top and in an item.
    Only the shape is judged here: what an id or a topic says (an id that cannot be used, an empty
    topic) is for problems. A file that starts with a UTF-8 byte order mark, as some editors save it,
    is read as the same file without the mark, the lines and columns of its slips included.

    TopicFileError, with one of the sentences above, for a file that is there and is not that: one that
    cannot be opened (NOT_OPENED); bytes that are not UTF-8 (NOT_JSON); text that is not JSON, an empty
    file among them, with the line and the column json.JSONDecodeError names (NOT_JSON_AT; for one slip
    the place may differ by Python, so the sentence says near); what json gives up on without naming a
    place, such as brackets nested deeper than it reads (NOT_JSON); a top that is no object; a key
    written twice at the top or in an item of topics, whichever key it is (SAME_KEY_TOP, SAME_KEY_ITEM;
    inside a value that is passed over it is passed over too); topics that is no list or is missing; an
    item that is no object; a value of another type than its key takes; or a when_used_up that is
    neither "stop" nor "reuse". The message is about one thing: load stops at the first it finds.
    """
    path = path_of(brand_dir)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TopicFileError(NOT_OPENED) from exc
    try:
        # utf-8-sig: a byte order mark at the head is dropped, so that it is not read as a slip at line 1.
        raw = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs)
    except json.JSONDecodeError as exc:
        raise TopicFileError(NOT_JSON_AT.format(line=exc.lineno, column=exc.colno)) from exc
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError is a ValueError
        raise TopicFileError(NOT_JSON) from exc
    if not isinstance(raw, dict):
        raise TopicFileError(NOT_OBJECT)
    if isinstance(raw, _KeyTwice):
        raise TopicFileError(SAME_KEY_TOP)
    mode = raw.get("when_used_up", STOP)
    if mode not in (STOP, REUSE):
        raise TopicFileError(BAD_MODE)
    listed = raw.get("topics")
    if not isinstance(listed, list):
        raise TopicFileError(NOT_LIST)
    return TopicFile(path=path, when_used_up=mode,
                     topics=tuple(_topic(item, place) for place, item in enumerate(listed, start=1)))


def _invisible(ch: str) -> bool:
    """True for a character sentence_key drops and problems names as invisible.

    These are INVISIBLE_CATEGORIES, INVISIBLE_MARKS and BLANK_LOOKING. Read the text in NFC first, so
    that a mark that joins its letter (a Mac file name gives NFD) is part of that letter.
    """
    return unicodedata.category(ch) in INVISIBLE_CATEGORIES or ch in INVISIBLE_MARKS or ch in BLANK_LOOKING


def _words(text: str) -> str:
    """A topic's words as sentence_key reads them.

    Format characters (Cf) are dropped, then the text is put in NFC, then the invisible characters NFC
    leaves (_invisible) are dropped and NFC is applied once more, and whitespace runs become one space.
    The format characters go before NFC, so that one between two jamo cannot keep them from composing;
    the second NFC composes what a dropped mark kept apart.
    """
    shown = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    kept = "".join(ch for ch in unicodedata.normalize("NFC", shown) if not _invisible(ch))
    return " ".join(unicodedata.normalize("NFC", kept).split())


def sentence_key(text: str) -> str:
    """The first 16 hex digits of the sha256 of a topic's words (_words), as UTF-8.

    The same topic typed in another Unicode form (a Mac file name gives NFD), with other spacing or
    with invisible characters (U+200B, a variation selector, U+034F) has the same key; any other change
    of wording is another key. The recipe is the one the claim files of the topic cards are named by
    (g{generation}-{key}.json in the runs folder's _cards), so it must not change: a sentence claimed
    under such a name would look unclaimed.
    """
    return hashlib.sha256(_words(text).encode("utf-8")).hexdigest()[:KEY_HEX]


def blank(line: str) -> bool:
    """True when nothing in the line, read in NFC, shows: only whitespace, control or invisible characters.

    problems asks it of a topic's words, and tools/ledger.py of the lines of a request, so that the two
    take the same lines for empty.
    """
    return all(ch.isspace() or unicodedata.category(ch) == "Cc" or _invisible(ch)
               for ch in unicodedata.normalize("NFC", line))


def usable_id(text: object) -> bool:
    """True for a string that matches ID in full and is not one of RESERVED_IDS."""
    return isinstance(text, str) and ID.fullmatch(text) is not None and text not in RESERVED_IDS


def id_problems(listed: Sequence[Topic]) -> list[str]:
    """One sentence for each id that cannot be used (usable_id) and for each id an earlier topic has, in file order.

    Every topic is looked at, approved or not. The sentences name a topic by its place, from 1, and none
    prints an id: an id that cannot be used may hold anything. An id that does not match ID gets BAD_ID
    and one that matches it and is a device name of Windows (RESERVED_IDS) gets RESERVED_ID. Such an id
    gets that sentence alone, and is not compared with the others. problems gives these first; the topics
    command asks them alone, before it prints a table that names each topic by its id.
    """
    found: list[str] = []
    first: dict[str, int] = {}
    for place, topic in enumerate(listed, start=1):
        if not ID.fullmatch(topic.id):
            found.append(BAD_ID.format(n=place))
        elif topic.id in RESERVED_IDS:
            found.append(RESERVED_ID.format(n=place))
        elif topic.id in first:
            found.append(SAME_ID.format(n=place, m=first[topic.id]))
        else:
            first[topic.id] = place
    return found


def _shape_problems(approved: Sequence[Topic]) -> dict[str, str]:
    """Topic id to its sentence, for an empty topic and for a topic with the key of an earlier one of approved."""
    found: dict[str, str] = {}
    first: dict[str, str] = {}
    for topic in approved:
        label = LABEL.format(id=topic.id)
        if blank(topic.topic):
            found[topic.id] = f"{label}이 비었습니다."
            continue
        key = sentence_key(topic.topic)
        if key in first:
            found[topic.id] = (f"{label}이 {first[key]}번과 같습니다. 공백, 유니코드 정규화, 보이지 않는 "
                               "서식 문자만 다른 주제는 같은 주제로 봅니다.")
        else:
            first[key] = topic.id
    return found


def _code_points(text: str) -> str:
    """'U+1F4A7', or 'U+C218 U+FE0F' for two characters: text named without printing it."""
    return " ".join(f"U+{ord(ch):04X}" for ch in text)


def _character_problems(label: str, line: str, banned: dict, form: dict) -> list[str]:
    """One sentence for each character or emoji of one topic that problems stops, in order of first appearance.

    The topic is read in NFC, as sentence_key reads it, so that a combining mark that joins the letter
    before it is part of that letter. A character is judged once, by the first of: banned in
    rules/form.toml (lint L001), a line break, another control character, an emoji lint stops wherever
    it stands (lint.find_emojis with lint.promoted_emoji, outside rules/form.toml's [emoji] allowed, as
    lint L010 judges; a VS16 that find_emojis counts with the character before it is judged with that
    emoji), an invisible character (_invisible), a mark of the manuscript syntax ('[', ']', '{', '}',
    '**').
    """
    text = unicodedata.normalize("NFC", line)
    head = f"{label}에 "
    emojis = dict(find_emojis(text, promoted_emoji(form)))
    allowed = {emoji_base(e) for e in form.get("emoji", {}).get("allowed", [])}
    tails = {i for at, seq in emojis.items() for i in range(at + 1, at + len(seq))}
    found: list[str] = []
    for i, ch in enumerate(text):
        code = f"U+{ord(ch):04X}"
        category = unicodedata.category(ch)
        sentence = None
        if ch in banned:
            sentence = f"{head}{_char_name(banned[ch], ch)} 문자가 있습니다."
        elif ch in LINE_BREAKS:
            sentence = f"{head}줄바꿈 문자({code})가 있습니다."
        elif category == "Cc":
            sentence = f"{head}제어 문자({code})가 있습니다."
        elif i in emojis:
            if emoji_base(emojis[i]) not in allowed:
                sentence = f"{head}허용 목록 밖의 이모지({_code_points(emojis[i])})가 있습니다."
        elif i in tails:
            pass  # the VS16 of the emoji before it, judged with that emoji
        elif _invisible(ch):
            sentence = f"{head}보이지 않는 문자({code})가 있습니다."
        elif ch in MARK_CHARS or text.startswith(BOLD_MARK, i):
            sentence = f"{head}원고 문법 기호({BOLD_MARK if ch == '*' else ch})가 있습니다."
        if sentence is not None and sentence not in found:
            found.append(sentence)
    return found


def _stray_colon(line: str, allowed: re.Pattern[str]) -> bool:
    """True when the topic has a colon the colon_allowed pattern does not cover (lint L003; a topic has no link)."""
    covered = {m.start() + k for m in allowed.finditer(line) for k, ch in enumerate(m.group(0)) if ch == ":"}
    return any(ch == ":" and i not in covered for i, ch in enumerate(line))


def _phrase_numbers(banned_phrases: Sequence[str]) -> dict[str, int]:
    """Each banned phrase in NFC to its number in banned_phrases, from 1 as written.

    Entries that are no string or are empty are counted and skipped; a phrase written twice (in one
    Unicode form or in two) keeps its first number.
    """
    numbers: dict[str, int] = {}
    for number, phrase in enumerate(banned_phrases, start=1):
        if isinstance(phrase, str) and phrase:
            numbers.setdefault(unicodedata.normalize("NFC", phrase), number)
    return numbers


def problems(found: TopicFile, rules: Rules, banned_phrases: Sequence[str] = ()) -> list[str]:
    """Sentences naming what keeps the topics from being handed out; an empty list when nothing does.

    The ids come first, those of every topic, approved or not (id_problems). When an id cannot be used
    or is used twice, those sentences are all that is returned and no topic is judged for its words:
    the sentences below name a topic by its id.

    Then the approved topics are judged, in file order. A node may carry a topic's words into the post,
    where tools/lint.py would stop them, and a topic must say something that can be told from the
    others. So each approved topic is judged for, in this order:
    - being empty (only whitespace, control or invisible characters) or having the sentence_key of an
      earlier approved topic;
    - characters (_character_problems): those rules/form.toml bans (lint L001; the look-alikes it only
      warns on, L002, pass), line breaks and other control characters, the emoji lint stops wherever
      they stand (L010), invisible characters such as U+200B, U+034F or a variation selector, and the
      manuscript marks '**', '[', ']', '{', '}'. Signs lint does not count as emoji (U+00B0, U+33A1,
      U+2103, U+2605) pass, and so do the emoji of rules/form.toml's allowed list, which lint judges by
      their place in the post (L011 to L013), a place a topic does not have;
    - a colon not between digits by colon_allowed (lint L003);
    - the brand's banned phrases (lint L030), compared in NFC;
    - markdown the manuscript syntax does not have (lint L070) and sentences about the form rather than
      for the reader (lint L050, rules/form.toml's [manuscript] meta_markers), judged by lint's own
      checks so that the two cannot drift apart; their sentences are lint's, after the topic's label.
    A topic that is not approved is not judged for its words, and an approved topic is not compared
    with it. Topics are named by id (LABEL), characters by code point (a banned one by its name in
    rules/form.toml) and banned phrases by their number in banned_phrases (_phrase_numbers), so that no
    sentence prints what a topic or a phrase holds, the manuscript marks aside: a middle dot, an em dash
    or a colon in them stays out of the sentences. The L070 and L050 sentences are lint's own and name
    the markdown kind or the form marker, which come from tools/lint.py and rules/form.toml rather than
    from the topic. A colon_allowed that does not compile is one sentence (COLON_RULE_UNREADABLE) at the
    head instead of a traceback, and colons are then not judged. No approved topics, no sentences.
    """
    about_ids = id_problems(found.topics)
    if about_ids:
        return about_ids
    approved = [topic for topic in found.topics if topic.approved]
    if not approved:
        return []
    form = getattr(rules, "form", None) or {}
    punct = form.get("punctuation", {})
    banned = punct.get("banned", {})
    sentences: list[str] = []
    try:
        colon_allowed = re.compile(punct.get("colon_allowed", COLON_ALLOWED))
    except (re.error, TypeError):
        colon_allowed = None
        sentences.append(COLON_RULE_UNREADABLE)
    phrases = _phrase_numbers(banned_phrases)
    shape = _shape_problems(approved)
    markers = tuple((form.get("manuscript", {}) or {}).get("meta_markers", META_MARKERS))
    for topic in approved:
        label = LABEL.format(id=topic.id)
        if topic.id in shape:
            sentences.append(shape[topic.id])
        sentences.extend(_character_problems(label, topic.topic, banned, form))
        if colon_allowed is not None and _stray_colon(topic.topic, colon_allowed):
            sentences.append(f"{label}에 숫자 사이가 아닌 콜론이 있습니다.")
        words = unicodedata.normalize("NFC", topic.topic)
        sentences.extend(f"{label}이 브랜드 금지 문자열(banned_phrases) {place}번에 걸렸습니다."
                         for phrase, place in phrases.items() if phrase in words)
        flagged: list = []
        unit = Unit(label, words)
        _check_markdown(unit, flagged)
        _check_meta(unit, flagged, markers)
        sentences.extend(f"{label}의 글이 린터 {issue.code}에 걸립니다. {issue.message}" for issue in flagged)
    return sentences


def keyword_notes(found: TopicFile) -> list[str]:
    """One sentence for each keyword two or more topics aim at, approved or not. These stop nothing.

    Keywords are compared in NFC and without the whitespace around them, and a keyword that is empty
    then is none. The sentences come in the order the keywords first stand among the topics counted, and each names
    its topics by id in file order without printing the keyword. A topic whose id cannot be used
    (usable_id) is left out of the count: an id that does not match ID may hold anything, and an id is
    printed as written. So the sentences are safe to show whether or not problems was asked about the
    ids first.
    """
    aimed: dict[str, list[str]] = {}
    for topic in found.topics:
        keyword = unicodedata.normalize("NFC", topic.keyword).strip()
        if keyword and usable_id(topic.id):
            aimed.setdefault(keyword, []).append(topic.id)
    return [SAME_KEYWORD.format(ids=", ".join(f"{topic_id}번" for topic_id in ids))
            for ids in aimed.values() if len(ids) > 1]
