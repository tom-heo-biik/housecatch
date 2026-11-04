"""tools/package.py

하우스캐치 블로그 도구를 압축 파일 하나로 묶습니다. 레포 맨 위 install.ps1(irm | iex)을 쓸 수 없을 때의 예비 경로이고,
받는 쪽은 압축을 풀고 설치하기.cmd를 두 번 누릅니다. 표준 라이브러리만 씁니다(Python 3.11 이상).

  python tools/package.py [--out DIR] [--python-version 3.13.17] [--cache-dir DIR]
  python tools/package.py --hash-term 낱말      (누출 검사 목록에 더할 값을 찍습니다)

1. 레포 폴더를 스테이징(<out>/staging/housecatch, 매번 새로)에 복사합니다. 받는 쪽 폴더가 퍼블릭 레포와 이어지도록 .git도
   넣습니다. git이 있으면 git이 올렸거나 올릴 파일(무시 목록 밖)을, 없으면 폴더를 훑은 파일을 넣고, 어느 쪽이든 dist/,
   python/, __pycache__/, *.pyc, durumi/runs/는 뺍니다. 브랜드 파일은 무시 목록에 들어가더라도 작업 트리에 있으면 넣습니다.
2. 윈도우 임베디드 파이썬을 레포 밖 캐시 폴더(기본 %LOCALAPPDATA%\\housecatch-build-cache)에 한 번만 받아 python/에 풉니다.
3. 판 기록(.housecatch-version)에 HEAD 커밋을 적습니다. install/update.py가 이 값으로 새 판이 나왔는지 봅니다.
4. 인코딩 규칙(install/install.ps1은 BOM 있음, 맨 위 install.ps1은 BOM 없음, 설치하기.cmd는 ASCII)을 확인하고 누출 검사를 합니다.
   다른 고객 이름이나 개발 PC 경로가 보이면 파일과 줄을 찍고 압축 없이 멈춥니다(.git은 보지 않음).
5. <out>/housecatch-YYYYMMDD-HHMM.zip으로 묶고(맨 위 폴더 housecatch), 경로, 크기, 파일 수, 브랜드 파일 유무를 찍습니다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PYTHON = "3.13.17"
# python.org가 함께 올린 sigstore 묶음에 적힌 SHA256입니다(install/install.ps1의 값과 같음).
KNOWN_SHA256 = {"3.13.17": "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"}
EMBED_URL = "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip"
VERSION_FILE = ".housecatch-version"
BRAND_FILES = ("durumi/brands/housecatch/brand.md", "durumi/brands/housecatch/topics.json",
               "durumi/brands/housecatch/topics-done.json")
SKIP_NAMES = {"__pycache__", "thumbs.db", ".ds_store"}

# 누출 검사 낱말. 이 파일도 퍼블릭 레포에 오르므로 다른 고객 이름과 개발 PC 경로를 글자로 적지 않고,
# 소문자로 맞춘 낱말의 길이와 SHA-256 앞 16자리만 둡니다. 낱말을 더하려면 --hash-term으로 값을 얻어 넣고,
# 그 낱말의 첫 글자(소문자)를 LEAK_FIRSTS에 더합니다.
LEAK_HASHES = frozenset({
    (3, "ab1f3d32f3d69f82"), (6, "768d714f96b8e7eb"), (8, "e9f22bd85689e373"), (9, "952944e980579cc6"),
    (2, "c4074ad5271e6422"), (4, "69a50ccc9b8077c3"), (2, "2b6d753ca45723cf"), (6, "0a43feda695ae4d8"),
    (6, "0b4321c5d3de8b2a"), (6, "2dea7d422bf0fa68"), (7, "7e81195690b73bb6"), (5, "4a77d58e0b501a64"),
})
LEAK_FIRSTS = frozenset("bchks수카칼함")
LEAK_LENGTHS = tuple(sorted({length for length, _ in LEAK_HASHES}))


def say(text: str = "") -> None:
    print(text, flush=True)


def fail(text: str) -> None:
    say(f"실패: {text}")
    sys.exit(1)


def term_hash(folded: str) -> str:
    return hashlib.sha256(folded.encode("utf-8")).hexdigest()[:16]


def remove_tree(path: Path) -> None:
    def unlock(func, target, _info):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=unlock)
        else:
            shutil.rmtree(path, onerror=unlock)


def git(*args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return done.stdout.decode("utf-8", "replace")


def skipped(rel: str, out_rel: str | None) -> bool:
    parts = rel.split("/")
    lowered = [part.casefold() for part in parts]
    if any(part in SKIP_NAMES for part in lowered) or lowered[-1].endswith(".pyc"):
        return True
    if lowered[0] in ("dist", "python", ".git") or rel == VERSION_FILE:
        return True
    if rel.startswith("durumi/runs/"):
        return True
    if out_rel and (rel == out_rel or rel.startswith(out_rel + "/")):
        return True
    return False


def list_files(out_dir: Path) -> tuple[list[str], str]:
    try:
        out_rel = out_dir.resolve().relative_to(REPO).as_posix()
    except ValueError:
        out_rel = None
    listed = git("-c", "core.quotepath=false", "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed is not None:
        candidates = [rel for rel in listed.split("\0") if rel]
        method = "git이 올렸거나 올릴 파일"
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(REPO):
            here = Path(dirpath)
            dirnames[:] = [name for name in dirnames if not skipped((here / name).relative_to(REPO).as_posix(), out_rel)]
            candidates += [(here / name).relative_to(REPO).as_posix() for name in filenames]
        # git이 없을 때도 .gitignore가 막는 생성물은 넣지 않습니다.
        candidates = [rel for rel in candidates
                      if not (rel.startswith("블로그/") and not rel.endswith("/.gitkeep"))
                      and not re.match(r"durumi/brands/[^/]+/blogs/", rel)]
        method = "폴더를 훑은 파일(git 없음)"
    files = {rel for rel in candidates if (REPO / rel).is_file() and not skipped(rel, out_rel)}
    files |= {rel for rel in BRAND_FILES if (REPO / rel).is_file()}
    return sorted(files), method


def stage(staging: Path, files: list[str]) -> None:
    remove_tree(staging)
    staging.mkdir(parents=True)
    for rel in files:
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)
    if (REPO / ".git").is_dir():
        shutil.copytree(REPO / ".git", staging / ".git")


def usable_python_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return "python.exe" in archive.namelist() and archive.testzip() is None
    except (OSError, zipfile.BadZipFile):
        return False


def fetch_python(version: str, cache_dir: Path) -> tuple[Path, str]:
    cached = cache_dir / f"python-{version}-embed-amd64.zip"
    if not (cached.is_file() and usable_python_zip(cached)):
        url = EMBED_URL.format(v=version)
        try:
            urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30).close()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                fail(f"python.org에 파이썬 {version}의 윈도우 임베디드 압축이 없습니다. {url}")
            raise
        cache_dir.mkdir(parents=True, exist_ok=True)
        part = cached.with_name(cached.name + ".part")
        say(f"파이썬({version})을 받습니다. {url}")
        with urllib.request.urlopen(url, timeout=300) as response, part.open("wb") as out:
            shutil.copyfileobj(response, out)
        if not usable_python_zip(part):
            part.unlink(missing_ok=True)
            fail(f"받은 파이썬 압축이 깨졌습니다. {url}")
        part.replace(cached)
    digest = hashlib.sha256(cached.read_bytes()).hexdigest()
    expected = KNOWN_SHA256.get(version)
    if expected and digest != expected:
        cached.unlink(missing_ok=True)
        fail(f"캐시의 파이썬 압축 SHA256이 python.org 공개 값과 다릅니다({digest}). 캐시에서 지웠으니 다시 실행해 주세요.")
    return cached, digest


def read_text(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def leaks_in(text: str) -> list[int]:
    """누출 낱말이 시작하는 자리(소문자로 맞춘 글의 위치)를 돌려줍니다."""
    folded = text.casefold()
    found = []
    for index, char in enumerate(folded):
        if char not in LEAK_FIRSTS:
            continue
        for length in LEAK_LENGTHS:
            piece = folded[index:index + length]
            if len(piece) == length and (length, term_hash(piece)) in LEAK_HASHES:
                found.append(index)
                break
    return found


def leak_check(staging: Path) -> list[str]:
    hits = []
    for dirpath, dirnames, filenames in os.walk(staging):
        here = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (here == staging and name == ".git")]
        for name in filenames:
            path = here / name
            rel = path.relative_to(staging).as_posix()
            if leaks_in(rel):
                hits.append(f"{rel} (파일 이름)")
            text = read_text(path)
            if text is None:
                continue
            folded = text.casefold()
            lines = text.splitlines()
            for index in leaks_in(text):
                number = folded.count("\n", 0, index) + 1
                line = lines[number - 1].strip() if number <= len(lines) else ""
                hits.append(f"{rel}:{number}: {line[:160]}")
    return hits


def encoding_problems(staging: Path) -> list[str]:
    """Windows PowerShell 5.1과 cmd가 읽을 수 있는 꼴인지 봅니다."""
    bom = b"\xef\xbb\xbf"
    problems = []
    inner = staging / "install" / "install.ps1"
    if inner.is_file() and not inner.read_bytes().startswith(bom):
        problems.append("install/install.ps1에 BOM이 없습니다. Windows PowerShell 5.1이 한글을 잘못 읽어 멈춥니다.")
    root = staging / "install.ps1"
    if root.is_file() and root.read_bytes().startswith(bom):
        problems.append("맨 위 install.ps1에 BOM이 있습니다. irm | iex가 첫 줄을 명령 이름으로 읽어 멈춥니다.")
    cmd = staging / "설치하기.cmd"
    if cmd.is_file() and not cmd.read_bytes().isascii():
        problems.append("설치하기.cmd에 ASCII 밖의 글자가 있습니다. cmd가 콘솔 코드 페이지로 읽어 깨집니다.")
    return problems


def write_zip(staging: Path, zip_path: Path) -> None:
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9,
                         strict_timestamps=False) as archive:
        for dirpath, dirnames, filenames in os.walk(staging):
            dirnames.sort()
            here = Path(dirpath)
            prefix = (Path("housecatch") / here.relative_to(staging)).as_posix()
            archive.write(here, prefix + "/")  # 빈 폴더도 남도록 폴더 항목을 넣습니다.
            for name in sorted(filenames):
                archive.write(here / name, f"{prefix}/{name}")


def verify_zip(zip_path: Path) -> tuple[int, int]:
    files = 0
    with zipfile.ZipFile(zip_path) as archive:
        if archive.testzip() is not None:
            fail("만든 압축 파일을 다시 읽다가 깨진 항목을 찾았습니다.")
        for info in archive.infolist():
            if not info.filename.startswith("housecatch/"):
                fail(f"맨 위 폴더가 housecatch가 아닌 항목이 있습니다. {info.filename}")
            if not info.filename.isascii() and not info.flag_bits & 0x800:
                fail(f"한글 이름에 UTF-8 표시가 없습니다. {info.filename}")
            if not info.is_dir():
                files += 1
        return files, len(archive.infolist())


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if sys.version_info < (3, 11):
        fail("Python 3.11 이상이 필요합니다.")
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    parser = argparse.ArgumentParser(description="하우스캐치 블로그 도구를 설치하기.cmd용 압축 파일로 묶습니다.")
    parser.add_argument("--out", type=Path, default=REPO / "dist", help="압축 파일을 둘 폴더(기본 레포의 dist/)")
    parser.add_argument("--python-version", default=DEFAULT_PYTHON, help=f"임베디드 파이썬 판(기본 {DEFAULT_PYTHON})")
    parser.add_argument("--cache-dir", type=Path, default=local / "housecatch-build-cache",
                        help="받은 파이썬을 둘 캐시 폴더(기본 %%LOCALAPPDATA%%\\housecatch-build-cache)")
    parser.add_argument("--hash-term", help="누출 검사 목록에 더할 낱말의 값을 찍고 끝냅니다")
    args = parser.parse_args()

    if args.hash_term:
        folded = args.hash_term.casefold()
        say(f'({len(folded)}, "{term_hash(folded)}")  첫 글자 {folded[0]!r}')
        return 0
    if not re.fullmatch(r"3\.\d+\.\d+", args.python_version):
        fail(f"파이썬 판을 3.13.17 꼴로 적어 주세요. {args.python_version}")

    out = args.out.resolve()
    staging = out / "staging" / "housecatch"
    files, method = list_files(out)
    say(f"레포 {REPO}")
    say(f"넣을 파일 {len(files)}개({method}), .git 폴더 {'넣음' if (REPO / '.git').is_dir() else '없음'}")
    stage(staging, files)

    head = (git("rev-parse", "--verify", "-q", "HEAD") or "").strip()
    if re.fullmatch(r"[0-9a-f]{40}", head):
        (staging / VERSION_FILE).write_bytes(head.encode("utf-8"))
        dirty = bool((git("status", "--porcelain") or "").strip())
        version_note = head[:7] + (" (커밋하지 않은 변경도 들어감)" if dirty else "")
    else:
        version_note = "없음(커밋이 없거나 git이 없음). 설치 뒤 첫 업데이트가 GitHub 판을 받습니다."

    python_zip, digest = fetch_python(args.python_version, args.cache_dir)
    with zipfile.ZipFile(python_zip) as archive:
        archive.extractall(staging / "python")

    problems = encoding_problems(staging)
    if problems:
        for problem in problems:
            say(f"  {problem}")
        fail("인코딩 규칙에 어긋난 파일이 있습니다. 압축 파일은 만들지 않았습니다.")

    hits = leak_check(staging)
    if hits:
        say("누출 검사에 걸렸습니다. 아래를 고친 뒤 다시 묶어 주세요.")
        for hit in hits:
            say(f"  {hit}")
        fail(f"{len(hits)}곳에서 다른 고객 이름이나 개발 PC 경로를 찾았습니다. 압축 파일은 만들지 않았습니다.")

    zip_path = out / f"housecatch-{dt.datetime.now():%Y%m%d-%H%M}.zip"
    write_zip(staging, zip_path)
    file_count, entry_count = verify_zip(zip_path)
    size = zip_path.stat().st_size

    say()
    say(f"압축 파일 {zip_path}")
    say(f"크기 {size / 1024 / 1024:.1f}MB({size:,}바이트), 파일 {file_count:,}개(폴더 항목까지 {entry_count:,}개)")
    say(f"판 기록 {version_note}")
    say(f"파이썬 {args.python_version}, 캐시 {python_zip}, SHA256 {digest}")
    present = [rel for rel in BRAND_FILES if (staging / rel).is_file()]
    missing = [rel for rel in BRAND_FILES if rel not in present]
    say("브랜드 파일 " + ", ".join(f"{Path(rel).name} {'있음' if rel in present else '없음'}" for rel in BRAND_FILES))
    if missing:
        say("경고: 브랜드 파일이 빠졌습니다. 이대로 보내면 하우스캐치 글을 쓰지 못합니다. " + ", ".join(missing))
    say("누출 검사 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
