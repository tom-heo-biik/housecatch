"""Topic plan: new topics for a brand, written by the plan node and kept by the code (Tom, 2026-11-05).

/housecatch writes several posts at once, each on a topic not written before, and nobody picks the topics.
The plan node gets the brand document and context/plan.md as its system prompt (nodes/plan.toml), and as
its request every topic topics.json holds, approved or not, with its note, and how many new ones to write.
It answers in JSON (schema). The code keeps a candidate only when it is new: not the sentence of a listed
topic or of a kept candidate (topics.sentence_key), not too close to one (too_close), within the length
bounds, and free of what topics.problems finds in it. It asks again for the rest, naming what it turned
down, up to ATTEMPTS times, and appends what it kept to topics.json as approved topics whose ids continue
the file's (next_ids). The file is rewritten whole; the topics already in it stay as they are.
"""
from __future__ import annotations

import difflib
import json
import os
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import topics
from assemble import assemble
from config import Brand, Rules, load_node, load_rules
from node import call_node, find_claude

NODE = "plan"
ATTEMPTS = 3
SIMILAR = 0.8  # difflib ratio on letters and digits alone; at or above it two topics are one
TOPIC_MIN, TOPIC_MAX = 8, 80  # characters of a topic line
NOTE_MAX = KEYWORD_MAX = 40
ID_SHAPE = re.compile(r"^([a-z]+)([0-9]+)$")
AUTO_NOTE = "자동 {date}"  # appended to the node's note, so a person can tell the plan node's topics apart
LOGIN_WORDS = ("/login", "log in", "login", "API key", "OAuth", "인증")

REQUEST_HEAD = "{name} 블로그의 새 주제 {count}개를 써 주십시오."
LISTED_HEAD = "이미 있는 주제 {n}개입니다. 이 주제들과 같거나 비슷한 주제는 쓰지 마십시오."
NONE_LISTED = "이미 있는 주제는 없습니다."
TURNED_DOWN_HEAD = "앞서 낸 주제 가운데 다음은 받지 않았습니다. 이 주제들도 다시 쓰지 마십시오."
SPREAD = "새 주제끼리도 겹치지 않게 장소와 상황과 읽는 사람의 처지를 고르게 나눠 주십시오."
NO_ANSWER = "주제 노드가 답하지 못했습니다({n}번째)."
NOT_LIST = "주제 노드의 답에 주제 목록이 없습니다({n}번째)."
LOGIN_NEEDED = ("Claude에 로그인되어 있지 않아 주제를 정하지 못했습니다. 터미널에서 claude를 한 번 실행해 로그인한 뒤 "
                "다시 시도해 주세요.")


def schema(count: int) -> dict:
    """JSON Schema of the node's answer: exactly `count` topics, each a line, a note and a keyword."""
    item = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "minLength": TOPIC_MIN, "maxLength": TOPIC_MAX},
            "note": {"type": "string", "maxLength": NOTE_MAX},
            "keyword": {"type": "string", "maxLength": KEYWORD_MAX},
        },
        "required": ["topic", "note", "keyword"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"topics": {"type": "array", "minItems": count, "maxItems": count, "items": item}},
        "required": ["topics"],
        "additionalProperties": False,
    }


def _line(value: object) -> str:
    """A string value on one line, whitespace runs made one space; anything else is ''."""
    return " ".join(value.split()) if isinstance(value, str) else ""


def _plain(text: str) -> str:
    """Letters and digits alone, so that spacing and marks do not make two topics differ."""
    return re.sub(r"[\W_]+", "", text)


def too_close(a: str, b: str) -> bool:
    """True when two topic lines are one topic told twice (SIMILAR)."""
    x, y = _plain(a), _plain(b)
    if not x or not y:
        return x == y
    return difflib.SequenceMatcher(None, x, y, autojunk=False).ratio() >= SIMILAR


def request_text(brand: Brand, listed: list[tuple[str, str]], count: int, turned: list[str]) -> str:
    """The node's request: the head, every listed topic as '- 주제 (메모)', what was turned down, SPREAD."""
    lines = [REQUEST_HEAD.format(name=brand.name, count=count), ""]
    if listed:
        lines.append(LISTED_HEAD.format(n=len(listed)))
        lines += [f"- {topic} ({note})" if note else f"- {topic}" for topic, note in listed]
    else:
        lines.append(NONE_LISTED)
    if turned:
        lines += ["", TURNED_DOWN_HEAD, *(f"- {line}" for line in turned)]
    lines += ["", SPREAD]
    return "\n".join(lines) + "\n"


def review(candidates: list, pool: list[str], rules: Rules, brand: Brand) -> tuple[list[dict], list[str]]:
    """(kept, turned down) of the node's candidates against pool, the topic lines already there.

    A kept candidate joins the pool at once, so two candidates of one answer are judged against each other
    too. A turned-down one comes back as 'line (reason)' for the next request.
    """
    kept: list[dict] = []
    turned: list[str] = []
    pool = list(pool)
    keys = {topics.sentence_key(line) for line in pool}
    for item in candidates:
        item = item if isinstance(item, dict) else {}
        line, note, keyword = _line(item.get("topic")), _line(item.get("note")), _line(item.get("keyword"))
        reason = ""
        if not TOPIC_MIN <= len(line) <= TOPIC_MAX:
            reason = f"길이 {len(line)}자"
        elif topics.sentence_key(line) in keys:
            reason = "이미 있는 주제와 같음"
        else:
            near = next((other for other in pool if too_close(line, other)), None)
            if near is not None:
                reason = f"'{near}'와 비슷함"
        if not reason:
            probe = topics.TopicFile(path=Path(topics.TOPICS_FILE), when_used_up=topics.STOP,
                                     topics=(topics.Topic(id="new", topic=line, keyword=keyword[:KEYWORD_MAX],
                                                          approved=True, note=note[:NOTE_MAX]),))
            found = topics.problems(probe, rules, brand.banned_phrases)
            if found:
                reason = found[0]
        if reason:
            turned.append(f"{line or '(빈 주제)'} ({reason})")
            continue
        kept.append({"topic": line, "note": note[:NOTE_MAX], "keyword": keyword[:KEYWORD_MAX]})
        pool.append(line)
        keys.add(topics.sentence_key(line))
    return kept, turned


def next_ids(ids: list[str], n: int) -> list[str]:
    """n ids that continue the file's: h045, h046 after h001..h044; 't' and three digits for a mixed file."""
    shapes = [ID_SHAPE.match(i) for i in ids]
    if ids and all(shapes) and len({m.group(1) for m in shapes}) == 1:
        prefix = shapes[0].group(1)
        width = max(len(m.group(2)) for m in shapes)
        number = max(int(m.group(2)) for m in shapes) + 1
    else:
        prefix, width, number = "t", 3, len(ids) + 1
    taken, found = set(ids), []
    while len(found) < n:
        candidate = f"{prefix}{number:0{width}d}"
        if candidate not in taken and topics.usable_id(candidate):
            found.append(candidate)
        number += 1
    return found


def append(brand_dir: Path, kept: list[dict], now: datetime) -> list[str]:
    """Append kept topics to topics.json as approved and return their ids. A missing file is made (stop)."""
    path = topics.path_of(Path(brand_dir))
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {"when_used_up": topics.STOP, "topics": []}
    listed = data.get("topics")
    if not isinstance(listed, list):
        raise ValueError(topics.NOT_LIST)
    ids = next_ids([str(item.get("id", "")) for item in listed if isinstance(item, dict)], len(kept))
    stamp = AUTO_NOTE.format(date=now.strftime("%Y-%m-%d"))
    for new_id, item in zip(ids, kept):
        note = f"{item['note']}, {stamp}" if item["note"] else stamp
        listed.append({"id": new_id, "topic": item["topic"], "keyword": item["keyword"], "approved": True,
                       "note": note})
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_name(f".{path.name}.{os.getpid()}.new")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temp, path)
    return ids


def login_needed(error: str) -> bool:
    """True when a node's error reads as a CLI that is not logged in."""
    lowered = (error or "").lower()
    return any(word.lower() in lowered for word in LOGIN_WORDS)


def generate(root, brand: Brand, count: int, *, call: Callable | None = None, log=print,
             now: datetime | None = None) -> list[tuple[str, str]]:
    """Write `count` new topics for brand into its topics.json; (id, topic) of those kept come back.

    call(system_prompt, request, schema) stands in for the node in tests; it returns a NodeResult. Fewer
    than count come back when the node could not give enough new ones in ATTEMPTS answers; none when it
    could not answer at all, after logging why (LOGIN_NEEDED for a CLI that is not logged in).
    """
    root = Path(root)
    rules = load_rules(root)
    cfg = load_node(root, NODE)
    system_prompt = assemble(root, cfg, brand, rules).text
    if call is None:
        binary, _version = find_claude()

        def call(system, request, answer_schema):
            return call_node(binary, system, request, cfg, answer_schema)

    found = topics.load(brand.dir)
    listed = [(t.topic, t.note) for t in found.topics] if found is not None else []
    kept_all: list[dict] = []
    turned_all: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        need = count - len(kept_all)
        if need <= 0:
            break
        pool = [line for line, _ in listed] + [item["topic"] for item in kept_all]
        shown = listed + [(item["topic"], item["note"]) for item in kept_all]
        result = call(system_prompt, request_text(brand, shown, need, turned_all), schema(need))
        if not result.ok or not isinstance(result.envelope, dict):
            error = getattr(result, "error", "") or getattr(result, "stderr", "")
            if login_needed(error):
                log(LOGIN_NEEDED)
                return []
            log(f"{NO_ANSWER.format(n=attempt)} {error}".strip())
            continue
        candidates = result.envelope.get("topics")
        if not isinstance(candidates, list):
            log(NOT_LIST.format(n=attempt))
            continue
        kept, turned = review(candidates, pool, rules, brand)
        kept_all += kept[:need]
        turned_all += turned
    if not kept_all:
        return []
    ids = append(brand.dir, kept_all, now or datetime.now())
    return list(zip(ids, (item["topic"] for item in kept_all)))
