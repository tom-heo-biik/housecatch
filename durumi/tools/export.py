"""Export: a copy of durumi with one brand, for that brand's own repository (Tom, 2026-11-05).

The housecatch repository (github.com/tom-heo-biik/housecatch, public) carries durumi in its durumi/
folder, and the people there run it through their own /housecatch skill. export fills that folder from
this working tree: the files a deploy ships (deploy.shipped_files) less skills/, every other brand's folder
and the deploy markers, plus the brand's topic record (topics-done.json), which deploy keeps out. The
topics the record says were written are copied as not approved: claims are read from the runs folder
alone, which starts empty in the copy, so they would be handed out again; the plan node still reads them as
topics that exist. The out folder is replaced as a whole except its runs folder, and MARKER records the
commit it came from, without any path of this machine.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import deploy
import ledger
import topics

MARKER = ".durumi-export.json"
KEEP = ("runs",)  # folders of the out folder that stay as they are
LEFT_OUT = ("skills",)  # the brand's repository brings its own skill


class ExportError(Exception):
    """The export cannot go ahead. The message is one sentence for a person."""


def files_for(root: Path, brand_id: str) -> list[Path]:
    """Paths relative to root that the export copies, in a stable order."""
    chosen = []
    for rel in deploy.shipped_files(root):
        if rel.parts[0] in LEFT_OUT:
            continue
        if rel.parts[0] == "brands" and (len(rel.parts) < 2 or rel.parts[1] != brand_id):
            continue
        chosen.append(rel)
    record = Path("brands") / brand_id / ledger.DONE_FILE
    if (root / record).is_file():
        chosen.append(record)
    return chosen


def written_ids(brand_dir: Path) -> set[str]:
    """Ids the topic record lists as written."""
    path = Path(brand_dir) / ledger.DONE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    items = data.get("topics") if isinstance(data, dict) else None
    return {str(item.get("id")) for item in items or [] if isinstance(item, dict) and item.get("state") == "written"}


def _unapprove(path: Path, ids: set[str]) -> int:
    """Set approved to false for the topics of topics.json at path whose id is in ids; how many changed."""
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    for item in data.get("topics", []):
        if isinstance(item, dict) and item.get("id") in ids and item.get("approved") is True:
            item["approved"] = False
            changed += 1
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return changed


def _commit(root: Path) -> str:
    """The short commit of the working tree, with '+' when files differ from it, or '' outside git."""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                              timeout=20).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=root, capture_output=True,
                               text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{head}+" if head and dirty else head


def _clear(out: Path) -> None:
    """Empty out except KEEP."""
    for item in out.iterdir():
        if item.name in KEEP:
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()


def export(root, brand_id: str, out) -> dict:
    """Fill out with the copy (module docstring); what was done comes back for the summary."""
    root, out = Path(root).resolve(), Path(out).resolve()
    brand_dir = root / "brands" / brand_id
    if not (brand_dir / "brand.toml").is_file():
        raise ExportError(f"브랜드 {brand_id}가 없습니다.")
    if out == root or root in out.parents or out in root.parents:
        raise ExportError("내보낼 폴더가 두루미 폴더와 겹칩니다.")
    files = files_for(root, brand_id)
    out.mkdir(parents=True, exist_ok=True)
    _clear(out)
    for rel in files:
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, target)
    unapproved = 0
    copied_topics = out / "brands" / brand_id / topics.TOPICS_FILE
    if copied_topics.is_file():
        unapproved = _unapprove(copied_topics, written_ids(brand_dir))
    marker = {"brand": brand_id, "commit": _commit(root), "exported": datetime.now().astimezone().isoformat(
        timespec="seconds"), "files": len(files)}
    (out / MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"out": out, "files": len(files), "unapproved": unapproved, "commit": marker["commit"]}
