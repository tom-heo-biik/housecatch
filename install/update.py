"""install/update.py

하우스캐치 블로그 도구를 GitHub main의 최신 판으로 맞춥니다. 표준 라이브러리만 쓰고, 설치 폴더의 임베디드 파이썬에서 돕니다.

  python/python.exe -X utf8 install/update.py [--skill-dir DIR] [--zip-path ZIP] [--sha SHA] [--verbose]

설치 폴더는 이 파일의 위치(install 폴더의 부모)로 알아냅니다. 최신 판 번호가 .housecatch-version과 다르면 GitHub 압축을
받아 복사 규칙대로 덮고, /housecatch 스킬(install/skill/SKILL.md의 {{ROOT}}를 설치 폴더로 바꾼 것)을 다시 쓴 뒤 판 기록을
고칩니다. 결과는 한 줄로 찍고, 어떤 실패에도 종료 코드 0으로 끝냅니다(글쓰기를 막지 않습니다).
--zip-path와 --sha는 시험용입니다. 받은 것처럼 쓸 압축 파일과 그 판 번호를 줍니다.
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import traceback
import urllib.request
import zipfile
from pathlib import Path

REPO = "tom-heo-biik/housecatch"
API_URL = f"https://api.github.com/repos/{REPO}/commits/main"
ZIP_URL = "https://codeload.github.com/" + REPO + "/zip/{sha}"
AGENT = "housecatch-update"
VERSION_FILE = ".housecatch-version"

# 복사 규칙. install/install.ps1의 $KeepPaths, $MirrorDirs와 같아야 합니다.
#   보존(KEEP): 설치 폴더에 이미 있으면 덮지 않습니다.
#   미러(MIRROR): 원본에 없는 파일을 설치 폴더에서 지웁니다(__pycache__는 둡니다).
#   나머지: 덮어씁니다.
KEEP = ("블로그", "python", "durumi/runs", "durumi/brands/housecatch/topics.json",
        "durumi/brands/housecatch/topics-done.json", VERSION_FILE)
MIRROR = ("durumi/tools", "durumi/nodes", "durumi/rules", "durumi/context", "install", ".claude/hooks")
CACHE = "__pycache__"

LATEST = "최신 판입니다({now})"
UPDATED = "새 판으로 업데이트했습니다({old} → {new})"
NO_CHECK = "업데이트를 확인하지 못해 지금 판으로 씁니다"
NOT_DONE = "업데이트를 마치지 못해 지금 판으로 씁니다"
DEV_COPY = "개발 폴더(.git)라 업데이트하지 않습니다"

SHA = re.compile(r"[0-9a-f]{40}")
VERBOSE = False


def note(error: BaseException) -> None:
    if VERBOSE:
        traceback.print_exception(error, file=sys.stderr)


def short(sha: str) -> str:
    if not sha:
        return "기록 없음"
    return sha[:7] if SHA.fullmatch(sha) else sha


def fetch_latest() -> str:
    request = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        sha = str(json.load(response)["sha"])
    if not SHA.fullmatch(sha):
        raise ValueError(f"판 번호를 읽지 못했습니다: {sha!r}")
    return sha


def download(sha: str, dest: Path) -> Path:
    request = urllib.request.Request(ZIP_URL.format(sha=sha), headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)
    return dest


def unpack(zip_path: Path, into: Path) -> Path:
    """압축을 풀고, GitHub 압축 맨 위의 housecatch-<판> 폴더 한 겹을 벗긴 경로를 돌려줍니다."""
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(into)
    entries = list(into.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return into


def under(rel: str, root: str) -> bool:
    rel, root = rel.casefold(), root.casefold()
    return rel == root or rel.startswith(root + "/")


def in_cache(parts: tuple[str, ...]) -> bool:
    return any(part.casefold() == CACHE for part in parts)


def make_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass


def apply_rules(src: Path, dst: Path) -> None:
    # 보존 여부는 복사 전에 한 번만 봅니다. 처음 설치에서 블로그/의 첫 파일을 놓은 뒤 나머지를 건너뛰지 않게 하려는 것입니다.
    kept = [keep for keep in KEEP if (dst / keep).exists()]
    for dirpath, dirnames, filenames in os.walk(src):
        here = Path(dirpath)
        rel_dir = here.relative_to(src)
        dirnames[:] = [name for name in dirnames
                       if name.casefold() != CACHE and not (rel_dir == Path(".") and name.casefold() == "dist")]
        for name in filenames:
            rel = (rel_dir / name).as_posix()
            if any(under(rel, keep) for keep in kept):
                continue
            source, target = here / name, dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if filecmp.cmp(source, target, shallow=False):
                    continue
                make_writable(target)
            shutil.copy2(source, target)
    for mirror in MIRROR:
        top = dst / mirror
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [name for name in dirnames if name.casefold() != CACHE]
            for name in filenames:
                path = Path(dirpath) / name
                if not (src / path.relative_to(dst)).is_file():
                    make_writable(path)
                    path.unlink()
        # 원본에 없는 빈 폴더를 깊은 것부터 지웁니다. 미러 폴더 자체도 원본에 없고 비었으면 지웁니다.
        for dirpath, _dirnames, _filenames in os.walk(top, topdown=False):
            path = Path(dirpath)
            rel = path.relative_to(dst)
            if in_cache(rel.parts) or (src / rel).is_dir():
                continue
            try:
                path.rmdir()
            except OSError:
                pass


def ensure_blog(root: Path) -> None:
    for name in ("발행 전", "발행 완료"):
        (root / "블로그" / name).mkdir(parents=True, exist_ok=True)


def write_skill(root: Path, skill_dir: Path) -> None:
    template = root / "install" / "skill" / "SKILL.md"
    text = template.read_bytes().decode("utf-8-sig").replace("{{ROOT}}", root.as_posix())
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_bytes(text.encode("utf-8"))


def run(argv: list[str]) -> str:
    global VERBOSE
    parser = argparse.ArgumentParser(description="하우스캐치 블로그 도구를 GitHub main의 최신 판으로 맞춥니다.")
    parser.add_argument("--skill-dir", type=Path, default=Path.home() / ".claude" / "skills" / "housecatch",
                        help="/housecatch 스킬 폴더(기본 %%USERPROFILE%%\\.claude\\skills\\housecatch)")
    parser.add_argument("--zip-path", type=Path, help="시험용. 받은 것처럼 쓸 압축 파일")
    parser.add_argument("--sha", help="시험용. 최신 판 번호(주면 GitHub API를 부르지 않습니다)")
    parser.add_argument("--verbose", action="store_true", help="실패 까닭을 stderr에 찍습니다")
    args = parser.parse_args(argv)
    VERBOSE = args.verbose

    root = Path(os.path.abspath(__file__)).parent.parent
    version_file = root / VERSION_FILE
    # 개발 레포에서 잘못 돌려 커밋하지 않은 작업을 덮지 않게 합니다. 설치본에는 판 기록이 있습니다.
    if (root / ".git").exists() and not version_file.exists():
        return DEV_COPY
    current = version_file.read_text(encoding="utf-8-sig").strip() if version_file.is_file() else ""

    try:
        latest = args.sha or fetch_latest()
    except Exception as error:
        note(error)
        return NO_CHECK
    if current == latest:
        return LATEST.format(now=short(latest))

    with tempfile.TemporaryDirectory(prefix="housecatch-update-", ignore_cleanup_errors=True) as work:
        try:
            zip_path = args.zip_path or download(latest, Path(work) / "housecatch.zip")
            source = unpack(zip_path, Path(work) / "x")
            if not (source / "install" / "skill" / "SKILL.md").is_file():
                raise FileNotFoundError("받은 판에 install/skill/SKILL.md가 없습니다")
        except Exception as error:
            note(error)
            return NO_CHECK
        try:
            apply_rules(source, root)
            ensure_blog(root)
            write_skill(root, args.skill_dir)
            # 판 기록은 맨 끝에 고칩니다. 중간에 멈추면 다음 실행이 다시 받습니다.
            version_file.write_bytes(latest.encode("utf-8"))
        except Exception as error:
            note(error)
            return NOT_DONE
    return UPDATED.format(old=short(current), new=short(latest))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        line = run(sys.argv[1:])
    except SystemExit as stop:  # argparse가 끝낼 때(--help는 0)
        if stop.code in (0, None):
            return 0
        line = NO_CHECK
    except BaseException as error:  # 어떤 실패도 글쓰기를 막지 않습니다.
        note(error)
        line = NOT_DONE
    print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
