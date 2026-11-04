"""Batch: several posts of one brand at once, on topics nobody had to pick (Tom, 2026-11-05).

/housecatch writes five posts side by side. batch first makes sure the brand has that many approved topics
left (claims.counts, LEFT and FAILED); when it has fewer, the plan node writes the rest (plan.generate). It
then starts one run per post, each its own process with the request that hands the topic over (REQUEST),
at most `parallel` at a time. Every run claims its own topic (claims.assign), so runs started together never
share one. Finished posts are moved from the brand's blogs folder to the outbox when one is given (posts a
killed batch left there are moved first), the topic record is exported (ledger.export), and one summary is
printed: the titles and where they are, and for a run that stopped, the last lines of its log.
"""
from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import claims
import ledger
import plan
import topics
from config import Brand, runs_root
from naming import now_kst

DEFAULT_COUNT = 5
REQUEST = "{name} 블로그 아무거나 써줘\n"  # resolve.topic_kind reads it as a request that hands the topic over
BATCH_DIR = "_batch"  # under the runs folder; show and the claims pass over names that start with '_'
POLL_SEC = 5
STAGGER_SEC = 2  # between two starts, so the runs do not all reach claude in one moment
TAIL_LINES = 4
TOOLS = Path(__file__).resolve().parent
TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
EXIT_OK, EXIT_SOME_STOPPED, EXIT_TOPIC, EXIT_NONE_WRITTEN = 0, 2, 4, 5


def free_count(root, brand: Brand, found: topics.TopicFile | None) -> int:
    """Approved topics a run started now could claim without repeating one: LEFT and FAILED."""
    if found is None:
        return 0
    tally = claims.counts(root, brand.id, found)
    return tally[claims.LEFT] + tally[claims.FAILED]


def _unique(folder: Path, name: str) -> Path:
    """folder/name, or name-2, name-3 when an older file has it."""
    stem, suffix, n = Path(name).stem, Path(name).suffix, 1
    while True:
        target = folder / (name if n == 1 else f"{stem}-{n}{suffix}")
        if not target.exists():
            return target
        n += 1


def move_posts(blogs: Path, outbox: Path, names=None) -> list[Path]:
    """Move the HTML posts of blogs (only `names` when given) into outbox; their new paths come back."""
    if not blogs.is_dir():
        return []
    moved = []
    for post in sorted(blogs.glob("*.html")):
        if names is not None and post.name not in names:
            continue
        target = _unique(outbox, post.name)
        os.replace(post, target)
        moved.append(target)
    return moved


def title_of(post: Path) -> str:
    """The post's <title>, or its file name when it has none."""
    try:
        match = TITLE.search(post.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        match = None
    return " ".join(html.unescape(match.group(1)).split()) if match else post.name


def tail(path: Path, n: int = TAIL_LINES) -> str:
    """The last n lines of a run's log on one line, or '' when it cannot be read."""
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return ""
    return " / ".join([line for line in lines if line][-n:])


def run_command(root: Path, brand: Brand, request: Path) -> list[str]:
    """One run of the request, its post saved into the brand's blogs folder."""
    return [sys.executable, "-X", "utf8", str(TOOLS / "durumi.py"), "run", str(request), "--root", str(root),
            "--brand", brand.id, "--deliver", "brand"]


def run_all(root: Path, brand: Brand, n: int, parallel: int, request: Path, folder: Path, log,
            spawn=subprocess.Popen, sleep=time.sleep, clock=time.monotonic) -> list[tuple[int, int, Path, float]]:
    """Start n runs, at most `parallel` at a time; (number, exit code, log path, minutes) of each, in order."""
    command = run_command(root, brand, request)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    waiting, active, done = list(range(1, n + 1)), {}, []
    while waiting or active:
        while waiting and len(active) < parallel:
            number = waiting.pop(0)
            log_path = folder / f"run-{number}.log"
            handle = open(log_path, "wb")
            proc = spawn(command, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                         cwd=str(root))
            active[number] = (proc, handle, log_path, clock())
            if waiting and len(active) < parallel:
                sleep(STAGGER_SEC)
        sleep(POLL_SEC)
        for number, (proc, handle, log_path, began) in list(active.items()):
            code = proc.poll()
            if code is None:
                continue
            handle.close()
            del active[number]
            minutes = (clock() - began) / 60
            done.append((number, code, log_path, minutes))
            log(f"{number}번째 글 {'끝' if code == 0 else '멈춤'} ({minutes:.0f}분)")
    return sorted(done)


def batch(root, brand: Brand, *, count: int = DEFAULT_COUNT, parallel: int | None = None, outbox=None,
          log=print, call=None, spawn=subprocess.Popen, sleep=time.sleep, clock=time.monotonic) -> int:
    """Write `count` posts of brand at once (module docstring). The exit code says how it went."""
    root = Path(root)
    began = clock()
    blogs = Path(brand.dir) / "blogs"
    outbox = Path(outbox) if outbox else None
    if outbox is not None:
        outbox.mkdir(parents=True, exist_ok=True)
        left = move_posts(blogs, outbox)
        if left:
            log(f"지난번에 끝나고 옮기지 못한 글 {len(left)}편을 {outbox.as_posix()}로 옮겼습니다.")
    found = topics.load(brand.dir)
    free = free_count(root, brand, found)
    if free < count:
        need = count - free
        log(f"새 주제 {need}개를 정합니다. 지난 주제와 겹치지 않게 고릅니다.")
        made = plan.generate(root, brand, need, call=call, log=log)
        for _id, topic in made:
            log(f"- {topic}")
        found = topics.load(brand.dir)
        free = free_count(root, brand, found)
    n = min(count, free)
    if n == 0:
        log("쓸 주제가 없어 글을 쓰지 못했습니다.")
        return EXIT_TOPIC
    if n < count:
        log(f"새 주제가 {n}개뿐이라 {n}편만 씁니다.")
    folder = runs_root(root) / BATCH_DIR / now_kst().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    request = folder / "request.txt"
    request.write_text(REQUEST.format(name=brand.name), encoding="utf-8")
    before = {p.name for p in blogs.glob("*.html")} if blogs.is_dir() else set()
    log(f"블로그 {n}편을 한꺼번에 씁니다. 한 편에 20분 안팎이 걸리고, 모두 함께 돌아갑니다.")
    results = run_all(root, brand, n, max(1, parallel or n), request, folder, log, spawn=spawn, sleep=sleep,
                      clock=clock)
    fresh = {p.name for p in blogs.glob("*.html")} - before if blogs.is_dir() else set()
    if outbox is not None:
        posts = move_posts(blogs, outbox, fresh)
    else:
        posts = sorted(blogs / name for name in fresh)
    try:
        ledger.export(root, brand, topics.load(brand.dir))
    except Exception as exc:  # the posts are written; a record that cannot be exported only gets a line
        log(f"주제 기록을 쓰지 못했습니다. {exc}")
    stopped = [r for r in results if r[1] != 0]
    log("")
    log(f"블로그 {len(posts)}편을 썼습니다(걸린 시간 {(clock() - began) / 60:.0f}분).")
    if posts:
        log(f"저장한 곳 {(outbox or blogs).as_posix()}")
        for post in posts:
            log(f"- {title_of(post)}")
    if stopped:
        log(f"쓰지 못한 글 {len(stopped)}편")
        for number, code, log_path, _minutes in stopped:
            log(f"- {number}번째 글(종료 코드 {code}) {tail(log_path)}")
    if not posts:
        return EXIT_NONE_WRITTEN
    return EXIT_SOME_STOPPED if stopped else EXIT_OK
