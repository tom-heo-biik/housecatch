"""Topic record: brands/{id}/topics-done.json, what became of a brand's topics and of its runs outside the list.

The team reads the record through git, beside the posts. The topics command (tools/durumi.py) exports it from
this machine's claims (tools/claims.py) and run folders, and shows the brand's topics from the same reading
(table). Its shape is

    {"topics": [{"id": "t001", "topic": "...", "state": "written",
                 "attempts": [{"attempt": 1, "run": "20261101-090000-alpha", "date": "2026-11-01",
                               "result": "written", "file": "2026-11-01-0900-....html", "found": true}]}],
     "off_list": [{"run": "...", "date": "...", "kind": "given", "topic": "...", "result": "written",
                   "file": "...", "found": true}]}

- topics holds the topics of topics.json that have a finished attempt, with the sentence the file gives,
  then by id the topic ids that hold claims and are not in topics.json (taken out of it, renamed, or the
  file gone), with the sentence their claims recorded, cut as a run outside the list cuts its request.
  An attempt is written when its run left exit code 0, set free or not, and failed when the run left
  another code or was set free without one, as tools/claims.py reads its state. An attempt still reserved
  is left out until it ends. state is the result of the topic's last attempt.
- run is the run folder's name, and date the day the run started, which the name holds. file is the
  name of the post the run's report.json delivered, and found says whether a file of that name is
  anywhere under the brand's blogs folder, where a person may have moved it. A failed attempt has no file.
- off_list holds the brand's runs that ended with exit code 0 without a topic of the list: the requests
  that gave their topic, and those that left it to the node of a brand without topics.json. kind is the
  run's topic_kind, and topic the first line of its request cut to OFF_LIST_CHARS, for some requests
  carry a whole manuscript. Such a run comes into the record only at an export that finds its post under
  the brand's blogs folder: one whose post was deleted, shown in the session or never saved has no post
  to read and gets no entry. Once in, its entry stays as every entry does, also when its post is deleted
  later, and what a later export reads of the same run is merged into it whatever that export found.

collect reads this machine; export passes over the runs outside the list that are not to come in
(_admitted), and merge puts the rest into the record the file holds. No entry is ever removed, so a run
the record holds stays in it after its folder is removed. An attempt is known by its number and its run and
a run outside the list by its run, so the newer word on one replaces the older, unless the older knows more
of the run: that it was written, where the newer reads it as failed (its folder gone after it was set free),
or the post's name, where the newer has none (its report.json gone). Two written entries of one run name
whose posts have different names are two runs, as runs two machines started in one second are, and both
stay. Keys the tool does not write, which a person or the publishing step put on an entry, stay on it. read
refuses a file it cannot take whole (LedgerError) and export then writes nothing over it; entries that
repeat in the file, as when a person put the entries of two records together by hand, are joined and not
refused. write puts the new record in place by renaming a temporary file of its own (deploy.replace_text),
so a write cut off leaves the old record rather than half of a new one. export reads the file again right
before it writes, and starts over when another export has changed it meanwhile.

What is not promised:
- The record holds what this machine's claims and runs folder showed at each export, less the runs
  outside the list passed over for want of a post, and what the file held before. A run whose folder was
  removed before any export never reaches it. A claim file whose id cannot name one (topics.usable_id),
  which only a hand makes, is not read.
- A run outside the list is known to have a post by the name its report.json gives it, in any folder
  under blogs. While no file of that name is there (the post deleted, or renamed by hand) or the report
  is gone, a run the record does not hold yet is passed over, though its post may be there under another
  name; it comes in at the first export that finds the name.
- Two machines whose runs of one brand started in the same second give them one name. When both saved
  posts of different names the record keeps both, each from an export that found its post; otherwise it
  takes the two for one run and keeps the word that knows more of it, or of two that know as much the
  one merged last. An entry of the name that names no post, which only a hand writes now, is taken for
  either run: with one in the record a run of that name comes in without its post, and which entries
  then stand depends on which machine exported first.
- A topic's sentence is the one topics.json gave the latest export that had the topic, or for an id it
  no longer lists, the one its latest claim recorded. The state is that of the last attempt by number
  and run name, which across machines need not be the latest in time.
- found is as of the export that wrote the entry.
- Exports into one brand folder at once are not locked. Each reads the file again right before it writes
  and starts over when it changed, up to EXPORT_TRIES times (then KeptChanging, and nothing is written).
  An entry another export writes between that last reading and the rename is still replaced; the next
  export writes it again while its run folder is there (and, for a run outside the list, its post).
- An attempt whose claim cannot be read or names no run folder has run "" and date "", and two such
  attempts of one topic and number count as one.

Importing this module has no side effects.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path

import claims
import topics
from config import Brand, runs_root
from deploy import replace_text
from naming import RUN_NAME
from pipeline import LEGACY_CARDS
from resolve import TOPIC_GIVEN, TOPIC_HANDED_OVER

DONE_FILE = "topics-done.json"  # in the brand folder, beside topics.json
EXPORT_TRIES = 5  # readings of the record one export makes while other exports keep changing it (export)
OFF_LIST_CHARS = 200  # characters kept of a request's first line, for a run outside the list
RESULTS = (claims.WRITTEN, claims.FAILED)  # what an attempt in the record ended as
KINDS = (TOPIC_GIVEN, TOPIC_HANDED_OVER)  # the topic_kind of a run that reached its folder; "" for any other
TOPIC_KEYS = ("id", "topic", "state", "attempts")  # a topic's keys, in the order the record writes them
# The keys the tool writes of an attempt and of a run outside the list. Any other key on one is its own: a
# person's, or the publishing step's. merge carries those to the entry that stands for the run (_standing).
ATTEMPT_KEYS = ("attempt", "run", "date", "result", "file", "found")
OFF_LIST_KEYS = ("run", "date", "kind", "topic", "result", "file", "found")
# A post's name as pipeline.save_to_brand gives it (naming.blog_name, final_name, a -2 for a taken name). It
# is printed and written as read from report.json, so another name is taken for none.
POST_NAME = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*\.html")
NO_RUN_NAME = "실행 이름을 읽지 못함"  # table: in place of a run name the claim does not hold
NO_RUN_RECORD = "실행 기록 없음"  # table: no meta.json of the run can be read
NOT_APPROVED = "승인 전"  # table: a topic the file lists and does not approve
# table, after the topics' lines, when a topic of the file is reserved, approved or not: how one is set free.
# check sends a session here (pipeline.CHECK_RESERVED) and the skill does not say how; a release is for a
# person to decide, so the table says it. {주제 id} is printed as it stands, braces and all, as the place for
# an id: the line names no topic and is never formatted.
HOW_TO_RELEASE = "예약됨인 주제가 쓰는 중인 글이 아니라면 사람이 확인한 뒤 같은 명령에 --release {주제 id}를 붙여 풉니다."
# table, right after the counts, while n claims the topic cards left wait to be imported (claims.pending_cards):
# check and run stop the brand's requests that hand the topic over until they are (pipeline.CARDS_PENDING), and
# the table would look as if nothing were the matter.
CARDS_WAITING = "아직 옮기지 않은 옛 주제 카드 예약이 {n}건 있습니다. 같은 명령에 --import-cards를 붙여 옮겨 주십시오."
# unlisted: n topic ids that hold claims are not in the topic file. The ids are counted, not their claims, and
# none is printed. An export writes their finished attempts under the sentence the claims recorded (collect).
UNLISTED = "예약은 있는데 주제 파일에 없는 주제 id가 {n}개 있습니다. 이 id들의 글은 예약할 때의 문장으로 주제 기록에 적습니다."

# The sentences of LedgerError, each naming the file. n counts the items of a list from 1, line and column
# count from 1 as json.JSONDecodeError does, and no sentence prints what the file holds.
NOT_OPENED = "topics-done.json을 열지 못했습니다."  # a folder in its place, no permission
NOT_JSON = "topics-done.json이 UTF-8 JSON이 아닙니다."
NOT_JSON_AT = "topics-done.json의 JSON 문법이 맞지 않습니다. {line}번째 줄 {column}번째 칸 근처를 봐 주십시오."
SAME_KEY = "topics-done.json에 같은 항목 이름을 두 번 적은 객체가 있습니다."
NOT_OBJECT = "topics-done.json의 맨 위가 객체가 아닙니다."
NOT_LIST = "topics-done.json의 {key}가 목록이 아닙니다."  # key: topics, off_list; also when the key is missing
BAD_ITEM = "topics-done.json의 {key} {n}번째 항목이 주제 기록의 모양이 아닙니다."
# KeptChanging: other exports changed the file each time this one was about to write it. The file was read
# each time, so the sentence does not say it could not be.
KEPT_CHANGING = "주제 기록이 내보내는 동안 계속 바뀌어 쓰지 않았습니다. 다시 돌려 주십시오."


class LedgerError(Exception):
    """topics-done.json is there and cannot be read as a record. The message is one sentence for a person."""


class KeptChanging(LedgerError):
    """Another export changed topics-done.json each of the EXPORT_TRIES times export was about to write it."""


class _KeyTwice(ValueError):
    """A JSON object of the file writes one key more than once (_pairs)."""


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    """json's object_pairs_hook: the object as a dict, and _KeyTwice when a key is written more than once.

    json by itself keeps the last value of such a key, which a rewrite would then make the only one.
    """
    read = dict(pairs)
    if len(read) < len(pairs):
        raise _KeyTwice
    return read


def _whole(value: object) -> bool:
    """True for a whole number that is no bool, as an attempt number and an exit code are written."""
    return isinstance(value, int) and not isinstance(value, bool)


def _key(attempt: dict) -> tuple[int, str]:
    """What tells one attempt of a topic from another: its number, then its run."""
    return attempt["attempt"], attempt["run"]


def _topic_shaped(item: object) -> bool:
    """True for a topic merge can take: id, topic and attempts, each attempt with a number, a run, a result."""
    if not (isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("topic"), str)
            and isinstance(item.get("attempts"), list)):
        return False
    return all(isinstance(one, dict) and _whole(one.get("attempt")) and isinstance(one.get("run"), str)
               and one.get("result") in RESULTS for one in item["attempts"])


def _off_shaped(item: object) -> bool:
    return isinstance(item, dict) and isinstance(item.get("run"), str)


def _all_shaped(items: list, key: str, shaped: Callable[[object], bool]) -> None:
    """LedgerError naming the place, from 1, of the first item of items that is not shaped."""
    for place, item in enumerate(items, start=1):
        if not shaped(item):
            raise LedgerError(BAD_ITEM.format(key=key, n=place))


def read(brand_dir: Path) -> dict | None:
    """The record in brand_dir/topics-done.json as merge holds one; None when there is no such file. Only reads.

    LedgerError, with one of the sentences above, for a file that is there and is not a record merge can
    take whole: one that cannot be opened; bytes that are not UTF-8 or not JSON (with the line and column
    json names); an object anywhere in it that writes a key twice; a top that is no object; topics or
    off_list that is no list or is missing; a topic without a string id and topic and a list of attempts;
    an attempt without a whole number, a string run and a result of RESULTS; or a run outside the list
    without a string run. Other keys, and values merge does not use, are kept as they are. A UTF-8 byte
    order mark an editor saved is read past.

    Entries that repeat are not refused: a topic id that stands twice, an attempt twice in a topic, a run
    outside the list twice. A person who puts the entries of two records together by hand may write one id
    or one run twice, and the next export must not stop there. They are joined as merge joins two records
    (the attempts of both, and of one run the entry or entries that stand), and the topics keep the order of
    their first place.
    """
    data = _file_bytes(Path(brand_dir) / DONE_FILE)
    return None if data is None else _parsed(data)


def _file_bytes(path: Path) -> bytes | None:
    """The bytes of the record file at path; None without one, LedgerError (NOT_OPENED) for one that cannot be read."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LedgerError(NOT_OPENED) from exc


def _parsed(data: bytes) -> dict:
    """The record data holds, as read gives it; LedgerError with the sentence read names for anything else."""
    try:
        raw = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs)
    except _KeyTwice as exc:
        raise LedgerError(SAME_KEY) from exc
    except json.JSONDecodeError as exc:
        raise LedgerError(NOT_JSON_AT.format(line=exc.lineno, column=exc.colno)) from exc
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError is a ValueError
        raise LedgerError(NOT_JSON) from exc
    if not isinstance(raw, dict):
        raise LedgerError(NOT_OBJECT)
    for key in ("topics", "off_list"):
        if not isinstance(raw.get(key), list):
            raise LedgerError(NOT_LIST.format(key=key))
    _all_shaped(raw["topics"], "topics", _topic_shaped)
    _all_shaped(raw["off_list"], "off_list", _off_shaped)
    return merge(None, raw, list(dict.fromkeys(topic["id"] for topic in raw["topics"])))


def _object(path: Path) -> dict | None:
    """A JSON object read from path; None when it cannot be read as one (gone, cut short, not an object)."""
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _run_name(run: object) -> str:
    """run when it is a run folder's name (naming.RUN_NAME), "" otherwise: no other is joined to a path or shown."""
    return run if isinstance(run, str) and RUN_NAME.fullmatch(run) else ""


def _date(run: str) -> str:
    """'2026-11-01' for the run 20261101-090000-alpha, the day it started; "" for a run that is no run name."""
    return f"{run[0:4]}-{run[4:6]}-{run[6:8]}" if _run_name(run) else ""


def _post_file(root: Path, run: str) -> str:
    """The name of the post the run's report.json delivered into the brand folder; "" for none or another name."""
    report = _object(runs_root(root) / run / "report.json") if _run_name(run) else None
    delivered = report.get("delivered") if report is not None else None
    path = delivered.get("path") if isinstance(delivered, dict) else None
    if not isinstance(path, str):
        return ""  # nothing saved: no delivery, a post shown in the session, or a save that failed
    name = path.rsplit("/", 1)[-1]  # the report holds the path with forward slashes on every platform (as_posix)
    return name if POST_NAME.fullmatch(name) else ""


def _post_names(brand_dir: Path) -> frozenset[str]:
    """The names of every file under the brand's blogs folder, in any folder below it."""
    names: set[str] = set()
    for _, _, files in os.walk(Path(brand_dir) / "blogs"):
        names.update(files)
    return frozenset(names)


def _first_line(text: object) -> str:
    """The first line of a request that shows something, without the whitespace around it, cut to OFF_LIST_CHARS.

    Lines end where str.splitlines ends them. A line of nothing but whitespace, control or invisible characters
    (a zero width space, a byte order mark) is passed over, as topics.blank judges a topic's words. The cut may
    fall after a space, which is then taken off too.
    """
    if not isinstance(text, str):
        return ""
    for line in text.splitlines():
        if not topics.blank(line):
            return line.strip()[:OFF_LIST_CHARS].rstrip()
    return ""


def _finished(root: Path, brand_id: str, topic_id: str, posts: frozenset[str]) -> list[dict]:
    """One topic's finished attempts as the record holds them, the first first.

    claims.attempts reads every attempt of the topic by the claims' own rule, so that the record and the
    states cannot drift apart. An attempt still reserved is passed over: it goes in once it ends.
    """
    done: list[dict] = []
    for entry in claims.attempts(root, brand_id, topic_id):
        if entry.state not in RESULTS:
            continue
        run = _run_name(entry.run)
        file = _post_file(root, run) if entry.state == claims.WRITTEN else ""
        done.append({"attempt": entry.attempt, "run": run, "date": _date(run), "result": entry.state, "file": file,
                     "found": bool(file) and file in posts})
    return done


def _off_list_run(meta: dict, brand_id: str) -> bool:
    """True for a run of the brand that ended with exit code 0 without a topic of the list.

    Such a run has neither assigned_topic, as runs write it now, nor topic_card, as they wrote it before
    topics.json; null or left out alike.
    """
    label, code = meta.get("brand"), meta.get("exit_code")
    return (isinstance(label, dict) and label.get("id") == brand_id and _whole(code) and code == 0
            and meta.get("assigned_topic") is None and meta.get("topic_card") is None)


def _off_list(root: Path, brand_id: str, posts: frozenset[str]) -> list[dict]:
    """The brand's runs outside the list, by run name, from one listing of the runs folder."""
    base = runs_root(root)
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return []
    found: list[dict] = []
    for name in names:
        meta = _object(base / name / "meta.json") if _run_name(name) else None
        if meta is None or not _off_list_run(meta, brand_id):
            continue
        request = meta.get("request") if isinstance(meta.get("request"), dict) else {}
        kind = request.get("topic_kind")
        file = _post_file(root, name)
        found.append({"run": name, "date": _date(name), "kind": kind if kind in KINDS else "",
                      "topic": _first_line(request.get("topic")), "result": claims.WRITTEN, "file": file,
                      "found": bool(file) and file in posts})
    return found


def _check_ids(found: topics.TopicFile) -> None:
    """ValueError, with the first sentence of topics.id_problems, for a topic file whose ids it finds fault with.

    That is an id that cannot name a claim file, or one that two topics share. The record and the table
    write each id as it stands and read the claims by it, so such a file is refused before anything is read,
    as tools/claims.py refuses it for a claim. The topics command asks topics.id_problems first and shows
    every sentence; this is the module's own guard, for another caller.
    """
    problems = topics.id_problems(found.topics)
    if problems:
        raise ValueError(problems[0])


def collect(root: Path, brand: Brand, found: topics.TopicFile | None) -> dict:
    """The record of this machine: the finished attempts of found's topics and the brand's runs outside the list.

    found is the brand's topic file, or None for a brand without one. Every topic of the file is read,
    approved or not: approval is about what goes out next. Then come the topic ids that hold claims and that
    the file does not list (claims.claimed_ids; all of them without a file), those a claim file may be named
    by (topics.usable_id), by id: a topic taken out of the file, or renamed, keeps its posts in the record.
    Such an id is named by the sentence its latest claim that can be read recorded (claims.recorded_topic),
    cut as a request is for a run outside the list (_first_line). A topic comes in only with a finished
    attempt. Topics come in that order, their attempts by number (claims.attempts), the runs outside the list
    by run name. Only reads. ValueError for a topic file _check_ids refuses and for a brand id that cannot
    name a claims folder (claims.claims_dir); OSError when the claims or runs folder cannot be listed.
    """
    root = Path(root)
    if found is not None:
        _check_ids(found)
    posts = _post_names(brand.dir)
    listed = [(topic.id, topic.topic) for topic in found.topics] if found is not None else []
    known = {topic_id for topic_id, _ in listed}
    others = [topic_id for topic_id in claims.claimed_ids(root, brand.id)
              if topic_id not in known and topics.usable_id(topic_id)]
    record = []
    for topic_id, sentence in [*listed, *((topic_id, None) for topic_id in others)]:
        attempts = _finished(root, brand.id, topic_id, posts)
        if not attempts:
            continue
        words = sentence if sentence is not None else _first_line(claims.recorded_topic(root, brand.id, topic_id))
        record.append({"id": topic_id, "topic": words, "state": attempts[-1]["result"], "attempts": attempts})
    return {"topics": record, "off_list": _off_list(root, brand.id, posts)}


def _rank(entry: dict) -> int:
    """How much an entry knows of its run: 2 that it was written and its post's name, 1 that it was written, 0 less."""
    if entry.get("result") != claims.WRITTEN:
        return 0
    return 2 if isinstance(entry.get("file"), str) and entry["file"] else 1


def _standing(old: dict | None, new: dict, known: Sequence[str]) -> dict:
    """Which of two entries of one run stands: new, unless old knows more of the run (_rank).

    A run that left exit code 0 does not come to another end, and the post it saved keeps its name. The run
    only reads as failed again when its folder is removed after it had been set free (tools/claims.py), and
    as written without a name when its report.json is gone; the record then keeps what it knew. Of two that
    know as much, new stands, so that found follows a post deleted or put back.

    The entry that stands also takes the other's keys that are not among known, the tool's own, and that it
    lacks: what a person or the publishing step added to an entry stays through a new export of its run.
    """
    if old is None:
        return new
    stands, other = (old, new) if _rank(old) > _rank(new) else (new, old)
    carried = {key: value for key, value in other.items() if key not in known and key not in stands}
    return {**stands, **carried} if carried else stands


def _post(entry: dict) -> str:
    """The name of the post an entry says its run saved: its file when _rank is 2, "" otherwise."""
    return entry["file"] if _rank(entry) == 2 else ""


def _joined(entries: Sequence[dict], known: Sequence[str]) -> list[dict]:
    """The entries that stand for entries of one run name (one attempt's number and run, or one run outside the
    list), given older first: one, or one for each post's name, in the order of the names.

    Entries that name two or more posts are of as many runs that share the name, as runs two machines started
    in one second do: a run's post keeps its name, so one run is never written twice. Each post's entries are
    joined by _standing, and each entry that stands takes the keys of its own of the entries that name no post
    (_standing again), which know less of whichever run they are of. Otherwise the entries are one run's and
    one stands (_standing). The same entries in another order, merged with the same rule, give the same
    entries, as long as of two that know as much and name the same post or none, the two are alike.
    """
    posts = sorted({_post(entry) for entry in entries} - {""})
    if len(posts) < 2:
        stands = None
        for entry in entries:
            stands = _standing(stands, entry, known)
        return [stands]
    nameless = None
    for entry in entries:
        if not _post(entry):
            nameless = _standing(nameless, entry, known)
    joined = []
    for post in posts:
        stands = None
        for entry in entries:
            if _post(entry) == post:
                stands = _standing(stands, entry, known)
        joined.append(_standing(nameless, stands, known))
    return joined


def _place(attempt: dict) -> tuple[int, str, str]:
    """Where an attempt stands among its topic's: by its number, then its run, then its post's name (_post)."""
    return attempt["attempt"], attempt["run"], _post(attempt)


def _settled(topic: dict) -> dict:
    """A topic of the record with its attempts in order and its state set again; keys of its own stay after."""
    attempts = sorted(topic["attempts"], key=_place)
    state = attempts[-1]["result"] if attempts else topic.get("state", "")
    rest = {key: value for key, value in topic.items() if key not in TOPIC_KEYS}
    return {"id": topic["id"], "topic": topic["topic"], "state": state, "attempts": attempts, **rest}


def merge(old: dict | None, new: dict, order: Sequence[str]) -> dict:
    """old with new put in, as a new record; neither is changed. order: the topic ids as the file lists them.

    old is what read gave (None without a file) and new what collect gave, as export passes it on
    (_admitted). Nothing of old is dropped: a topic,
    an attempt or a run outside the list new lacks stays as it is. An attempt is known by its number and run
    and a run outside the list by its run. Of two entries of one run new's stands, unless old's knows more of
    the run, and the one that stands keeps the keys of the other that the tool does not write (_standing).
    Written entries of one run name whose posts have different names are entries of as many runs, and each
    stays (_joined). A topic new has takes new's sentence and keeps the attempts of both. Each topic's state is
    set again as the result of its last attempt (the highest number, then the last run name). Topics come in
    order, then those order lacks by id; attempts by number, then run, then the post's name; the runs outside
    the list by run, then the post's name. Keys of their own, on top and in a topic, stay, new's over old's. A
    topic id or a run that stands twice in one record is joined by the same rule, the later place as the
    newer; read gives its file so.

    The result therefore does not depend on which of two machines' records is merged first, as long as they
    give each topic the same sentence and of a run name both hold, each saved a post of a name of its own or
    one knows more of the run than the other (_rank); and merging new once more changes nothing.
    """
    place: dict[str, int] = {}
    for index, topic_id in enumerate(order):
        place.setdefault(topic_id, index)
    by_id: dict[str, dict] = {}
    attempts: dict[str, dict[tuple[int, str], list[dict]]] = {}  # each topic's attempts by _key, older first
    runs: dict[str, list[dict]] = {}  # the runs outside the list by run, older first
    rest: dict = {}
    for record in (old, new):
        if record is None:
            continue
        for topic in record["topics"]:
            by_id[topic["id"]] = {**by_id.get(topic["id"], {}), **topic}
            grouped = attempts.setdefault(topic["id"], {})
            for one in topic["attempts"]:
                grouped.setdefault(_key(one), []).append(one)
        for item in record["off_list"]:
            runs.setdefault(item["run"], []).append(item)
        rest.update((key, value) for key, value in record.items() if key not in ("topics", "off_list"))
    settled = (_settled({**topic, "attempts": [one for group in attempts[topic_id].values()
                                               for one in _joined(group, ATTEMPT_KEYS)]})
               for topic_id, topic in by_id.items())
    merged = sorted(settled, key=lambda topic: (topic["id"] not in place, place.get(topic["id"], 0), topic["id"]))
    outside = sorted((item for group in runs.values() for item in _joined(group, OFF_LIST_KEYS)),
                     key=lambda item: (item["run"], _post(item)))
    return {"topics": merged, "off_list": outside, **rest}


def write(brand_dir: Path, record: dict) -> Path:
    """Write record to brand_dir/topics-done.json and return that path.

    The record is readable UTF-8 JSON with an indent of two and one newline at the end. deploy.replace_text
    writes it to a temporary file of its own beside the record and renames that over it. So a write cut off
    never leaves half a record under the name, two exports into one brand folder at once do not write into
    one temporary file, and one a killed export left behind has a name deploy keeps out of the copy. A record
    with text UTF-8 cannot hold (half of a surrogate pair) is written with \\u escapes instead, as
    pipeline.write_json writes such data. An OSError from the write is raised, and the record on disk is then
    as it was.
    """
    path = Path(brand_dir) / DONE_FILE
    replace_text(path, _text(record))
    return path


def _text(record: dict) -> str:
    """record as write puts it in the file: readable UTF-8 JSON, or \\u escapes for text UTF-8 cannot hold."""
    text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(record, ensure_ascii=True, indent=2) + "\n"
    return text


def _same_run(one: dict, other: dict) -> bool:
    """True for two entries of runs outside the list that merge takes for one run (_joined).

    They have one run name and do not name two different posts: a run's post keeps its name, so two names
    are two runs that share a run name, as runs two machines started in one second do. An entry that names
    no post (_post) is of whichever run of the name there is.
    """
    return one["run"] == other["run"] and (not _post(one) or not _post(other) or _post(one) == _post(other))


def _admitted(old: dict | None, new: dict) -> dict:
    """new as export merges it into old: without the runs outside the list that are not to come into the record.

    A run outside the list comes in only when its post was found under the brand's blogs folder (found, as
    collect set it): the runs of a trial whose posts were deleted, a post shown in the session and a save
    that failed have no post to read, and would otherwise fill the list. A run old already holds (_same_run)
    is taken whatever was found, so that what this machine knows of it is merged as merge has it (_standing):
    its post deleted later, the entry stays, and found turns false as long as the run's report.json still
    names the post. The topics are not sifted, for an attempt of the list is recorded with or without its
    post. Neither old nor new is changed.
    """
    held: dict[str, list[dict]] = {}
    for item in old["off_list"] if old is not None else ():
        held.setdefault(item["run"], []).append(item)
    return {**new, "off_list": [item for item in new["off_list"]
                                if item["found"] or any(_same_run(item, one) for one in held.get(item["run"], ()))]}


def export(root: Path, brand: Brand, found: topics.TopicFile | None) -> tuple[Path, dict]:
    """Read the brand's record, collect this machine's, merge them and write the result: (its path, the record).

    Of the runs outside the list collect found, those without a post under blogs that the record does not
    hold yet are passed over (_admitted); the count of off_list is therefore of the runs the record holds,
    not of the runs this machine has. Which runs the record holds is read from each reading of the file.

    Another export may write the file while this one collects. So once the new record is made, the file's
    bytes are read again, and when they are not those the record was made from, everything is done again
    from the reading, up to EXPORT_TRIES times; what another export wrote is then merged too. When they
    differ every time, KeptChanging (KEPT_CHANGING) is raised and the file is left as the others wrote it.
    The second reading comes right before deploy.replace_text writes the temporary file and renames it, and
    nothing guards the moment in between (module docstring).

    LedgerError from read comes before anything is collected or written, so a file that cannot be read is
    left as it is. found, and the other errors, as for collect; nothing is written when it raises.
    """
    path = Path(brand.dir) / DONE_FILE
    order = [topic.id for topic in found.topics] if found is not None else []
    for _ in range(EXPORT_TRIES):
        before = _file_bytes(path)
        old = None if before is None else _parsed(before)
        record = merge(old, _admitted(old, collect(root, brand, found)), order)
        text = _text(record)
        if _file_bytes(path) != before:
            continue  # another export wrote the file meanwhile: read it again
        replace_text(path, text)
        return path, record
    raise KeptChanging(KEPT_CHANGING)


def written(record: dict | None, topic_id: str, run: str | None) -> bool:
    """True when record, as read gave it, has run as a written attempt of the topic topic_id.

    The topics command asks this before it releases a topic: a written topic whose run folder was removed
    reads as reserved, and set free it would go out again.
    """
    if record is None or not _run_name(run):
        return False
    return any(one["run"] == run and one["result"] == claims.WRITTEN
               for topic in record["topics"] if topic["id"] == topic_id for one in topic["attempts"])


def _line(root: Path, entry: claims.Entry, approved: bool) -> str:
    """One topic's line of table.

    An approved topic's is its id and its state as NAMES says it, and for a claimed one the attempt, the run,
    the post's name when it was written, and NO_RUN_RECORD when the run's meta.json cannot be read. A topic
    that is not approved has NOT_APPROVED after its id, and nothing more without a claim: with one, what the
    claim says follows, so that a reservation it holds can be seen and released.
    """
    name = claims.NAMES[entry.state]
    head = [entry.id] if approved else [entry.id, NOT_APPROVED]
    if entry.state == claims.LEFT:
        return "  ".join([*head, name] if approved else head)
    run = _run_name(entry.run)
    parts = [*head, name, f"시도 {entry.attempt}", run or NO_RUN_NAME]
    file = _post_file(root, run) if run and entry.state == claims.WRITTEN else ""
    if file:
        parts.append(file)
    if not run or _object(runs_root(root) / run / "meta.json") is None:
        parts.append(NO_RUN_RECORD)
    return "  ".join(parts)


def table(root: Path, brand: Brand, found: topics.TopicFile) -> list[str]:
    """The lines the topics command prints for a brand's topic file, from one reading of the claims.

    First the brand and what happens once the topics are used up, then how many approved topics are in each
    state and how many are not approved. CARDS_WAITING comes next while claims the topic cards left wait to be
    imported (claims.pending_cards), and for a brand whose brand.toml still has topic_cards, pipeline.LEGACY_CARDS:
    check and run stop its requests that hand the topic over until the claims are imported and the key is gone,
    and the table would look as if nothing were the matter. Then one line for each topic in file order (_line),
    and after them HOW_TO_RELEASE when a topic of the file is reserved, whether it is approved or not: either is
    released the same way. The lines print ids, run names and post names, never a topic's words. ValueError
    for a topic file _check_ids refuses, for which the command shows the sentences of topics.id_problems
    instead of the table; OSError and ValueError from claims.entries and claims.pending_cards as they raise
    them. Only reads.
    """
    _check_ids(found)
    states = claims.entries(root, brand.id, [topic.id for topic in found.topics])
    approved = [topic for topic in found.topics if topic.approved]
    tally = {state: 0 for state in claims.NAMES}
    for topic in approved:
        tally[states[topic.id].state] += 1
    mode = "다 쓰면 다시 돌려씁니다(reuse)." if found.when_used_up == topics.REUSE else "다 쓰면 멈춥니다(stop)."
    lines = [f"브랜드 {brand.name}({brand.id}), {mode}",
             f"승인된 주제 {len(approved)}개 가운데 남음 {tally[claims.LEFT]}, 예약됨 {tally[claims.RESERVED]}, "
             f"작성됨 {tally[claims.WRITTEN]}, 실패 {tally[claims.FAILED]}. 승인 전 {len(found.topics) - len(approved)}개."]
    waiting = claims.pending_cards(root, brand.id, found)
    if waiting:
        lines.append(CARDS_WAITING.format(n=waiting))
    if brand.legacy_cards:
        lines.append(LEGACY_CARDS)
    lines += [_line(root, states[topic.id], topic.approved) for topic in found.topics]
    if any(states[topic.id].state == claims.RESERVED for topic in found.topics):
        lines.append(HOW_TO_RELEASE)
    return lines


def unlisted(root: Path, brand: Brand, found: topics.TopicFile | None) -> list[str]:
    """One sentence (UNLISTED) when claims stand under topic ids the topic file does not list; none otherwise.

    These are the ids collect adds after the file's topics, an id taken out of the file or renamed: an
    export writes their finished attempts under the sentences their claims recorded. Without a topic file
    none of the claimed ids is listed. Only ids a claim file may be named by (topics.usable_id) are counted,
    as collect reads no other. The sentence gives the number of such ids, not of their claims, and prints
    none of them. Only reads; OSError and ValueError as claims.claimed_ids raises them.
    """
    listed = {topic.id for topic in found.topics} if found is not None else set()
    count = sum(1 for topic_id in claims.claimed_ids(root, brand.id)
                if topic_id not in listed and topics.usable_id(topic_id))
    return [UNLISTED.format(n=count)] if count else []
