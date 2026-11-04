"""durumi command line.

  python tools/durumi.py preflight
  python tools/durumi.py check <request_file|-> [--brand id] [--deliver brand|session]
  python tools/durumi.py run <request_file|-> [--brand id] [--deliver brand|session]
  python tools/durumi.py render <envelope.json> [--brand id] [--out file]
  python tools/durumi.py lint <envelope.json> [--brand id]
  python tools/durumi.py check-docs [paths...]
  python tools/durumi.py measure <envelope.json> [--brand id]
  python tools/durumi.py doctor
  python tools/durumi.py show <run_id|latest>
  python tools/durumi.py topics <brand> [--write | --release ID [--confirmed-dead] | --import-cards]
  python tools/durumi.py deploy [--out DIR]
  python tools/durumi.py sync-skill
  python tools/durumi.py plan <brand> [--count N]
  python tools/durumi.py batch <brand> [--count N] [--parallel N] [--outbox DIR] [--topic TEXT ...]
  python tools/durumi.py export --brand id --out DIR

Every command also takes --root DIR (the durumi root itself; the run commands preflight prints
carry it, so a run uses the root preflight found) and --project DIR (a Claude Code project folder
to look for projects/durumi in; the skill passes ${CLAUDE_PROJECT_DIR}). See config.find_root.
--python py or --python python3 makes the commands preflight and check print start with that name
instead of python; the skill passes it when python itself cannot run (pipeline.PYTHONS).

Exit codes: 0 success, 1 failure, 2 format errors, 3 brand not decided, 4 no topic,
5 node error, 6 environment error.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if (TOOLS.parent / ".durumi-source.json").is_file():  # config.SOURCE_MARKER: this is a deploy copy
    # A deploy is swapped in by renaming the whole folder; a .pyc written in that moment would bring
    # the old folder name back to life, so code running from a copy writes no bytecode.
    sys.dont_write_bytecode = True

MIN_PYTHON = (3, 11)  # tomllib; the python3 that macOS ships with its developer tools is 3.9
# The skill starts even when preflight could not finish; with no check command to run, the model must
# relay the report and stop rather than guess commands or open the tools to fix them.
STOP_NOTICE = "확인 명령이 없으니 원인을 찾으려고 명령을 실행하거나 파일을 읽지 말고, 이 안내를 사용자에게 그대로 전한 뒤 이번 요청을 끝내십시오."


def python_too_old(version=None, executable=None) -> str | None:
    """Why this interpreter cannot run durumi, or None. Checked before any tool module is imported."""
    version = tuple(sys.version_info[:3] if version is None else version)
    if version >= MIN_PYTHON:
        return None
    have, need = (".".join(str(part) for part in v) for v in (version, MIN_PYTHON))
    return (f"- 두루미는 파이썬 {need} 이상에서 돌아가는데 찾은 파이썬은 {have}입니다({executable or sys.executable}). "
            "python.org나 Homebrew(brew install python)로 설치한 뒤 /durumi를 다시 불러 주십시오.")


# The skill injects preflight under python, then py, then python3, each with its stderr dropped, and
# ends with an echo that explains when none of them could run it. An interpreter that is too old
# exits non-zero so the next one gets its turn, and says why on stderr, where the skill never shows
# it: a newer interpreter later in the chain must not find a refusal printed above its report.
_TOO_OLD = python_too_old()
if _TOO_OLD:
    print(_TOO_OLD, file=sys.stderr)
    sys.exit(6)

try:
    import claims  # noqa: E402
    from config import (Brand, ConfigError, find_root, in_plugin_cache, is_deploy_copy, is_durumi_root,  # noqa: E402
                        list_brands, load_brand, load_rules)
    from doccheck import check_doc, format_report, iter_markdown  # noqa: E402
    from measure import measure  # noqa: E402
    from naming import now_kst  # noqa: E402
    import deploy  # noqa: E402
    import ledger  # noqa: E402
    from node import NodeError, _envelope_from, find_claude, probe  # noqa: E402
    from normalize import normalize_envelope  # noqa: E402
    import pipeline  # noqa: E402
    import topics  # noqa: E402
    import batch  # noqa: E402
    import export  # noqa: E402
    import plan  # noqa: E402
except Exception as exc:  # a broken tool module; preflight still answers instead of handing over to py or python3
    if __name__ != "__main__" or sys.argv[1:2] != ["preflight"]:
        raise
    print(f"### 두루미 준비 점검\n\n- 도구를 불러오지 못했습니다. {type(exc).__name__}: {exc}\n- {STOP_NOTICE}")
    sys.exit(0)

EXIT_FAIL = 1
DELIVER_HELP = ("완성한 글을 어디에 둘지. brand는 brands/{브랜드 id}/blogs/에 저장하고 session은 브랜드 폴더에 "
                "저장하지 않고 요약 끝에 찍습니다(실행 폴더에는 어느 쪽이든 남습니다). 없으면 루트를 찾은 방식으로 "
                "정합니다(모노레포 안이면 brand, --root만 있으면 명령을 돌린 폴더가 모노레포 안인지로)")
REQUESTS_TEMP_DIR = "durumi-requests"
# The topics command's own sentences; the lines of its table are tools/ledger.py's. None prints a topic's
# words, and a topic id is printed only when it is one a claim file can be named by (topics.usable_id).
NO_TOPIC_FILE = "주제 파일 topics.json이 없습니다."
NOT_AN_ID = "쓸 수 없는 주제 id입니다. 영문 소문자나 숫자로 시작하고 영문 소문자, 숫자, 하이픈만 32자까지 씁니다."
WRITE_IN_COPY = "배포본에서는 주제 기록을 쓰지 않습니다. 모노레포에서 돌려 주십시오."
WRITE_STOPPED = "주제 기록 topics-done.json을 읽지 못해 내보내기를 멈췄습니다. 파일은 그대로 두었습니다."  # the reason follows
RELEASE_UNCHECKED = "주제 기록 topics-done.json을 읽지 못해 작성됨으로 적혀 있는지는 보지 않았습니다."  # the reason follows
# --release, for a reserved topic whose last run the topic record has as written: the run folder was removed,
# and set free the topic would be written a second time.
RELEASE_WRITTEN = "주제 {id}번은 주제 기록에 작성됨으로 적혀 있어 풀지 않습니다. 실행 폴더가 없어 예약됨으로 보이는 것입니다."
# --release without --confirmed-dead, of a reservation younger than claims.RUN_MAY_LIVE (three hours) or whose time
# cannot be read (claims.MayBeRunning): its run may still be writing, and set free the topic would be written twice.
# Sessions started together each see the others' runs as reservations with no finished record, and may run this.
RELEASE_TOO_SOON = ("주제 {id}번은 예약한 지 3시간이 지나지 않아 아직 쓰는 중일 수 있습니다. 실행이 죽은 것을 사람이 직접 "
                    "확인했을 때만 --confirmed-dead를 붙여 다시 지시해 주십시오. 확인하지 못했다면 3시간이 지난 뒤에 풉니다.")
CONFIRMED_DEAD_ALONE = "--confirmed-dead는 --release와 함께 씁니다."  # argparse's error, with the topics usage
# --release done. A topic that is not approved does not go out until it is, set free or not.
RELEASED = "주제 {id}번의 예약을 풀었습니다. 다음 배정에서 다시 나갑니다."
RELEASED_NOT_APPROVED = "주제 {id}번의 예약을 풀었습니다. 승인 전 주제라 승인한 뒤에 다시 나갑니다."
SHARED_WITH_POSTS = "글과 함께 커밋해 팀과 나눕니다."  # --write, after the line that says where the record was written
# --import-cards done (claims.Imported): the four counts, then each card claim file not copied on a line of its own,
# after the words its count was said with. Card claim file names are made of a lap and a hash, never of a topic's words.
IMPORTED = ("옛 카드 예약을 옮겼습니다. 새로 만든 예약 {made}건, 이미 옮겨 둔 예약 {there}건, 옮기지 못한 예약 {lost}건, "
            "새 예약이 이미 있어 옮기지 않은 예약 {blocked}건.")
NOT_IMPORTED = "- 옮기지 못한 예약 {name}"  # lost: no topic has its sentence, or its record cannot be read
NOT_IMPORTED_BESIDE_A_CLAIM = "- 새 예약이 이미 있어 옮기지 않은 예약 {name}"  # blocked


def _reconfigure_streams() -> None:
    # Line buffering: a run started in the background writes to a file or pipe, where Python would
    # otherwise hold every line until the end. The run folder line printed at the start must show at once.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", line_buffering=True)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _load_envelope(path: Path):
    """An envelope JSON, or a raw node output (draft.out.json) holding one, normalized as a run does.

    The pipeline undoes a doubly escaped body before judging (normalize.py), so lint, render and
    measure do too, and say so on stderr; otherwise they would judge a node's raw output differently.
    """
    try:
        data = json.loads(Path(path).read_bytes().decode("utf-8-sig"))
    except OSError as exc:
        raise ConfigError(f"봉투 파일을 읽지 못했습니다. {path} ({exc})") from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigError(f"봉투 파일이 UTF-8 JSON이 아닙니다. {path} ({exc})") from exc
    if isinstance(data, dict) and "body" not in data and ("structured_output" in data or "result" in data):
        inner = _envelope_from(data)
        if inner is not None:
            data = inner
    data, notes = normalize_envelope(data)
    for note in notes:
        _err(f"알림 {note}")
    return data


def _brand(root: Path, brand_id: str | None):
    if brand_id:
        return load_brand(root, brand_id)
    return pipeline.neutral_brand(root)


def _print_issues(issues) -> None:
    for issue in issues:
        mark = {"error": "오류", "warn": "경고", "info": "알림"}.get(issue.severity, issue.severity)
        print(f"{mark} {pipeline.format_issue(issue)}")


def cmd_preflight(root, args) -> int:
    """Always exits 0 with a report on standard output.

    The skill injects preflight under python, py and python3 in turn with stderr dropped, so a
    failing preflight would hand over to the next interpreter (on Windows python3 is usually the
    Microsoft Store stub) and hide its own error.
    """
    try:
        text = pipeline.preflight(root, kind=getattr(args, "root_kind", None), python=args.python)
    except Exception as exc:  # noqa: BLE001 - reported in the output instead of raised
        text = f"### 두루미 준비 점검\n\n- 준비 점검이 도중에 멈췄습니다. {type(exc).__name__}: {exc}\n- {STOP_NOTICE}\n"
    print(text, end="")
    return 0


def save_stdin_request(data: bytes, now=None, base=None) -> Path:
    """Save a request read from standard input into the OS temp folder.

    The request never goes inside a plugin folder: Claude Code refuses file writes there, and the
    pipeline keeps its own copy in the run folder anyway. Each file is created exclusively, so two
    runs started in the same second never write into one file.
    """
    folder = Path(base) if base is not None else Path(tempfile.gettempdir()) / REQUESTS_TEMP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now_kst(now).strftime("%Y%m%d-%H%M%S")
    n = 1
    while True:
        path = folder / (f"{stamp}.txt" if n == 1 else f"{stamp}-{n}.txt")
        try:
            with open(path, "xb") as fh:
                fh.write(data)
            return path
        except FileExistsError:
            n += 1


def _request_path(args) -> Path | None:
    """The request file named on the command line, or standard input saved to a file (for -).

    None means standard input was empty; the caller reports it.
    """
    if args.request_file != "-":
        return Path(args.request_file)
    stream = getattr(sys.stdin, "buffer", None)
    data = stream.read() if stream is not None else sys.stdin.read().encode("utf-8")
    if not data.strip():
        _err("표준 입력으로 받은 요청 원문이 비었습니다.")
        return None
    path = save_stdin_request(data)
    print(f"요청 원문을 저장했습니다. {path.as_posix()}", flush=True)
    return path


def _deliver(root, args) -> str:
    """--deliver as given, else decided from how the root was found (pipeline.delivery_for)."""
    return args.deliver or pipeline.delivery_for(root, getattr(args, "root_kind", None))


def cmd_check(root, args) -> int:
    path = _request_path(args)
    if path is None:
        return pipeline.EXIT_TOPIC
    return pipeline.check(root, path, brand_id=args.brand, deliver=_deliver(root, args), python=args.python)


def cmd_run(root, args) -> int:
    path = _request_path(args)
    if path is None:
        return pipeline.EXIT_TOPIC
    return pipeline.run(root, path, brand_id=args.brand, root_kind=getattr(args, "root_kind", None),
                        deliver=_deliver(root, args), python=args.python)


def cmd_deploy(root, args) -> int:
    if is_deploy_copy(root):
        _err("배포본 안에서는 배포하지 않습니다. 원본 폴더의 tools/durumi.py로 실행해 주십시오.")
        return EXIT_FAIL
    try:
        claude, _ = find_claude()
    except NodeError:
        claude = None
    validate = (lambda folder: deploy.validate_with(claude, folder)) if claude else None
    if not _sync_mirrored(root):  # the copy carries the monorepo's current sections
        return EXIT_FAIL
    try:
        summary = deploy.build(root, args.out, validate=validate)
    except deploy.DeployError as exc:
        _err(str(exc))
        return EXIT_FAIL
    out = summary["out"]
    print(f"배포본을 만들었습니다. {out}")
    print(f"- {summary['name']} {summary['version']}, 파일 {summary['files']}개, {summary['bytes']:,}바이트")
    print(f"- 내용 해시 {summary['content_sha256'][:12]}" + ("" if summary["changed"] else " (지난 배포와 같음)"))
    print("- 검증 claude plugin validate로 마켓플레이스와 플러그인을 확인한 뒤 바꿔 끼웠습니다." if validate
          else "- 검증 claude를 찾지 못해 claude plugin validate는 건너뛰었습니다.")
    print("- 처음 한 번은 아래 두 명령으로 마켓플레이스를 더하고 설치합니다.")
    print(f'  claude plugin marketplace add "{out}"')
    print("  claude plugin install durumi@durumi --scope user")
    print("- 그다음부터는 deploy만 다시 실행하면 됩니다. 로컬 마켓플레이스의 플러그인은 설치할 때 캐시에 사본이 생겨도 "
          "실행할 때는 배포본을 제자리에서 읽어서, 다음 세션부터 새 배포본이 쓰입니다.")
    print("- 모노레포 안에서는 설치하지 않아도 /durumi를 쓸 수 있습니다. 모노레포 스킬은 sync-skill이 만듭니다.")
    return 0


def _sync_mirrored(root) -> bool:
    """Copy the monorepo's mirrored sections in (deploy.MIRRORED) and say what happened. False on failure."""
    try:
        results = deploy.sync_mirrored(root)
    except OSError as exc:
        _err(f"모노레포 CLAUDE.md의 절을 옮기지 못했습니다. {exc}")
        return False
    for result in results:
        if result["problem"]:
            _err(result["problem"])
        elif result["changed"]:
            print(f"모노레포 CLAUDE.md의 절을 옮겼습니다. {result['path']}")
    return not any(result["problem"] for result in results)


def cmd_sync_skill(root, args) -> int:
    mirrored = _sync_mirrored(root)
    try:
        result = deploy.sync_project_skill(root)
    except (deploy.DeployError, OSError) as exc:
        _err(f"모노레포 스킬을 만들지 못했습니다. {exc}")
        return EXIT_FAIL
    if not result["changed"]:
        print(f"모노레포 스킬이 이미 플러그인 스킬과 같습니다. {result['path']}")
    else:
        print(f"모노레포 스킬을 {'만들었습니다' if result['created'] else '다시 만들었습니다'}. {result['path']}")
        print("- 커밋해서 올리면 팀원은 pull만 받고 다음 세션에서 /durumi를 쓸 수 있습니다(파이썬 3.11 이상과 네이티브 "
              "claude가 있으면 됩니다. npm의 claude.cmd는 쓰지 않고, VS Code 확장에 든 claude는 찾아 씁니다).")
    return 0 if mirrored else EXIT_FAIL


def cmd_render(root, args) -> int:
    rules = load_rules(root)
    verdict = pipeline.check_envelope(_load_envelope(Path(args.envelope)), rules, _brand(root, args.brand))
    if verdict.html is None:
        _err("원고를 HTML로 바꾸지 못했습니다.")
        for issue in verdict.issues:
            _err(pipeline.format_issue(issue))
        return pipeline.EXIT_CHECKS_FAILED
    if args.out:
        pipeline.write_text(Path(args.out), verdict.html)
        print(f"HTML을 저장했습니다. {Path(args.out).as_posix()}")
    else:
        sys.stdout.write(verdict.html)
    if not verdict.ok:
        _err("렌더는 했지만 형식 오류가 있습니다. lint 명령으로 확인해 주십시오.")
        return pipeline.EXIT_CHECKS_FAILED
    return 0


def cmd_lint(root, args) -> int:
    rules = load_rules(root)
    brand = _brand(root, args.brand)
    verdict = pipeline.check_envelope(_load_envelope(Path(args.envelope)), rules, brand)
    _print_issues(verdict.issues)
    errors = sum(i.severity == "error" for i in verdict.issues)
    others = len(verdict.issues) - errors
    note = "" if args.brand else " 브랜드를 정하지 않아 브랜드 금지 문자열은 보지 않았습니다."
    if verdict.ok:
        print(f"형식 판정 통과입니다. 경고와 알림 {others}건.{note}")
        return 0
    print(f"형식 오류 {errors}건, 경고와 알림 {others}건입니다.{note}")
    return pipeline.EXIT_CHECKS_FAILED


def cmd_check_docs(root, args) -> int:
    if args.paths:
        fixtures = (root / "tests" / "fixtures").resolve()
        files = []
        for given in args.paths:
            path = Path(given)
            found = iter_markdown([path])
            if path.is_dir():
                # Deliberately broken test fixtures are skipped when a folder is scanned, and so are
                # deploy copies (dist/marketplace/durumi), whose H1s name the monorepo paths they came from.
                found = [f for f in found if not f.resolve().is_relative_to(fixtures)
                         and not any(is_deploy_copy(folder) for folder in f.parents)]
            files += found
    elif is_deploy_copy(root) or pipeline.in_plugin_cache(root):
        print("사본이라 문서 검사를 건너뜁니다. 문서의 H1은 모노레포 기준 경로라서 사본에서는 맞지 않으니 원본 폴더에서 "
              "check-docs로 검사합니다.")
        return 0
    else:
        files = pipeline.default_doc_paths(root)
    if not files:
        print("검사할 문서가 없습니다.")
        return 0
    repo = pipeline.repo_root_of(root)
    failed = 0
    for path in files:
        report = check_doc(repo, path)
        print(format_report(report))
        failed += not report.ok
    print(f"문서 {len(files)}개 가운데 통과 {len(files) - failed}개, 문제 있음 {failed}개입니다.")
    return EXIT_FAIL if failed else 0


def cmd_measure(root, args) -> int:
    rules = load_rules(root)
    verdict = pipeline.check_envelope(_load_envelope(Path(args.envelope)), rules, _brand(root, args.brand))
    if verdict.doc is None:
        _err("원고를 파싱하지 못해 재지 못했습니다.")
        for issue in verdict.issues:
            _err(pipeline.format_issue(issue))
        return pipeline.EXIT_CHECKS_FAILED
    print(json.dumps(measure(verdict.doc, rules), ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(root, args) -> int:
    print(f"파이썬 {platform.python_version()} ({sys.executable})")
    print(f"루트 {root.as_posix()}")
    try:
        binary, version = find_claude()
    except NodeError as exc:
        print(str(exc))
        return pipeline.EXIT_ENV
    print(f"claude {version} ({Path(binary).as_posix()})")
    print("격리 점검을 시작합니다. 노드를 한 번 실제로 부르므로 비용이 조금 듭니다.")
    result = probe(binary, root=root)
    print(f"카나리아 노출 {'있음' if result.get('canary_leaked') else '없음'}, 입력 토큰 {result.get('input_tokens')}, "
          f"비용 {result.get('cost')}, 시간 {result.get('duration_sec')}초")
    if result.get("answer"):
        print("모델의 답")
        print(result["answer"])
    if result.get("ok"):
        print("격리 점검을 통과했습니다.")
        return 0
    print(f"격리 점검에 실패했습니다. {result.get('error', '')}")
    return pipeline.EXIT_ENV


def cmd_show(root, args) -> int:
    code, text = pipeline.show(root, args.run_id)
    if code:
        _err(text)
    else:
        sys.stdout.write(text)
    return code


def _topics_show(root: Path, brand: Brand, found: topics.TopicFile | None) -> int:
    """Print the brand's topics and their states (ledger.table), then what a person should know besides.

    The table ends by saying how a reserved topic is released, when one is (ledger.HOW_TO_RELEASE). What comes
    besides is how many topic ids hold claims the topic file does not list (ledger.unlisted), which topics aim
    at one keyword (topics.keyword_notes) and what keeps the topics from being handed out (topics.problems,
    under the head pipeline.load_topics gives them). None of it fails the command: the states are shown all
    the same. A brand without a topic file says so, then that its brand.toml still has topic_cards when it
    does (pipeline.LEGACY_CARDS, which stops its requests that hand the topic over), then the claims left
    under ids of a file that is gone; that is no failure either.
    """
    if found is None:
        try:
            notes = ledger.unlisted(root, brand, None)
        except (OSError, ValueError) as exc:
            _err(f"{pipeline.CLAIMS_UNREAD} {exc}")
            return pipeline.EXIT_ENV
        for line in (NO_TOPIC_FILE, *([pipeline.LEGACY_CARDS] if brand.legacy_cards else []), *notes):
            print(line)
        return pipeline.EXIT_OK
    try:
        rules = load_rules(root)  # the topics' words are judged by them (topics.problems)
    except (ConfigError, OSError) as exc:
        _err(f"설정을 읽지 못했습니다. {exc}")
        return pipeline.EXIT_ENV
    try:
        lines = [*ledger.table(root, brand, found), *ledger.unlisted(root, brand, found)]
    except (OSError, ValueError) as exc:  # the claims folder cannot be read, or the brand id cannot name one
        _err(f"{pipeline.CLAIMS_UNREAD} {exc}")
        return pipeline.EXIT_ENV
    problems = topics.problems(found, rules, brand.banned_phrases)
    for line in (*lines, *topics.keyword_notes(found)):
        print(line)
    if problems:
        print(pipeline.TOPICS_REFUSED_RULES if topics.COLON_RULE_UNREADABLE in problems else pipeline.TOPICS_REFUSED)
        for problem in problems:
            print(f"- {problem}")
    return pipeline.EXIT_OK


def _topics_write(root: Path, brand: Brand, found: topics.TopicFile | None) -> int:
    """Export the topic record (ledger.export) and say where it is and how many entries it holds.

    Then SHARED_WITH_POSTS, since the team reads the record through git beside the posts, and, when topic
    ids the file does not list hold claims, how many: the export writes their posts under the sentences
    their claims recorded (ledger.unlisted, read before the export so that claims that cannot be read stop
    it with nothing written). A record that cannot be read stops the export with its reason, and the file
    is left as it is. A record other exports kept changing meanwhile (ledger.KeptChanging) stops it with
    that sentence alone, since the file could be read.
    """
    try:
        notes = ledger.unlisted(root, brand, found)
        path, record = ledger.export(root, brand, found)
    except ledger.KeptChanging as exc:
        _err(str(exc))
        return pipeline.EXIT_ENV
    except ledger.LedgerError as exc:
        _err(WRITE_STOPPED)
        _err(f"- {exc}")
        return pipeline.EXIT_ENV
    except (OSError, ValueError) as exc:  # the claims or runs cannot be read, or the record cannot be written
        _err(f"주제 기록을 내보내지 못했습니다. {exc}")
        return pipeline.EXIT_ENV
    print(f"주제 기록을 썼습니다. {path.as_posix()} (주제 {len(record['topics'])}개, 목록 밖 주제 "
          f"{len(record['off_list'])}개)")
    print(SHARED_WITH_POSTS)
    for note in notes:
        print(note)
    return pipeline.EXIT_OK


def _topics_release(root: Path, brand: Brand, found: topics.TopicFile | None, topic_id: str,
                    confirmed_dead: bool = False) -> int:
    """Set one reserved topic free (claims.release), so that it goes out again; any other topic is refused.

    The id must be one of the topic file's. A topic in another state is refused with that state. So is a
    reserved topic whose last run the topic record has as written (ledger.written): its run folder was
    removed, and set free the topic would be written a second time; --confirmed-dead does not change that.
    A record that cannot be read is said in one line and that check is passed over, as it is without a
    record. A reservation younger than claims.RUN_MAY_LIVE, or whose time cannot be read, is refused with
    RELEASE_TOO_SOON unless confirmed_dead (--confirmed-dead). A topic that is not approved is set free
    alike, and told that it goes out once it is approved (RELEASED_NOT_APPROVED).
    """
    if found is None:
        _err(NO_TOPIC_FILE)
        return pipeline.EXIT_ENV
    if not topics.usable_id(topic_id):
        _err(NOT_AN_ID)  # it may hold anything, so it is not printed
        return pipeline.EXIT_ENV
    listed = next((topic for topic in found.topics if topic.id == topic_id), None)
    if listed is None:
        _err(f"주제 파일에 {topic_id}번이 없습니다.")
        return pipeline.EXIT_ENV
    try:
        entry = claims.entries(root, brand.id, [topic_id])[topic_id]
    except (OSError, ValueError) as exc:
        _err(f"{pipeline.CLAIMS_UNREAD} {exc}")
        return pipeline.EXIT_ENV
    if entry.state != claims.RESERVED:
        _err(f"주제 {topic_id}번은 풀 수 없습니다(지금 {claims.NAMES[entry.state]}). 예약됨인 주제만 풉니다.")
        return pipeline.EXIT_ENV
    try:
        record = ledger.read(brand.dir)
    except ledger.LedgerError as exc:
        _err(f"{RELEASE_UNCHECKED} {exc}")
        record = None
    if ledger.written(record, topic_id, entry.run):
        _err(RELEASE_WRITTEN.format(id=topic_id))
        return pipeline.EXIT_ENV
    try:
        claims.release(root, brand.id, topic_id, now=now_kst(), confirmed_dead=confirmed_dead)
    except claims.MayBeRunning:
        _err(RELEASE_TOO_SOON.format(id=topic_id))
        return pipeline.EXIT_ENV
    except ValueError as exc:  # no longer reserved: its run ended or another release came first
        _err(str(exc))
        return pipeline.EXIT_ENV
    except OSError as exc:
        _err(f"주제 {topic_id}번의 예약을 풀지 못했습니다. {exc}")
        return pipeline.EXIT_ENV
    print((RELEASED if listed.approved else RELEASED_NOT_APPROVED).format(id=topic_id))
    return pipeline.EXIT_OK


def _topics_import(root: Path, brand: Brand, found: topics.TopicFile | None) -> int:
    """Copy the claims of the old topic cards into topic claims (claims.import_cards) and say how many (IMPORTED).

    The four counts are the claims made, those an earlier import had made, the card claims no topic took or
    whose record cannot be read (lost), and those of a topic that holds a claim a run made (blocked), whose
    state that claim keeps. The lost and the blocked are then named by their files (NOT_IMPORTED,
    NOT_IMPORTED_BESIDE_A_CLAIM). The claims go where the root's runs go (config.runs_root), which for a
    deploy copy is the home folder's ~/.durumi/runs: the brand folder is not written, so a copy imports as
    the working tree does. cmd_topics has refused a topic file whose ids cannot be used before this is
    reached; should claims.import_cards refuse one all the same, its sentence is shown.
    """
    if found is None:
        _err(NO_TOPIC_FILE)
        return pipeline.EXIT_ENV
    try:
        got = claims.import_cards(root, brand.id, found)
    except ValueError as exc:  # a topic file or a brand id the claims refuse; nothing was made
        _err(str(exc))
        return pipeline.EXIT_ENV
    except OSError as exc:
        _err(f"옛 카드 예약을 옮기지 못했습니다. {exc}")
        return pipeline.EXIT_ENV
    print(IMPORTED.format(made=got.made, there=got.there, lost=len(got.lost), blocked=len(got.blocked)))
    for name in got.lost:
        print(NOT_IMPORTED.format(name=name))
    for name in got.blocked:
        print(NOT_IMPORTED_BESIDE_A_CLAIM.format(name=name))
    return pipeline.EXIT_OK


def cmd_topics(root, args) -> int:
    """topics BRAND [--write | --release ID [--confirmed-dead] | --import-cards]: a brand's topics and their fate.

    Without a direction the states are shown (_topics_show). --write exports the topic record
    (_topics_write), --release sets a reserved topic free (_topics_release), with --confirmed-dead also one
    whose run may still be writing, and --import-cards copies the claims of the old topic cards
    (_topics_import); argparse takes one of the three at most, and --confirmed-dead only with --release
    (main). A brand that is not registered exits with 3. Anything else that stops the command exits with 6,
    its sentence on standard error: a topic file that cannot be read; a topic id in it that cannot be used or
    comes twice, which stops all four uses alike with pipeline.TOPICS_REFUSED and the sentences of
    topics.id_problems, since ids are written as they stand and claims are read and made by them; and
    --write in a deploy copy or a plugin cache copy, whose brand folder is emptied by the next deploy or
    update.
    """
    try:
        brands = list_brands(root)
    except ConfigError as exc:
        _err(f"설정을 읽지 못했습니다. {exc}")
        return pipeline.EXIT_ENV
    brand = next((b for b in brands if b.id == args.brand), None)
    if brand is None:
        _err(pipeline.unknown_brand(args.brand, brands))  # the value only when it is a brand id's shape
        return pipeline.EXIT_BRAND
    if args.write and (is_deploy_copy(root) or in_plugin_cache(root)):
        _err(WRITE_IN_COPY)
        return pipeline.EXIT_ENV
    try:
        found = topics.load(brand.dir)
    except topics.TopicFileError as exc:
        _err(str(exc))
        return pipeline.EXIT_ENV
    about_ids = topics.id_problems(found.topics) if found is not None else []
    if about_ids:
        _err(pipeline.TOPICS_REFUSED)
        for problem in about_ids:
            _err(f"- {problem}")
        return pipeline.EXIT_ENV
    if args.release is not None:  # an empty id is still a direction, and is refused as an id
        return _topics_release(root, brand, found, args.release, args.confirmed_dead)
    if args.import_cards:
        return _topics_import(root, brand, found)
    if args.write:
        return _topics_write(root, brand, found)
    return _topics_show(root, brand, found)


def _brand_named(root, brand_id: str):
    """(brand, exit code): the registered brand of that id, or None with EXIT_BRAND after saying so."""
    try:
        brands = list_brands(root)
    except ConfigError as exc:
        _err(f"설정을 읽지 못했습니다. {exc}")
        return None, pipeline.EXIT_ENV
    brand = next((b for b in brands if b.id == brand_id), None)
    if brand is None:
        _err(pipeline.unknown_brand(brand_id, brands))
        return None, pipeline.EXIT_BRAND
    return brand, pipeline.EXIT_OK


def cmd_plan(root, args) -> int:
    """plan BRAND [--count N]: the plan node writes N new topics into the brand's topics.json."""
    brand, code = _brand_named(root, args.brand)
    if brand is None:
        return code
    try:
        made = plan.generate(root, brand, max(1, args.count), log=print)
    except (ConfigError, NodeError, OSError, ValueError) as exc:
        _err(f"주제를 정하지 못했습니다. {exc}")
        return pipeline.EXIT_ENV
    for topic_id, topic in made:
        print(f"- {topic_id} {topic}")
    print(f"새 주제 {len(made)}개를 topics.json에 더했습니다.")
    return pipeline.EXIT_OK if made else pipeline.EXIT_TOPIC


def cmd_batch(root, args) -> int:
    """batch BRAND [--count N] [--parallel N] [--outbox DIR]: several posts at once (tools/batch.py)."""
    brand, code = _brand_named(root, args.brand)
    if brand is None:
        return code
    try:
        return batch.batch(root, brand, count=max(1, args.count), parallel=args.parallel, outbox=args.outbox,
                           given=args.topic, log=lambda line: print(line, flush=True))
    except (ConfigError, NodeError, OSError, ValueError) as exc:
        _err(f"글을 쓰지 못했습니다. {exc}")
        return pipeline.EXIT_ENV


def cmd_export(root, args) -> int:
    """export --brand ID --out DIR: one brand's copy of durumi for its own repository (tools/export.py)."""
    try:
        done = export.export(root, args.brand, args.out)
    except (export.ExportError, deploy.DeployError, OSError, ValueError) as exc:
        _err(f"내보내지 못했습니다. {exc}")
        return pipeline.EXIT_ENV
    print(f"내보냈습니다. {done['out'].as_posix()} (파일 {done['files']}개, 이미 쓴 주제 {done['unapproved']}개는 "
          f"배정에서 뺌, 원본 {done['commit'] or '알 수 없음'})")
    return pipeline.EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="두루미 루트 폴더. 준비 점검이 찍는 실행 명령에 들어 있습니다")
    common.add_argument("--project", help="Claude Code 프로젝트 폴더. 그 안의 projects/durumi를 루트로 씁니다")
    common.add_argument("--python", choices=pipeline.PYTHONS, default=pipeline.DEFAULT_PYTHON,
                        help="찍는 명령을 시작할 파이썬 이름. python으로 돌리지 못해 py나 python3로 돌 때 스킬이 넘깁니다")
    parser = argparse.ArgumentParser(prog="durumi", description="두루미 네이버 블로그 파이프라인")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    add("preflight", help="실행 전 준비 점검").set_defaults(func=cmd_preflight)
    p = add("check", help="노드를 부르지 않고 요청의 브랜드와 주제를 판정해 실행 명령을 알려 줍니다")
    p.add_argument("request_file", help="요청 파일 경로, 또는 표준 입력으로 받으려면 -")
    p.add_argument("--brand", help="브랜드 id. 요청에서 브랜드를 하나로 정하지 못했을 때 사람이 고른 값")
    p.add_argument("--deliver", choices=pipeline.DELIVERIES, help=DELIVER_HELP)
    p.set_defaults(func=cmd_check)
    p = add("run", help="요청 하나로 초안, 퇴고, 최종본까지 만듭니다")
    p.add_argument("request_file", help="요청 파일 경로, 또는 표준 입력으로 받으려면 -")
    p.add_argument("--brand", help="브랜드 id. 요청에서 브랜드를 하나로 정하지 못했을 때 사람이 고른 값")
    p.add_argument("--deliver", choices=pipeline.DELIVERIES, help=DELIVER_HELP)
    p.set_defaults(func=cmd_run)
    for name, func, text in (("render", cmd_render, "봉투를 HTML로 바꿉니다"),
                             ("lint", cmd_lint, "봉투의 형식을 판정합니다"),
                             ("measure", cmd_measure, "봉투의 지표를 잽니다")):
        p = add(name, help=text)
        p.add_argument("envelope")
        p.add_argument("--brand", help="브랜드 id(고정 팔레트와 금지 문자열에 씀)")
        if name == "render":
            p.add_argument("--out", help="HTML을 저장할 파일. 없으면 표준 출력")
        p.set_defaults(func=func)
    p = add("check-docs", help="비익 표준 양식 검사")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_check_docs)
    add("doctor", help="claude 위치와 판, 격리 점검").set_defaults(func=cmd_doctor)
    p = add("deploy", help="실행에 필요한 파일만 담은 배포본을 로컬 마켓플레이스로 만듭니다")
    p.add_argument("--out", help="배포 폴더. 없으면 두루미 폴더의 dist/marketplace")
    p.set_defaults(func=cmd_deploy)
    add("sync-skill", help="플러그인 스킬로 모노레포 스킬(/durumi)을 다시 만들고, 모노레포 CLAUDE.md에서 두루미 문서가 "
                           "옮겨 싣는 절(context/writing.md의 적확한 표현)을 옮깁니다").set_defaults(func=cmd_sync_skill)
    p = add("show", help="지난 실행의 보고서")
    p.add_argument("run_id", help="runs 아래 폴더 이름 또는 latest")
    p.set_defaults(func=cmd_show)
    p = add("topics", help="브랜드의 주제마다 상태를 보여 주고, 지시하면 주제 기록을 내보내거나 예약을 풀거나 옛 카드 예약을 옮깁니다")
    p.add_argument("brand", help="브랜드 id")
    directions = p.add_mutually_exclusive_group()  # one at most
    directions.add_argument("--write", action="store_true",
                            help="주제 기록 topics-done.json을 내보냅니다. 배포본에서는 쓰지 않습니다")
    directions.add_argument("--release", metavar="ID", help="예약됨인 주제 하나의 예약을 풀어 다음 배정에서 다시 나가게 합니다")
    directions.add_argument("--import-cards", action="store_true", help="옛 주제 카드의 예약을 주제 예약으로 옮깁니다")
    p.add_argument("--confirmed-dead", action="store_true",
                   help="--release와 함께 씁니다. 실행이 죽은 것을 사람이 확인했을 때, 예약한 지 3시간이 지나지 않은 예약도 풉니다")
    p.set_defaults(func=cmd_topics, usage_error=p.error)  # main refuses --confirmed-dead without --release with it
    p = add("plan", help="주제 노드가 지난 주제와 겹치지 않는 새 주제를 정해 topics.json에 더합니다")
    p.add_argument("brand", help="브랜드 id")
    p.add_argument("--count", type=int, default=batch.DEFAULT_COUNT, help="정할 주제의 수")
    p.set_defaults(func=cmd_plan)
    p = add("batch", help="주제 고르기 없이 한 브랜드의 글 여러 편을 한꺼번에 씁니다. 모자란 주제는 주제 노드가 정합니다")
    p.add_argument("brand", help="브랜드 id")
    p.add_argument("--count", type=int, default=batch.DEFAULT_COUNT, help="쓸 글의 수(기본 5)")
    p.add_argument("--parallel", type=int, help="한꺼번에 돌릴 실행의 수(기본은 글의 수)")
    p.add_argument("--outbox", help="끝난 글을 옮길 폴더. 없으면 브랜드의 blogs 폴더에 둡니다")
    p.add_argument("--topic", action="append", help="사람이 정한 주제. 여러 번 쓰면 주제마다 한 편씩 쓰고 --count는 보지 않습니다")
    p.set_defaults(func=cmd_batch)
    p = add("export", help="한 브랜드만 담은 두루미 사본을 그 브랜드의 레포 폴더로 내보냅니다")
    p.add_argument("--brand", required=True, help="브랜드 id")
    p.add_argument("--out", required=True, help="사본을 채울 폴더(그 안의 runs 폴더는 그대로 둡니다)")
    p.set_defaults(func=cmd_export)
    return parser


def resolve_root(args, env=None) -> tuple[Path, str]:
    """(root, kind). --root must name a durumi root; otherwise find_root decides, --project first."""
    if args.root:
        root = Path(args.root)
        if not is_durumi_root(root):
            raise ConfigError(f"--root가 두루미 루트가 아닙니다(.claude-plugin/plugin.json의 name이 durumi여야 함): {root}")
        return root.resolve(), "given"
    return find_root(env, project=args.project)


def main(argv=None, env=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "confirmed_dead", False) and args.release is None:
        args.usage_error(CONFIRMED_DEAD_ALONE)  # the topics command's argparse error: exit code 2, nothing read
    try:
        root, args.root_kind = resolve_root(args, env)
        return args.func(root, args)
    except ConfigError as exc:
        _err(str(exc))
        return pipeline.EXIT_ENV


if __name__ == "__main__":
    _reconfigure_streams()
    sys.exit(main())
