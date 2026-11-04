"""Build the deploy copy of the plugin: only what a run needs, laid out as a local marketplace.

  python tools/durumi.py deploy [--out DIR]

This root is the source of truth, but it also holds what must not ship: runs/ (every run's request
and output), tests/, docs/, CLAUDE.md, caches and other tools' state (.omc/). Installing from a local
directory marketplace copies the plugin folder as it is (runs/ and all) into Claude Code's cache, even
though the plugin then loads in place from the marketplace folder (checked with 2.1.284). So the
marketplace points at a clean copy instead, and a rebuild reaches the next session without a
plugin update:

  <out>/.claude-plugin/marketplace.json   marketplace "durumi", plugin source "./durumi"
  <out>/.durumi-deploy.json               what this build recorded (the deploy marker)
  <out>/durumi/                           .claude-plugin/plugin.json, skills, tools, rules, nodes,
                                          context, brands, and .durumi-source.json (this root's path)

<out> defaults to dist/marketplace under this root (dist/ is ignored by git). Only a folder that
carries the deploy marker is ever emptied, so a mistyped --out cannot wipe anything else. A rebuild
is written to <out>.new, checked (the command line runs claude plugin validate on it), and only then
renamed into place, so sessions reading the copy in place never see it half written. An OS lock
on <out>.durumi-lock keeps two deploys from writing into one folder at once.

Inside the monorepo nobody installs anything: Claude Code reads the project's own skills, so the
project skill .claude/skills/durumi/SKILL.md is the entry there, and a teammate only pulls. It is
generated from the plugin skill (python tools/durumi.py sync-skill) so the two entries never drift.
The same command copies the monorepo's CLAUDE.md sections that durumi documents carry (MIRRORED).

Importing this module has no side effects.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from config import SOURCE_MARKER, in_plugin_cache, is_deploy_copy

if os.name == "nt":
    import msvcrt
else:
    import fcntl

SHIP = (".claude-plugin/plugin.json", "skills", "tools", "rules", "nodes", "context", "brands")
OUTPUT_DIRS = ("blogs", "analysis", "meeting", "sources")  # brands/{id}/ folders kept out of the copy (_is_output)
OUTPUT_FILES = ("topics-done.json",)  # files of brands/{id}/ kept out of the copy, as OUTPUT_DIRS keeps folders out
SKIP_NAMES = {"__pycache__", ".omc", ".DS_Store", "Thumbs.db"}
SKIP_SUFFIXES = (".pyc", ".pyo")
TEMP_SUFFIX = ".new"
# The name replace_text gives the temporary file it writes beside a document, .writing.md.k3j9x2ab.new
# for writing.md: a dot, the document's name, a dot, the eight characters tempfile puts between a
# prefix and a suffix (lower-case letters, digits and '_' in Python 3.11 to 3.14), TEMP_SUFFIX.
TEMP_NAME = re.compile(r"\..+\.[a-z0-9_]{8}" + re.escape(TEMP_SUFFIX), re.DOTALL)
MARKER = ".durumi-deploy.json"
STAGING_SUFFIX = ".new"  # <out>.new is built first, then swapped in for <out>
OLD_SUFFIX = ".old"
LOCK_SUFFIX = ".durumi-lock"  # <out>.durumi-lock holds an OS lock while a deploy runs
PLUGIN_DIR = "durumi"
SCHEMA = "https://anthropic.com/claude-code/marketplace.schema.json"
# rename_into_place: on Windows a rename over a file another process holds open can fail with PermissionError while
# it is held, and from the topic step on other processes read a run's meta.json and the topic record (tools/claims.py,
# tools/ledger.py). Reasoned from the code, not measured: the tools have not been run on Windows with readers at
# work, so whether REPLACE_TRIES tries REPLACE_WAIT_SEC seconds apart outlast such a reader there is not known.
REPLACE_TRIES = 5
REPLACE_WAIT_SEC = 0.1


class DeployError(Exception):
    pass


def default_out(root) -> Path:
    return Path(root) / "dist" / "marketplace"


def _skip(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES)


def _is_temp(name: str) -> bool:
    """True for a file name of the shape replace_text gives its temporary file (TEMP_NAME).

    That file is renamed over the document as soon as it is written, so one found beside a document
    is a run still writing it or the leftover of a run killed before the rename; neither ships. Only
    that shape is left out: notes.new, .hidden.new and writing.md.new ship as any other file does.
    """
    return TEMP_NAME.fullmatch(name) is not None


def _is_output(rel: Path) -> bool:
    """brands/{id}/blogs/ holds finished posts (pipeline.save_to_brand), brands/{id}/analysis/ the monthly
    blog analyses (the naver-blog-analysis skill), brands/{id}/meeting/ the documents read with the client
    at meetings and brands/{id}/sources/ the materials the client sent, which brand.md is written from;
    none of them ever ships. Nor does brands/{id}/topics-done.json, the record of the topics written that the
    topics command exports for the team (tools/ledger.py): a run does not read it, and a copy does not keep it
    up to date. brands/{id}/topics.json, which a run reads, ships."""
    parts = rel.parts
    if len(parts) <= 2 or parts[0] != "brands":
        return False
    return parts[2] in OUTPUT_DIRS or (len(parts) == 3 and parts[2] in OUTPUT_FILES)


def shipped_files(root, entries=SHIP) -> list[Path]:
    """Every file under `entries` that goes into the deploy copy, relative to root, in a stable order."""
    root = Path(root)
    files = []
    for entry in entries:
        path = root / entry
        if path.is_file():
            files.append(Path(entry))
        elif path.is_dir():
            for item in sorted(path.rglob("*")):
                rel = item.relative_to(root)
                # _is_temp reads the file's own name only: replace_text makes files, never folders.
                if (item.is_file() and not any(_skip(part) for part in rel.parts) and not _is_temp(rel.name)
                        and not _is_output(rel)):
                    files.append(rel)
        else:
            raise DeployError(f"배포에 필요한 항목이 없습니다. {entry}")
    return files


def content_hash(root, files) -> str:
    """sha256 over the shipped files' paths and bytes, so any change to what ships changes it."""
    digest = hashlib.sha256()
    for rel in files:
        digest.update(rel.as_posix().encode("utf-8") + b"\0")
        digest.update((Path(root) / rel).read_bytes() + b"\0")
    return digest.hexdigest()


def _check_out(root: Path, out: Path) -> None:
    if out == root or out in root.parents:
        raise DeployError(f"배포 폴더가 두루미 폴더이거나 그 상위 폴더입니다. {out}")
    for entry in SHIP:
        if out == root / entry or (root / entry) in out.parents:
            raise DeployError(f"배포 폴더가 배포할 항목 안에 있습니다. {out}")
    if out.exists() and not out.is_dir():
        raise DeployError(f"배포 폴더 자리에 파일이 있습니다. {out}")
    if out.is_dir() and any(out.iterdir()) and not (out / MARKER).is_file() and not _only_bytecode(out):
        raise DeployError(f"두루미 배포본이 아닌 폴더라 비우지 않습니다. {out}")


def previous(out) -> dict:
    """What the last build in `out` recorded, or {} when there was none."""
    try:
        return json.loads((Path(out) / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _add_mode(path: Path, bits: int, info=None) -> None:
    try:
        mode = stat.S_IMODE((info or path.lstat()).st_mode)
        if mode & bits != bits:
            os.chmod(path, mode | bits)
    except OSError:
        pass  # the retry that follows reports what is still refused


def _is_link(info) -> bool:
    """A symbolic link, or on Windows any reparse point (a junction too), from an lstat result."""
    attributes = getattr(info, "st_file_attributes", 0)  # Windows only
    return stat.S_ISLNK(info.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _open_up(path: Path) -> None:
    """Give the owner what deleting path needs, adding permissions and never taking any away.

    Windows refuses to delete an entry with the read-only attribute, which chmod with the write bit
    clears, so path and everything below it get that bit. POSIX decides by the folder holding an
    entry, so path and every folder below it get owner rwx; path's own folder is already writable,
    since the deploy has just renamed a folder in it. A link or a junction is left alone: chmod and
    the walk would follow it out of the folder, and its own mode never stops it being deleted.
    """
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            info = current.lstat()
        except OSError:
            continue
        if _is_link(info):
            continue
        if not stat.S_ISDIR(info.st_mode):
            _add_mode(current, stat.S_IWUSR, info)
            continue
        _add_mode(current, stat.S_IRWXU, info)
        try:
            pending.extend(current.iterdir())
        except OSError:
            pass


def _delete_once(path: Path) -> None:
    if path.is_dir() and not _is_link(path.lstat()):
        shutil.rmtree(path)
    else:
        path.unlink()  # a link, a junction too, goes without what it points at


def _delete(path: Path) -> None:
    """Delete a file or a folder; refused for permissions, open it up (_open_up) and try once more."""
    try:
        _delete_once(path)
    except PermissionError:
        _open_up(path)
        _delete_once(path)


def _remove_once(folder: Path) -> None:
    for child in folder.iterdir():
        if child.name != MARKER:
            _delete_once(child)
    if os.path.lexists(folder / MARKER):
        _delete_once(folder / MARKER)
    folder.rmdir()


def _remove(folder: Path) -> None:
    """Delete a folder this module made, its marker last: stopped halfway, it still reads as ours."""
    try:
        _remove_once(folder)
    except PermissionError:
        _open_up(folder)
        _remove_once(folder)


def _raise(error: OSError) -> None:
    raise error


def _only_bytecode(folder: Path) -> bool:
    """A folder holding nothing but bytecode Python cached by itself.

    That is .pyc files in __pycache__ folders and the folders leading to them. Anything else is
    someone's and must never be deleted as a leftover: an empty folder, a link, and a folder that
    cannot be listed too, since what it holds is unknown.
    """
    try:
        if not folder.is_dir() or _is_link(folder.lstat()):
            return False
        for current, dirs, files in os.walk(folder, onerror=_raise):
            here = Path(current)
            if any(_is_link((here / name).lstat()) for name in dirs + files):
                return False
            if here != folder and here.name != "__pycache__" and not dirs:
                return False
            if files and (here.name != "__pycache__" or any(not name.endswith(".pyc") for name in files)):
                return False
    except OSError:
        return False
    return True


def _clear(folder: Path) -> None:
    """Remove a leftover staging or old folder, but only one this module made (it carries MARKER)."""
    if not os.path.lexists(folder):
        return
    if not _is_link(folder.lstat()) and folder.is_dir() and ((folder / MARKER).is_file() or _only_bytecode(folder)):
        _remove(folder)  # a link here is someone's: what it points at is never emptied
        return
    raise DeployError(f"두루미 배포본이 아닌 폴더가 배포 폴더 옆에 있어 배포하지 않습니다. 확인한 뒤 지워 주십시오. {folder}")


def _rename_into(src: Path, dst: Path) -> None:
    """src.rename(dst), clearing first a dst that Python brought back with only bytecode in it.

    Python recreates missing parent folders when it writes a .pyc, so a preflight running from the
    copy between the two renames of a swap can resurrect `out` with a lone __pycache__ inside.
    """
    try:
        src.rename(dst)
    except OSError:
        if not _only_bytecode(dst):
            raise
        _delete(dst)
        src.rename(dst)


def _swap(new: Path, out: Path) -> None:
    """Put the finished copy `new` in place of `out` by renaming, and keep the old copy if that fails."""
    if out.exists() and not (out / MARKER).is_file() and _only_bytecode(out):
        _delete(out)  # left by an earlier swap that such a .pyc interrupted
    if not out.exists():
        new.rename(out)
        return
    old = out.with_name(out.name + OLD_SUFFIX)
    _clear(old)
    out.rename(old)  # a file held open inside `out` stops the deploy here, before anything changed
    try:
        _rename_into(new, out)
    except OSError:
        _rename_into(old, out)
        raise
    try:
        _remove(old)
    except OSError:
        pass  # it keeps its marker, so the next deploy clears it


def _os_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # one byte, past the end of an empty file is fine
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _lock(out: Path):
    """Hold an OS lock on <out>.durumi-lock while deploying; a second deploy into the same folder is refused.

    The operating system drops the lock when the process ends however it ends, so a deploy that was
    killed never blocks the next one and no clock is involved. The file itself is never deleted or
    truncated: whatever sits under that name beside a mistyped --out stays as it was.
    """
    path = out.with_name(out.name + LOCK_SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        try:
            _os_lock(handle)
        except OSError:
            raise DeployError(f"다른 배포가 이 폴더에 배포하는 중입니다. 그 배포가 끝난 뒤 다시 실행하십시오. {out.as_posix()}") from None
        try:
            yield
        finally:
            try:
                _os_unlock(handle)
            except OSError:
                pass  # closing the file below releases it anyway


def validate_with(claude, folder) -> list[str]:
    """What `claude plugin validate` rejects in the marketplace and in the plugin under `folder`."""
    problems = []
    for label, target in (("마켓플레이스", Path(folder)), ("플러그인", Path(folder) / PLUGIN_DIR)):
        try:
            done = subprocess.run([str(claude), "plugin", "validate", str(target)], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(f"{label} 검증을 돌리지 못했습니다({exc}).")
            continue
        if done.returncode != 0:
            problems.append(f"{label} 검증에 실패했습니다({' '.join((done.stdout + done.stderr).split())[-400:]}).")
    return problems


def build(root, out=None, validate=None) -> dict:
    """Rebuild `out` from root and return a summary (see the module docstring for the layout).

    The copy is built beside `out` (<out>.new) and swapped in whole, so a session that loads the
    plugin in place never meets a half-filled folder, and a failure anywhere leaves the last copy as
    it was. validate(folder) -> list of problems, when given, must find none before the swap.
    """
    root = Path(root).resolve()
    out = (default_out(root) if out is None else Path(out)).resolve()
    kept = "지난 배포본은 그대로입니다. " if (out / MARKER).is_file() else ""
    try:
        return _build(root, out, validate, kept)
    except OSError as exc:
        raise DeployError(f"배포본을 만들거나 바꿔 끼우지 못했습니다. {kept}{exc}") from exc


def _build(root: Path, out: Path, validate, kept: str) -> dict:
    _check_out(root, out)
    try:
        plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeployError(f".claude-plugin/plugin.json을 읽지 못했습니다. {exc}") from exc
    files = shipped_files(root)
    digest = content_hash(root, files)
    staging = out.with_name(out.name + STAGING_SUFFIX)
    with _lock(out):
        before = previous(out)
        _clear(staging)
        try:
            summary = _write_copy(root, staging, files, plugin, digest, before, out)
            problems = validate(staging) if validate else []
            if problems:
                raise DeployError("배포본 검증에 실패해 바꿔 끼우지 않았습니다. " + kept + " ".join(problems))
            _swap(staging, out)
        finally:
            if staging.exists():
                try:
                    _remove(staging)
                except OSError:
                    pass  # it keeps its marker, so the next deploy clears it
    return summary


def _write_copy(root: Path, staging: Path, files, plugin: dict, digest: str, before: dict, out: Path) -> dict:
    """Write the whole deploy copy into `staging` and return the summary it records."""
    staging.mkdir(parents=True)
    (staging / MARKER).write_text("{}\n", encoding="utf-8", newline="\n")  # a crashed build is ours to clear
    target = staging / PLUGIN_DIR
    for rel in files:
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, dest)  # contents only: a read-only source must not make an undeletable copy
    marketplace = {
        "$schema": SCHEMA,
        "name": plugin["name"],
        "description": "두루미 배포용 마켓플레이스. python tools/durumi.py deploy가 만듭니다.",
        "owner": plugin.get("author") or {"name": "biik"},
        "plugins": [{
            "name": plugin["name"],
            "description": plugin.get("description", ""),
            "version": plugin.get("version", "0.0.0"),
            "source": f"./{PLUGIN_DIR}",
            "category": "productivity",
        }],
    }
    # The copy records where it came from: loaded from outside the monorepo, it still runs on the
    # working tree (config.find_root), and it keeps no runs of its own (config.runs_root).
    (target / SOURCE_MARKER).write_text(json.dumps({"source": root.as_posix()}, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8", newline="\n")
    (staging / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (staging / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(marketplace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    summary = {
        "name": plugin["name"],
        "version": plugin.get("version", "0.0.0"),
        "content_sha256": digest,
        "files": len(files),
        "bytes": sum((root / rel).stat().st_size for rel in files),
        "out": out.as_posix(),
        "changed": before.get("content_sha256") != digest,
    }
    (staging / MARKER).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8", newline="\n")
    return summary


CODE = ("skills", "tools")  # what runs from a deploy copy itself; the rest is read from the root it finds


def code_differs(copy, source) -> bool:
    """Whether a deploy copy's skill and tools differ from what the source would ship now.

    Only skills/ and tools/ are listed: preflight asks this on every call of the installed plugin,
    and brands/ fills with committed posts.
    """
    try:
        theirs, ours = (shipped_files(folder, CODE) for folder in (copy, source))
    except DeployError:
        return True
    return theirs != ours or content_hash(copy, theirs) != content_hash(source, ours)


# The monorepo's project skill
#
# The plugin skill reaches tools/durumi.py as ${CLAUDE_PLUGIN_ROOT}/tools/durumi.py. The project skill
# walks there from its own folder instead (${CLAUDE_SKILL_DIR}), which holds whichever folder the
# project was opened at. Nothing else differs, apart from a YAML comment saying the file is generated.

PLUGIN_SKILL = Path("skills") / "durumi" / "SKILL.md"
PROJECT_SKILL = Path(".claude") / "skills" / "durumi" / "SKILL.md"
PLUGIN_CLI = "${CLAUDE_PLUGIN_ROOT}/tools/durumi.py"
FRONT = "---\n"


def monorepo_of(root) -> Path | None:
    """The git working tree holding root below itself (the monorepo), or None."""
    root = Path(root).resolve()
    return next((folder for folder in root.parents if (folder / ".git").exists()), None)


def project_skill_text(plugin_text: str, cli_from_skill: str, note: str) -> str:
    """The plugin skill with tools/durumi.py reached from the project skill's folder, marked as generated."""
    if not plugin_text.startswith(FRONT) or PLUGIN_CLI not in plugin_text:
        raise DeployError(f"플러그인 스킬의 머리말이나 주입 명령이 예상과 달라 모노레포 스킬을 만들지 않습니다. {PLUGIN_SKILL.as_posix()}")
    body = plugin_text[len(FRONT):].replace(PLUGIN_CLI, "${CLAUDE_SKILL_DIR}/" + cli_from_skill)
    return f"{FRONT}# {note}\n{body}"


def project_skill(root) -> tuple[Path, str]:
    """(path, text) of the project skill for the monorepo that holds root."""
    root = Path(root).resolve()
    if is_deploy_copy(root) or in_plugin_cache(root):
        raise DeployError("모노레포 스킬은 원본 폴더에서만 만듭니다. 배포본이나 플러그인 캐시에서는 만들지 않습니다.")
    mono = monorepo_of(root)
    if mono is None:
        raise DeployError(f"두루미 폴더를 담은 git 저장소를 찾지 못해 모노레포 스킬을 만들지 않습니다. {root}")
    path = mono / PROJECT_SKILL
    cli = Path(os.path.relpath(root / "tools" / "durumi.py", path.parent)).as_posix()
    rel = root.relative_to(mono).as_posix()
    note = (f"생성 파일입니다. {rel}/{PLUGIN_SKILL.as_posix()}를 고친 뒤 모노레포 루트에서 "
            f"{rel}/tools/durumi.py sync-skill을 python이나 py나 python3로 돌려 다시 만드세요.")
    try:
        plugin_text = (root / PLUGIN_SKILL).read_text(encoding="utf-8")
    except OSError as exc:
        raise DeployError(f"플러그인 스킬을 읽지 못했습니다. {PLUGIN_SKILL.as_posix()} ({exc})") from exc
    return path, project_skill_text(plugin_text, cli, note)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")  # universal newlines: a CRLF checkout still matches
    except (OSError, UnicodeDecodeError):
        return None


def sync_project_skill(root) -> dict:
    """Write the project skill when it is missing or differs. {"path", "created", "changed"}.

    It is written by replace_text, beside the skill and renamed over it: an interrupted sync never
    leaves half a skill, and a symlink at the path is replaced instead of followed onto whatever file
    it points at. An OSError from the write is raised for the caller to report.
    """
    path, text = project_skill(root)
    before = _read_text(path)
    if before != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_text(path, text)
    return {"path": path.as_posix(), "created": before is None, "changed": before != text}


def stale_project_skill(root) -> Path | None:
    """The project skill when it exists but no longer matches the plugin skill, else None."""
    try:
        path, text = project_skill(root)
    except DeployError:
        return None
    before = _read_text(path)
    return path if before is not None and before != text else None


# Sections of the monorepo's documents that a durumi document carries word for word. Nodes never see
# the monorepo's CLAUDE.md, so the section is copied in, and by code rather than by hand (Tom,
# 2026-09-30): a run, a deploy and sync-skill each copy it over whatever the target held.
MIRRORED = ((Path("CLAUDE.md"), "적확한 표현", Path("context") / "writing.md"),)


def _section_span(text: str, title: str) -> tuple[int, int] | None:
    """(start, end) of the H2 section whose title begins with `title`, up to the next H2 or the end."""
    found = re.search(rf"(?m)^## {re.escape(title)}(?=[(\s]|$)", text)
    if found is None:
        return None
    following = re.search(r"(?m)^## ", text[found.end():])
    return found.start(), found.end() + following.start() if following else len(text)


def _mirror_plan(root) -> list[tuple[Path, str | None, str | None, str | None]]:
    """(target, its text now, its text with the section copied in, problem) for each mirrored section.

    Nothing is planned for a copy or a root outside a git working tree: no monorepo stands above them.
    """
    root = Path(root).resolve()
    mono = monorepo_of(root)
    if mono is None or is_deploy_copy(root) or in_plugin_cache(root):
        return []
    plans = []
    for source_rel, title, target_rel in MIRRORED:
        target = root / target_rel
        source_text, target_text = _read_text(mono / source_rel), _read_text(target)
        if source_text is None:
            continue  # a working tree without that document is not the monorepo the section lives in
        source_span = _section_span(source_text, title)
        if target_text is None or source_span is None:
            where = target_rel.as_posix() if target_text is None else f"모노레포의 {source_rel.as_posix()}"
            plans.append((target, target_text, None, f"{where}에서 '{title}' 절을 찾지 못해 옮기지 않았습니다."))
            continue
        section = source_text[source_span[0]:source_span[1]].rstrip("\n")
        span = _section_span(target_text, title)
        if span is None:
            after = target_text.rstrip("\n") + "\n\n\n" + section + "\n"
        else:
            old = target_text[span[0]:span[1]]
            after = target_text[:span[0]] + section + (old[len(old.rstrip("\n")):] or "\n") + target_text[span[1]:]
        plans.append((target, target_text, after, None))
    return plans


def _mode_for(target: Path) -> int:
    """The POSIX mode of the file replace_text puts at target.

    A regular file there keeps its own mode. Nothing there, or a link (which is replaced, not
    followed), gets the mode of a new file, 0o666 less the umask, as open() gives one. os.umask
    returns the umask only by setting another, so it is set to 0 and put back at once; a file that
    another thread creates in between would get the wider mode.
    """
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        info = None
    if info is not None and stat.S_ISREG(info.st_mode):
        return stat.S_IMODE(info.st_mode)
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def rename_into_place(fresh: Path, target: Path) -> None:
    """os.replace(fresh, target), tried again REPLACE_WAIT_SEC later while it raises PermissionError, REPLACE_TRIES
    times in all; the last PermissionError is raised then. Any other error is raised at once. fresh stays where it
    is when this raises: deleting it is the caller's.
    """
    for tried in range(1, REPLACE_TRIES + 1):
        try:
            os.replace(fresh, target)
            return
        except PermissionError:
            if tried == REPLACE_TRIES:
                raise
        time.sleep(REPLACE_WAIT_SEC)


def replace_text(target: Path, text: str) -> None:
    """Write text (UTF-8, LF) to a temporary file of its own beside target, then rename it over target.

    The mirrored document is rewritten by every run as soon as the monorepo's section changes, so
    several runs started together write it at once. With one fixed temporary name (target.name +
    ".new") one run could empty that file while another was still writing it, or rename it away
    first. Half a document then took target's place, where a run could read it as its system prompt
    or build its own rewrite on it, losing the sections around the mirrored one for good; the run
    whose file was gone failed with FileNotFoundError. The topic record is written the same way
    (tools/ledger.py), so that two exports into one brand folder at once cannot do that to it.

    The temporary file's name has the shape TEMP_NAME, which deploy leaves out (_is_temp). A target
    that is not there yet is created. A rename refused with PermissionError is tried again a moment
    later (rename_into_place). If any step fails, the error is raised after one try at deleting the
    temporary file; when that try fails too the file stays, and only the first error is raised. Text
    UTF-8 cannot hold (half of a surrogate pair) is such a failure, a UnicodeEncodeError, and target is
    left as it was.
    """
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=target.parent,
                                         prefix=f".{target.name}.", suffix=TEMP_SUFFIX, delete=False)
    fresh = Path(handle.name)
    try:
        with handle:
            handle.write(text)
        # On POSIX the temporary file is created owner-only (0600), so it is given the mode the file
        # at target is to have. Windows has only a read-only attribute, and copying it would make
        # this file undeletable if the rename failed.
        if os.name != "nt":
            os.chmod(fresh, _mode_for(target))
        rename_into_place(fresh, target)
    except BaseException:
        with contextlib.suppress(OSError):
            fresh.unlink()
        raise


def sync_mirrored(root) -> list[dict]:
    """Copy each mirrored section in where it differs. [{"path", "changed", "problem"}].

    A changed target is rewritten by replace_text, so a run reading it meets the old text or the
    whole new one. An OSError from the rewrite is raised for the caller to report.
    """
    results = []
    for target, before, after, problem in _mirror_plan(root):
        changed = problem is None and after != before
        if changed:
            replace_text(target, after)
        results.append({"path": target.as_posix(), "changed": changed, "problem": problem})
    return results


def stale_mirrored(root) -> list[Path]:
    """Targets whose mirrored section no longer matches the monorepo's."""
    return [target for target, before, after, problem in _mirror_plan(root) if problem is None and after != before]
