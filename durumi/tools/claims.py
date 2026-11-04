"""Topic claims: which run holds which topic of brands/{id}/topics.json, and what became of it.

A claim is a file, runs_root/_topics/{brand id}/{topic id}.{attempt}.json, created with open(path, "xb"):
the OS lets only one process create a path, so runs that share one runs folder never hold the same
attempt of a topic. Claim files are never removed or rewritten. A topic's state is read, not stored:
no claim is LEFT; the last attempt's run decides the rest by the exit code in its meta.json (none yet
RESERVED, 0 WRITTEN, another FAILED). A file {topic id}.{attempt}.free.json, made by release, turns a
reserved attempt into FAILED. An exit code the run leaves afterwards still decides, so a run set free
that ends with 0 after all is WRITTEN. release frees a claim younger than RUN_MAY_LIVE, or one whose time
cannot be read, only when told its run is dead (confirmed_dead). A claim whose record cannot be read
counts as RESERVED, so the topic is not handed out twice.

What is not promised:
- Under reuse, more runs at once than there are approved topics repeat topics. While several of them
  are between creating their claim files and writing the records, one topic's third attempt may go out
  before another topic's second.
- Each runs folder keeps its own claims. Another machine, a git worktree or ~/.durumi/runs may hand
  out a topic this folder has handed out.
- A claim whose record cannot be read (a run died between creating its claim file and writing it)
  stays RESERVED until a person releases it, and under reuse it is tried last.
- Runs started together may read the same records and write one seq.
- A write that fails after the claim file was created leaves an empty claim. Its run cannot be read
  from it, so the topic stays RESERVED whatever exit code that run leaves.
- Once a run folder is removed, what became of the run cannot be read: a topic it wrote reads as
  RESERVED, and as FAILED, going out again, if that attempt had been set free.
- The state is the last attempt's alone. When an attempt set free ends with 0 after the next attempt
  went out and that one fails, the topic goes out again.
- RUN_MAY_LIVE is a generous guess, not a bound: a run that repairs its post calls a node again, each
  call up to an hour, and may still be writing after it; released then, its topic is written twice.

Importing this module has no side effects.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from config import runs_root
from naming import BRAND_ID, RUN_NAME
from topics import ID, REUSE, SAME_ID, Topic, TopicFile, sentence_key, usable_id

TOPICS_DIR = "_topics"  # under runs_root, beside the run folders; one folder per brand
LEFT, RESERVED, WRITTEN, FAILED = "left", "reserved", "written", "failed"
NAMES = {LEFT: "남음", RESERVED: "예약됨", WRITTEN: "작성됨", FAILED: "실패"}  # the states as a person is told them
CLAIM_NAME = re.compile(rf"({ID.pattern})\.([1-9][0-9]*)\.json")
FREE_NAME = re.compile(rf"({ID.pattern})\.([1-9][0-9]*)\.free\.json")
CARDS_DIR = "_cards"  # the claims folder of tools/cards.py, which this module replaced
CARD_CLAIM_NAME = re.compile(r"g([1-9][0-9]*)-([0-9a-f]{16})\.json")
TRIES = 1000  # listings one assign makes before it gives up, so that it always ends
RUN_MAY_LIVE = timedelta(hours=3)  # two node calls of up to 3600 s each (nodes/*.toml) and slack
# The sentences of the ValueError about a topic id. An id that is no topics.usable_id may hold anything,
# so none of them prints it; n counts the topics of the file from 1 (topics.SAME_ID names two places).
NO_CLAIM_FILE = "주제 파일의 {n}번째 id로는 예약 파일을 만들 수 없습니다."  # assign, import_cards
NO_SUCH_ID = "풀려는 주제의 id가 주제 id로 쓸 수 없는 값입니다."  # release


class UsedUp(Exception):
    """There is no topic to give: no approved topic is left or failed and the topic file does not say reuse,
    or the file has no approved topic at all, whatever it says."""


class MayBeRunning(ValueError):
    """The reservation is younger than RUN_MAY_LIVE, or its time cannot be read (release). Its message is the id."""


@dataclass(frozen=True)
class Entry:
    id: str
    state: str
    attempt: int  # the last attempt, 0 when the topic has no claim
    run: str | None  # the run of the last attempt as its record names it, which may be no run folder's name
    seq: int | None  # the last attempt's seq; None without a claim or when its record cannot be read


@dataclass(frozen=True)
class Claim:
    id: str
    topic: str
    attempt: int  # from 1
    times: int  # 1 + the attempts before this one that ended WRITTEN; above 1 the topic is being reused
    remaining: int  # approved topics LEFT or FAILED right after this claim (a moment's count)
    path: Path  # the claim file


@dataclass(frozen=True)
class Imported:
    made: int                 # claims made by this call
    there: int                # claims an earlier import had made (the same card claim file)
    lost: tuple[str, ...]     # card claim files no topic matches or whose record cannot be read
    blocked: tuple[str, ...]  # card claim files of a topic that already has a claim no import made


@dataclass(frozen=True)
class _Move:
    """A card claim file that matches a topic and can be read, and what an import would do with it (_plan)."""

    name: str  # the card claim file
    topic: Topic
    attempt: int  # the card claim's generation, which becomes the attempt
    record: dict  # the card claim's record
    verdict: str  # MAKE, THERE or BLOCKED


MAKE, THERE, BLOCKED = "make", "there", "blocked"  # _Move.verdict


def claims_dir(root: Path | str, brand_id: str) -> Path:
    """Where a brand's claims go: runs_root(root)/_topics/{brand id}."""
    if not BRAND_ID.fullmatch(brand_id or ""):
        raise ValueError(f"브랜드 id에 쓸 수 없는 문자가 있습니다. {brand_id!r}")
    return runs_root(root) / TOPICS_DIR / brand_id


def _listing(folder: Path) -> tuple[dict[str, int], set[tuple[str, int]], int]:
    """(each topic id's last attempt, the (id, attempt) pairs set free, how many claim files there are) from one
    listing; a missing folder has none. Free files are no claim files."""
    last: dict[str, int] = {}
    freed: set[tuple[str, int]] = set()
    count = 0
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return last, freed, count
    for name in names:
        if match := FREE_NAME.fullmatch(name):
            freed.add((match.group(1), int(match.group(2))))
        elif match := CLAIM_NAME.fullmatch(name):
            last[match.group(1)] = max(last.get(match.group(1), 0), int(match.group(2)))
            count += 1
    return last, freed, count


def _record(path: Path) -> dict | None:
    """A JSON object read from path; None when it cannot be read as one (gone, empty, cut short, not an object)."""
    try:
        record = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def _whole(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _result(root: Path | str, run: object) -> str:
    """What became of a run: WRITTEN for exit code 0, FAILED for another, RESERVED without one or without its record."""
    if not isinstance(run, str) or not RUN_NAME.fullmatch(run):
        return RESERVED
    meta = _record(runs_root(root) / run / "meta.json")
    code = _whole(meta.get("exit_code")) if meta else None
    if code is None:
        return RESERVED
    return WRITTEN if code == 0 else FAILED


def _attempt(root: Path | str, folder: Path, topic_id: str, attempt: int, freed: set[tuple[str, int]]) -> Entry:
    if attempt == 0:
        return Entry(topic_id, LEFT, 0, None, None)
    record = _record(folder / f"{topic_id}.{attempt}.json")
    run = record.get("run") if record else None
    seq = _whole(record.get("seq")) if record else None
    state = RESERVED if record is None else _result(root, run)
    if state == RESERVED and (topic_id, attempt) in freed:
        state = FAILED  # set free by release; an exit code the run leaves later still decides
    return Entry(topic_id, state, attempt, run if isinstance(run, str) else None, seq)


def _entries(root: Path | str, brand_id: str, ids: Sequence[str]) -> tuple[dict[str, Entry], int]:
    """(entries, how many claim files the folder holds of any topic id), from one listing of the claims folder."""
    folder = claims_dir(root, brand_id)
    last, freed, count = _listing(folder)
    return {topic_id: _attempt(root, folder, topic_id, last.get(topic_id, 0), freed) for topic_id in ids}, count


def entries(root: Path | str, brand_id: str, ids: Sequence[str]) -> dict[str, Entry]:
    """Each topic id's Entry by its last attempt, from one listing of the claims folder. Only reads."""
    return _entries(root, brand_id, ids)[0]


def attempts(root: Path | str, brand_id: str, topic_id: str) -> list[Entry]:
    """Every attempt of one topic, the first first, each as entries would read it were it the last. Only reads.

    One listing of the claims folder gives the topic's claim files and the free files. Each attempt's
    state follows the rule of the last one (_attempt): the exit code its run left decides, a free file
    turns an attempt without a result into FAILED, and a claim whose record cannot be read is RESERVED.
    Only attempts that have a claim file come back, so a number may be missing between two of them. The
    topic record (tools/ledger.py) is collected from this.

    An empty list for a topic without a claim, for a missing claims folder and, before anything is read,
    for a topic_id that is no topics.usable_id. ValueError for a brand id that cannot name a claims folder
    (claims_dir), once the topic id has passed.
    """
    if not usable_id(topic_id):
        return []
    folder = claims_dir(root, brand_id)
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    numbers: list[int] = []
    freed: set[tuple[str, int]] = set()
    for name in names:
        if match := FREE_NAME.fullmatch(name):
            freed.add((match.group(1), int(match.group(2))))
        elif (match := CLAIM_NAME.fullmatch(name)) and match.group(1) == topic_id:
            numbers.append(int(match.group(2)))
    return [_attempt(root, folder, topic_id, number, freed) for number in sorted(numbers)]


def claimed_ids(root: Path | str, brand_id: str) -> list[str]:
    """The topic ids that have a claim file in the brand's claims folder, each once, in order. Only reads.

    They are the ids the listing takes for claims, whether or not a topic file still lists them: the topics
    command says how many it does not list, and the topic record holds their posts under the sentences their
    claims recorded (recorded_topic). A free file without its claim gives no id. An empty list for a missing
    folder; ValueError for a brand id that cannot name a claims folder (claims_dir).
    """
    folder = claims_dir(root, brand_id)
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    return sorted({match.group(1) for name in names if (match := CLAIM_NAME.fullmatch(name))})


def recorded_topic(root: Path | str, brand_id: str, topic_id: str) -> str | None:
    """The topic sentence the latest claim of topic_id whose record can be read was made with ("topic"). Only reads.

    Claims record the words their topic had when they were made, so a topic taken out of the topic file, or
    renamed, is still named by them (tools/ledger.py). The claims go by attempt number, the highest first; one
    whose record cannot be read is passed over. None when no record can be read, when that record's topic is
    missing or no string, and, before anything is read, for a topic_id that is no topics.usable_id. ValueError
    for a brand id that cannot name a claims folder (claims_dir), once the topic id has passed.
    """
    if not usable_id(topic_id):
        return None
    folder = claims_dir(root, brand_id)
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return None
    numbers = sorted((int(match.group(2)) for name in names
                      if (match := CLAIM_NAME.fullmatch(name)) and match.group(1) == topic_id), reverse=True)
    for number in numbers:
        record = _record(folder / f"{topic_id}.{number}.json")
        if record is not None:
            words = record.get("topic")
            return words if isinstance(words, str) else None
    return None


def _order(found: dict[str, Entry], listed: Sequence[Topic], reuse: bool) -> list[Topic]:
    """The topics to try: those LEFT or FAILED as the file lists them; when none and the file says reuse,
    every topic, the one whose last attempt is oldest (lowest seq) first, an unread seq last."""
    free = [topic for topic in listed if found[topic.id].state in (LEFT, FAILED)]
    if free or not reuse:
        return free
    place = {topic.id: index for index, topic in enumerate(listed)}

    def age(topic: Topic) -> tuple[bool, int, int]:
        seq = found[topic.id].seq
        return seq is None, 0 if seq is None else seq, place[topic.id]

    return sorted(listed, key=age)


def _check_ids(topic_file: TopicFile) -> None:
    """ValueError for the first topic, approved or not, whose id cannot name a claim file or is an earlier topic's.

    An id is part of the claim file's name. One that is no topics.usable_id could put the file outside
    the claims folder ("../x", an absolute path), give it a name the listing does not take for a claim
    (a capital letter) or name a device of Windows, and two topics with one id would share their claims.
    topics.problems tells a person as much; this is the module's own guard, for a caller that did not
    ask it. The sentence names the topic by its place, from 1, and does not print the id.
    """
    first: dict[str, int] = {}
    for place, topic in enumerate(topic_file.topics, start=1):
        if not usable_id(topic.id):
            raise ValueError(NO_CLAIM_FILE.format(n=place))
        if topic.id in first:
            raise ValueError(SAME_ID.format(n=place, m=first[topic.id]))
        first[topic.id] = place


def assign(root: Path | str, brand_id: str, topic_file: TopicFile, run_id: str, *, now: datetime) -> Claim:
    """Claim one approved topic for run_id and return it; UsedUp when there is none to give.

    ValueError, before anything is made, for a topic file with an id that cannot name a claim file or
    that two topics share (_check_ids), approved or not. The folder is listed, the topics are tried in
    _order, each by creating the claim file of its next attempt with open(path, "xb"). FileExistsError
    means another run took that attempt meanwhile, so the next topic is tried; when every one was taken
    the folder is listed again, up to TRIES times (then RuntimeError). Any other OSError is raised.

    The claim's seq is one more than the greater of the highest seq read of the approved topics' last
    attempts and the number of claim files in the folder, from the same listing. The count keeps a seq
    above those of the claims made before it, when the topics that held the highest are no longer approved
    or their records cannot be read, so that under reuse the order of the oldest does not go back.
    """
    _check_ids(topic_file)
    listed = [topic for topic in topic_file.topics if topic.approved]
    ids = [topic.id for topic in listed]
    reuse = topic_file.when_used_up == REUSE
    folder = claims_dir(root, brand_id)
    if listed:
        folder.mkdir(parents=True, exist_ok=True)
    for _ in range(TRIES):
        found, claimed = _entries(root, brand_id, ids)
        order = _order(found, listed, reuse)
        if not order:
            raise UsedUp(brand_id)
        seq = max(max((entry.seq for entry in found.values() if entry.seq is not None), default=0), claimed) + 1
        for topic in order:
            attempt = found[topic.id].attempt + 1
            path = folder / f"{topic.id}.{attempt}.json"
            record = {"run": run_id, "claimed_at": now.isoformat(timespec="seconds"), "id": topic.id,
                      "topic": topic.topic, "attempt": attempt, "seq": seq}
            data = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            try:
                handle = open(path, "xb")
            except FileExistsError:
                continue  # another run claimed this attempt after the listing
            with handle:
                handle.write(data)
            after = entries(root, brand_id, ids)
            remaining = sum(1 for entry in after.values() if entry.state in (LEFT, FAILED))
            # A free file never makes an attempt WRITTEN, so the earlier attempts are read without any.
            times = 1 + sum(1 for earlier in range(1, attempt)
                            if _attempt(root, folder, topic.id, earlier, set()).state == WRITTEN)
            return Claim(id=topic.id, topic=topic.topic, attempt=attempt, times=times, remaining=remaining, path=path)
    raise RuntimeError(f"예약 폴더를 {TRIES}번 읽는 동안 고른 주제를 매번 다른 실행이 먼저 가져갔습니다. {folder}")


def _claimed_at(record: dict | None) -> datetime | None:
    """When a claim's record says it was made, with its offset; None for a record that says no such time."""
    value = record.get("claimed_at") if record is not None else None
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.utcoffset() is not None else None  # a time without an offset cannot be set beside now


def release(root: Path | str, brand_id: str, topic_id: str, *, now: datetime, confirmed_dead: bool = False) -> Path:
    """Turn a RESERVED topic's last attempt into FAILED by creating its free file; now is a time with its offset.

    ValueError for a topic in any other state, its sentence naming the state as NAMES does, and, before
    anything is read, for a topic_id that is no topics.usable_id, which the sentence does not print.

    Then, unless confirmed_dead, MayBeRunning (a ValueError) and nothing made when the last attempt's claim
    was made less than RUN_MAY_LIVE before now, made later than now, or says no time that can be read
    (_claimed_at): its run may still be writing, and freed, the topic would be written twice. confirmed_dead
    is for a person who saw the run die. RUN_MAY_LIVE is a generous guess, not a bound: a run that repairs
    its post calls a node again, each call up to an hour, and may live past it.
    """
    if not usable_id(topic_id):
        raise ValueError(NO_SUCH_ID)
    entry = entries(root, brand_id, [topic_id])[topic_id]
    if entry.state != RESERVED:
        raise ValueError(f"주제 {topic_id}번은 예약됨이 아니라서 풀 수 없습니다(지금 {NAMES[entry.state]}).")
    folder = claims_dir(root, brand_id)
    if not confirmed_dead:
        when = _claimed_at(_record(folder / f"{topic_id}.{entry.attempt}.json"))
        if when is None or now - when < RUN_MAY_LIVE:  # made later than now, the difference is below 0
            raise MayBeRunning(topic_id)
    path = folder / f"{topic_id}.{entry.attempt}.free.json"
    data = (json.dumps({"freed_at": now.isoformat(timespec="seconds")}, indent=2) + "\n").encode("utf-8")
    with open(path, "xb") as handle:
        handle.write(data)
    return path


def counts(root: Path | str, brand_id: str, topic_file: TopicFile) -> dict[str, int]:
    """How many approved topics are in each state (keys LEFT, RESERVED, WRITTEN, FAILED) and how many topics
    are not approved (key "unapproved"). Only reads."""
    listed = [topic.id for topic in topic_file.topics if topic.approved]
    tally = {state: 0 for state in (LEFT, RESERVED, WRITTEN, FAILED)}
    for entry in entries(root, brand_id, listed).values():
        tally[entry.state] += 1
    tally["unapproved"] = len(topic_file.topics) - len(listed)
    return tally


def _imported_from(record: dict | None) -> str | None:
    """The card claim file an import made this claim from, as its record names it; None for a claim no import made.

    That is a record that cannot be read, one without imported_from, and one whose imported_from is not the
    name of a card claim file (CARD_CLAIM_NAME), which no import writes.
    """
    source = record.get("imported_from") if record is not None else None
    return source if isinstance(source, str) and CARD_CLAIM_NAME.fullmatch(source) else None


def _plan(root: Path | str, brand_id: str, topic_file: TopicFile) -> tuple[list[_Move], list[str]]:
    """What import_cards would do now with each card claim of the brand: (the moves, the names of the lost). Only reads.

    A card claim file g{generation}-{key}.json belongs to the topic whose sentence has the key
    (topics.sentence_key); when topics share a sentence, to an approved one before one that is not, and of
    those alike to the one the file lists first. One that no topic matches, or whose record cannot be read,
    is lost. The others become moves, by topic and then by generation, each with a verdict:
    - BLOCKED, every card claim of a topic that holds a claim no import made (_imported_from): the topic's
      state is that claim's to decide, and a copy could become its last attempt;
    - THERE, when the claim {topic id}.{generation}.json is a copy of this very card claim;
    - BLOCKED, when that claim is there and is a copy of another card claim;
    - MAKE otherwise.
    ValueError, before anything is read, for a topic file assign would refuse (_check_ids) and for a brand
    id that cannot name a claims folder (claims_dir). The claims folder is listed only when a card claim
    matches a topic, so that a brand without card claims reads nothing of it.
    """
    _check_ids(topic_file)
    folder = claims_dir(root, brand_id)
    source = runs_root(root) / CARDS_DIR / brand_id
    try:
        names = sorted(os.listdir(source))
    except FileNotFoundError:
        return [], []
    by_key: dict[str, Topic] = {}
    for topic in sorted(topic_file.topics, key=lambda topic: not topic.approved):  # approved first, each in file order
        by_key.setdefault(sentence_key(topic.topic), topic)
    lost: list[str] = []
    found: dict[str, list[tuple[int, str, dict]]] = {}
    for name in names:
        match = CARD_CLAIM_NAME.fullmatch(name)
        if not match:
            continue
        topic = by_key.get(match.group(2))
        old = _record(source / name)
        if topic is None or old is None:
            lost.append(name)
            continue
        found.setdefault(topic.id, []).append((int(match.group(1)), name, old))
    if not found:
        return [], lost
    try:
        present = os.listdir(folder)
    except FileNotFoundError:
        present = []
    held: dict[str, dict[int, str | None]] = {}  # each topic's claims: attempt to the card claim it was copied from
    for name in present:
        if (match := CLAIM_NAME.fullmatch(name)) and match.group(1) in found:
            held.setdefault(match.group(1), {})[int(match.group(2))] = _imported_from(_record(folder / name))
    topic_of = {topic.id: topic for topic in topic_file.topics}
    moves: list[_Move] = []
    for topic_id, cards in found.items():
        claimed = held.get(topic_id, {})
        own = any(source_name is None for source_name in claimed.values())  # a claim no import made
        for attempt, name, old in sorted(cards, key=lambda card: card[:2]):
            if own or (attempt in claimed and claimed[attempt] != name):
                verdict = BLOCKED
            else:
                verdict = THERE if attempt in claimed else MAKE
            moves.append(_Move(name=name, topic=topic_of[topic_id], attempt=attempt, record=old, verdict=verdict))
    return moves, lost


def pending_cards(root: Path | str, brand_id: str, topic_file: TopicFile) -> int:
    """How many claims import_cards would make if called now (_plan's MAKE): the card claims still waiting. Only reads.

    The lost and the blocked are not counted: no import ever makes a claim of them. 0 without the old
    folder. ValueError as import_cards raises it, before anything is read.
    """
    moves, _ = _plan(root, brand_id, topic_file)
    return sum(1 for move in moves if move.verdict == MAKE)


def import_cards(root: Path | str, brand_id: str, topic_file: TopicFile) -> Imported:
    """Copy the card claims tools/cards.py left (runs_root/_cards/{brand id}/g{generation}-{key}.json) into topic claims.

    Each move of _plan whose verdict is MAKE becomes the claim {topic id}.{generation}.json, created with
    open(path, "xb") as assign creates one, with the run, the time and the seq the card claim recorded,
    the id and the words of the topic file, and imported_from, the card claim's file name. The moves of a
    topic go by generation. When the place is taken by the time the claim is created, the claim there is
    read again: a copy of this card claim counts as there, and any other claim blocks this card claim; a
    claim no import made blocks the topic's later moves too, as _plan would have. What comes back
    (Imported) counts the claims made and those there, and names the card claim files lost and blocked.
    The card claims are left as they are. ValueError, before anything is made, as _plan raises it.

    A topic that holds a claim no import made keeps the state its own claims give: its card claims are
    blocked, so no import turns a topic the claims have written into one going out again. Not promised:
    a run that claims an attempt below the ones an import is creating, between the import's reading and
    its writing, is not seen, and the copy then becomes the topic's last attempt. A run claims only after
    pending_cards found no card claim of its own topic file waiting (pipeline.check, pipeline.run), so this
    takes a topic file changed while the run goes, to match card claims it did not match, and an import
    made from it at that moment.
    """
    moves, lost = _plan(root, brand_id, topic_file)
    folder = claims_dir(root, brand_id)
    made, there = 0, 0
    blocked: list[str] = []
    stopped: set[str] = set()  # topics found holding a claim no import made when a copy was created
    for move in moves:
        if move.verdict == THERE:
            there += 1
            continue
        if move.verdict == BLOCKED or move.topic.id in stopped:
            blocked.append(move.name)
            continue
        path = folder / f"{move.topic.id}.{move.attempt}.json"
        record = {"run": move.record.get("run"), "claimed_at": move.record.get("claimed_at"), "id": move.topic.id,
                  "topic": move.topic.topic, "attempt": move.attempt, "seq": _whole(move.record.get("seq")),
                  "imported_from": move.name}
        data = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        folder.mkdir(parents=True, exist_ok=True)
        try:
            handle = open(path, "xb")
        except FileExistsError:  # taken since _plan read the folder
            source = _imported_from(_record(path))
            if source == move.name:
                there += 1
                continue
            blocked.append(move.name)
            if source is None:
                stopped.add(move.topic.id)
            continue
        with handle:
            handle.write(data)
        made += 1
    return Imported(made=made, there=there, lost=tuple(sorted(lost)), blocked=tuple(sorted(blocked)))
