"""Run ids, final file names and collision-free folders. Importing this module has no side effects."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Korea Standard Time is a fixed UTC+9 offset with no daylight saving. zoneinfo is not used
# because Windows Python may ship without tzdata.
KST = timezone(timedelta(hours=9), "KST")

# Both are matched with fullmatch: with re.match, `$` also matches before a trailing newline,
# so a slug ending in a newline would pass and later fail as a file name.
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
SLUG_MIN, SLUG_MAX = 3, 60
BRAND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
# Run folder names made by run_id and unique_dir: 20260930-143012-brand, ...-brand-2. Other folders
# in runs/, such as the topic claims in _topics (tools/claims.py), the claims the topic cards left in
# _cards (which claims.import_cards reads) or a _requests folder, start with '_' and never match.
RUN_NAME = re.compile(r"\d{8}-\d{6}-[A-Za-z0-9][A-Za-z0-9_-]*")


def now_kst(now: datetime | None = None) -> datetime:
    """Current time in KST. `now` (timezone-aware) may be passed to convert a given instant."""
    if now is None:
        return datetime.now(KST)
    if now.tzinfo is None:
        raise ValueError("시간대가 없는 시각은 KST로 바꿀 수 없습니다.")
    return now.astimezone(KST)


def _kst(now: datetime) -> datetime:
    # Aware datetimes are converted to KST; naive ones are taken as KST wall-clock time.
    return now if now.tzinfo is None else now.astimezone(KST)


def run_id(now: datetime, brand_id: str) -> str:
    """'20260930-143012-brand'."""
    if not BRAND_ID.fullmatch(brand_id or ""):
        raise ValueError(f"브랜드 id에 쓸 수 없는 문자가 있습니다: {brand_id!r}")
    return f"{_kst(now):%Y%m%d-%H%M%S}-{brand_id}"


def valid_slug(slug: str) -> bool:
    return bool(SLUG.fullmatch(slug or "")) and SLUG_MIN <= len(slug) <= SLUG_MAX


def final_name(now: datetime, slug: str) -> str:
    """'2026-09-30-{slug}.html', dated in KST."""
    if not valid_slug(slug):
        raise ValueError(f"slug는 영문 소문자, 숫자, 하이픈으로 된 {SLUG_MIN}~{SLUG_MAX}자여야 합니다: {slug!r}")
    return f"{_kst(now):%Y-%m-%d}-{slug}.html"


def blog_name(now: datetime, slug: str) -> str:
    """'2026-09-30-1430-{slug}.html' for brands/{id}/blogs/, which the team shares through git.

    The start time (to the minute) makes it unlikely that two teammates who write the same topic on
    the same day commit one path twice; the same minute on two machines would still collide.
    """
    final_name(now, slug)  # the same slug rule
    return f"{_kst(now):%Y-%m-%d-%H%M}-{slug}.html"


def unique_dir(base: Path, name: str, create: bool = True) -> Path:
    """base/name, or base/name-2, base/name-3, ... when taken.

    With create=True (default) the folder is created atomically, so two runs started in the
    same second cannot receive the same folder. With create=False only the free path is returned.
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"폴더 이름으로 쓸 수 없습니다: {name!r}")
    base = Path(base)
    if create:
        base.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        candidate = base / (name if n == 1 else f"{name}-{n}")
        if create:
            try:
                candidate.mkdir()
                return candidate
            except FileExistsError:
                pass
        elif not candidate.exists():
            return candidate
        n += 1
