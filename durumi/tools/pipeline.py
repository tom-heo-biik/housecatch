"""The durumi run: request -> draft node -> checks and repairs -> revise node -> checks -> report.

Code does the assembling, converting, judging, measuring and saving. The two nodes only write.
Nothing here rewrites a word the nodes or people wrote: failed checks go back to the same node
as a repair request.

Exit codes of run():
  0 success, 2 checks failed after the repair limit (outputs kept), 3 brand not decided,
  4 no topic, 5 node error, 6 environment error (also: the rendered HTML failed the lint_html
  safety net or the renderer failed, which the node cannot repair; a topic file that cannot be
  used or has no topic to give, or card claims not imported yet, before the run folder is made,
  or a topic that could not be claimed, after it; and any unexpected error after the run folder
  was made, with the report still written)

Importing this module has no side effects.
"""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import platform
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import claims
import topics
from assemble import assemble, prepare_document
from config import (SELF_ROOT, Brand, ConfigError, Rules, find_root, in_plugin_cache, is_deploy_copy,
                    list_brands, load_node, load_rules, runs_root)
from deploy import code_differs, default_out, rename_into_place, stale_mirrored, stale_project_skill, sync_mirrored
from doccheck import check_paths
from document import ImageSlot, LinkBlock, LinkSlot
from factcheck import factcheck, markdown_lines as factcheck_lines, summary_line as factcheck_summary
from issues import ERROR, WARN, Issue, has_errors
from lint import lint_document, lint_html
from markup import parse
from measure import compare, measure
from normalize import normalize_envelope
from present import as_markdown
from naming import BRAND_ID, RUN_NAME, blog_name, final_name, now_kst, run_id, unique_dir
from node import NodeError, NodeResult, call_node, envelope_schema, find_claude, input_tokens, removed_env_names
from render import RenderError, render
from resolve import TOPIC_HANDED_OVER, TOPIC_MISSING, resolve_brand, topic_kind

EXIT_OK = 0
EXIT_CHECKS_FAILED = 2
EXIT_BRAND = 3
EXIT_TOPIC = 4
EXIT_NODE = 5
EXIT_ENV = 6

STAGES = (("draft", "초안"), ("revise", "퇴고"))
STAGE_LABEL = dict(STAGES)
REPAIR_HEAD = "앞선 원고에서 다음 문제가 발견되었습니다."
# A request that hands the topic over to a brand with a topic file gets one approved topic of it
# (brands/{id}/topics.json, claimed with tools/claims.py), and the nodes are told it beside the
# request as written (assigned_request, revise_request). The heads say what this post is about and
# why, and nothing of other posts: a node is isolated. The revise head says what the tool did, that
# the draft node was given the topic too, and not that the draft kept to it, which the tool does not
# check. They still call the topics 주제 카드, as when brand.toml listed them: the nodes read them,
# so they are kept letter for letter.
ASSIGNED_HEAD = ("다음은 요청이 주제 고르기를 맡겨서 두루미가 이 브랜드의 주제 카드에서 이번 글에 배정한 주제입니다. 이 주제로 써 "
                 "주십시오.")
REVISE_ASSIGNED_HEAD = ("다음은 두루미가 이 브랜드의 주제 카드에서 이번 글에 배정한 주제입니다. 초안 노드에도 이 주제를 주었습니다. "
                        "퇴고에서도 이 주제를 지켜 주십시오.")
# The head above the sentences of topics.problems (load_topics). The second one is said when the
# colon rule itself does not compile (topics.COLON_RULE_UNREADABLE), which is mended in rules/form.toml
# and not in the topic file; each sentence under it names what it is about.
TOPICS_REFUSED = "주제 파일을 쓸 수 없습니다. topics.json을 고쳐 주십시오."
TOPICS_REFUSED_RULES = "주제 파일을 쓸 수 없습니다. 아래 문제를 고쳐 주십시오."
# brand.toml's topic_cards are no longer read (config.Brand.legacy_cards): until they are moved to
# topics.json and the key is taken out, the brand's requests that hand the topic over stop (load_topics).
LEGACY_CARDS = "brand.toml의 topic_cards는 이제 쓰지 않습니다. 주제를 topics.json으로 옮기고 topic_cards를 지워 주십시오."
# No topic to give (claims.UsedUp). check and run say it before anything starts; a run whose claim
# finds the last topics taken by runs started since says it in its report and summary (claim_topic).
TOPICS_USED_UP = ("주제가 모자랍니다. topics.json의 승인된 주제를 모두 썼거나 예약했습니다. 주제를 더하거나 approved를 "
                  "true로 바꿔 주십시오.")
# Added to the report and the summary of a run whose topic had been written before (claims.Claim.times
# above 1). claims.assign hands out a topic left or failed before any other, so a topic added to
# topics.json goes out before one is given again.
TOPIC_REUSED = "주제를 다 써서, 가장 오래전에 쓴 주제부터 다시 배정했습니다. topics.json에 주제를 더하면 새 주제가 먼저 나갑니다."
CLAIM_FAILED = "주제를 예약하지 못했습니다."  # the error follows it, in the report and the summary (claim_topic)
CLAIMS_UNREAD = "주제 예약 기록을 읽지 못했습니다."  # check, and run when card claims cannot be read, before the error; exit EXIT_ENV
# check and run, before anything starts, while n claims the topic cards left in runs/_cards wait to be imported
# (claims.pending_cards): the claims folder does not know of them, so a topic they wrote would go out again, and an
# import after a new claim of that topic would no longer copy them (claims.import_cards). The line after it is the
# topics command with --import-cards, whole (cards_pending).
CARDS_PENDING = ("아직 옮기지 않은 옛 주제 카드 예약이 {n}건 있습니다. 옮기기 전에 글을 쓰면 이미 쓴 주제가 다시 "
                 "나갑니다. 아래 명령으로 먼저 옮겨 주십시오.")
# How check names the topic of a request that gets one (topic_line). free is the approved topics
# LEFT or FAILED, count the approved topics less those RESERVED now (never below 0) and name
# brand.name. check and run stop with TOPICS_USED_UP instead when free is 0 under stop, and when no
# topic is approved under either mode.
CHECK_STOP = ("주제는 실행할 때 도구가 {name} 주제 파일에서 아직 쓰지 않은 주제를 적힌 순서대로 정합니다(남은 주제 {free}개). "
              "같은 runs 폴더를 쓰는 실행은 동시에 {free}개까지 띄워도 서로 다른 주제를 받습니다. 요청이 주제 고르기를 맡겼습니다.")
CHECK_REUSE = ("주제는 실행할 때 도구가 {name} 주제 파일에서 아직 쓰지 않은 주제나 가장 오래전에 쓴 주제로 정합니다(남은 주제 "
               "{free}개). 같은 runs 폴더를 쓰는 실행은 동시에 {count}개까지 띄워도 서로 다른 주제를 받습니다. 요청이 주제 "
               "고르기를 맡겼습니다.")
# check, right after CHECK_REUSE when free is 0: the post gets a topic given before.
CHECK_REUSE_NEXT = "남은 주제가 없어 이번 글은 이미 쓴 주제를 다시 받습니다."
# check, after the topic line and the keyword notes, or after TOPICS_USED_UP, when n approved topics are
# RESERVED: a run is writing them, or left no result that can be read (it died, or its folder was
# removed), and they are held until a person releases them. The line after it is the topics command it
# names, whole (topics_command), and the table that command prints says how one is released
# (ledger.HOW_TO_RELEASE).
CHECK_RESERVED = "끝난 기록이 없는 예약이 {n}개 있습니다. 쓰는 중인 글이 아니라면 topics 명령으로 확인해 주십시오."
NO_STAGES = "형식 판정 없음(노드를 부르기 전에 멈췄습니다)"  # summary_lines, for a run that never reached the draft node
RENDERER_STOP = ("렌더 결과가 HTML 안전망(lint_html)에 걸렸거나 렌더에 실패했습니다. 노드가 고칠 수 없는 오류라 수리 요청을 "
                 "보내지 않고 멈췄습니다. 렌더러 결함이나 규칙 파일끼리의 불일치를 확인해 주십시오.")


# Formatting elements whose count the revise node may change, for the report (measure keys).
FORM_COUNTS = (("quotes", "인용", "개"), ("images", "이미지 자리", "곳"), ("links", "링크 자리", "곳"),
               ("subheadings", "소제목", "개"), ("bold", "굵게", "개"), ("color", "글자 색", "개"),
               ("highlight", "형광펜", "개"), ("list_items", "목록 항목", "개"),
               ("checklist_items", "체크리스트 항목", "개"), ("tables", "표", "개"))


# Small helpers


def write_text(path: Path, text: str) -> None:
    """UTF-8, LF, no BOM (write_bytes avoids newline translation on Windows).

    Written beside the file and renamed over it, so a failed write (a full disk) never leaves half a
    file under the real name for the report or a person to take as finished. A rename refused with
    PermissionError, as Windows may refuse one while another process reads the file (the topics command
    and the claims read a run's meta.json), is tried again a moment later (deploy.rename_into_place); not
    measured on Windows. When it still fails, the part file is deleted and the error raised.
    """
    path = Path(path)
    part = path.with_name(path.name + ".part")
    try:
        part.write_bytes(text.encode("utf-8"))
        rename_into_place(part, path)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def write_json(path: Path, data) -> None:
    """JSON as readable UTF-8; a lone surrogate from a node (UTF-8 cannot hold one) falls back to \\u escapes."""
    try:
        write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except UnicodeEncodeError:
        write_text(path, json.dumps(data, ensure_ascii=True, indent=2) + "\n")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_request(path: Path) -> tuple[bytes, str]:
    """Raw bytes and text. Only the BOM and line endings are normalized; words stay as written."""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8")
    if text.startswith("﻿"):
        text = text[1:]
    return raw, text.replace("\r\n", "\n").replace("\r", "\n")


def format_issue(issue: Issue) -> str:
    parts = [f"[{issue.code}]"]
    if issue.where:
        parts.append(issue.where)
    parts.append(issue.message)
    line = " ".join(parts)
    if issue.excerpt:
        line += f" (발췌 '{issue.excerpt}')"
    return line


def neutral_brand(root: Path) -> Brand:
    """A brand with no fixed palettes and no phrases, for render and lint without --brand."""
    base = Path(root) / "brands"
    return Brand(id="none", name="(브랜드 없음)", aliases=(), palettes=None, banned_phrases=(),
                 watch_phrases=(), dir=base, doc_path=base / "none" / "brand.md")


def repo_root_of(root: Path) -> Path:
    """The git working tree holding `root` (H1 paths are relative to it), else root itself."""
    root = Path(root).resolve()
    for candidate in (root, *root.parents):
        if (candidate / ".git").exists():
            return candidate
    return root


def default_doc_paths(root: Path) -> list[Path]:
    """Human documents of the plugin: CLAUDE.md, context/*.md, brands/*/brand.md, skills/*/SKILL.md."""
    root = Path(root)
    paths = [root / "CLAUDE.md"] if (root / "CLAUDE.md").is_file() else []
    return paths + runtime_doc_paths(root)


def runtime_doc_paths(root: Path) -> list[Path]:
    """The documents a run reads: context/*.md, brands/*/brand.md, skills/*/SKILL.md.

    CLAUDE.md is left out: it guides whoever works on this folder, and no node or skill reads it.
    """
    root = Path(root)
    return (sorted((root / "context").glob("*.md")) + sorted((root / "brands").glob("*/brand.md"))
            + sorted((root / "skills").glob("*/SKILL.md")))


def placeholders(doc) -> dict:
    """Image and link slots a person fills after pasting."""
    images, links = [], []
    for block in doc.blocks:
        if isinstance(block, ImageSlot):
            images.append(block.desc)
        elif isinstance(block, LinkBlock):
            links.append(block.target)
        for seq in _inline_seqs(block):
            links.extend(node.target for node in seq if isinstance(node, LinkSlot))
    return {"images": images, "links": links}


def _inline_seqs(block) -> list:
    if hasattr(block, "inlines"):
        return [block.inlines]
    if hasattr(block, "lines"):
        return list(block.lines)
    if hasattr(block, "items"):
        return list(block.items)
    if hasattr(block, "header"):
        return list(block.header) + [cell for row in block.rows for cell in row]
    return []


# Checking one envelope


@dataclass
class Verdict:
    doc: object | None
    html: str | None
    issues: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.doc is not None and self.html is not None and not has_errors(self.issues)


def check_envelope(envelope, rules, brand) -> Verdict:
    """Parse, lint the tree, render and lint the HTML. Judges only; never edits the envelope."""
    doc, issues = parse(envelope, rules, brand)
    issues = list(issues)
    html = None
    if doc is not None:
        issues += lint_document(doc, rules, brand)
        try:
            html = render(doc, rules)
        except RenderError as exc:
            issues.append(Issue("R001", ERROR, f"원고를 HTML로 바꾸지 못했습니다. {exc}", "렌더"))
        if html is not None:
            issues += lint_html(html, rules)
    return Verdict(doc=doc, html=html, issues=issues)


def renderer_issue(issue: Issue) -> bool:
    """Errors about the rendered HTML (H0xx) or the renderer itself (R001).

    The HTML is made by the renderer from the rules, so these point at a renderer defect or rules
    that disagree with each other. A node cannot fix them by rewriting the manuscript.
    """
    return issue.severity == ERROR and (issue.code.startswith("H") or issue.code == "R001")


# Node requests


def repair_request(base_request: str, issues, envelope) -> str:
    errors = [issue for issue in issues if issue.severity == ERROR and not renderer_issue(issue)]
    lines = [base_request.rstrip("\n"), "", "", REPAIR_HEAD, ""]
    lines += [f"- {format_issue(issue)}" for issue in errors]
    lines += [
        "",
        "앞선 봉투는 아래와 같습니다. 위 문제를 모두 고친 봉투 전체를 같은 형식으로 다시 내 주십시오.",
        "",
        "```json",
        json.dumps(envelope, ensure_ascii=False, indent=2),
        "```",
    ]
    return "\n".join(lines) + "\n"


def assigned_request(request_text: str, topic: str) -> str:
    """The draft request of a run that claimed a topic: the request as written, then the topic.

    Repairs start from this request too (repair_request), so every draft attempt carries the topic.
    """
    return request_text.rstrip("\n") + "\n\n\n" + ASSIGNED_HEAD + "\n\n" + topic + "\n"


def revise_request(request_text: str, draft_envelope: dict, topic: str | None = None) -> str:
    """The person's request, the claimed topic if any, then the draft envelope.

    Only the person's words stand under the head that names them; the topic the tool added has a head
    of its own. Without a topic the request is the same, byte for byte, as before topics were assigned.
    """
    lines = [
        "다음은 사람이 보낸 요청 원문입니다.",
        "",
        request_text.strip("\n"),
        "",
        "",
    ]
    if topic is not None:
        lines += [REVISE_ASSIGNED_HEAD, "", topic, "", ""]
    lines += [
        "다음은 초안 노드가 낸 봉투입니다. 이 초안을 퇴고해 같은 형식의 봉투 하나로 내 주십시오.",
        "",
        "```json",
        json.dumps(draft_envelope, ensure_ascii=False, indent=2),
        "```",
    ]
    return "\n".join(lines) + "\n"


# Stages


@dataclass
class Stage:
    name: str
    ok: bool = False
    exit_code: int = EXIT_OK
    error: str = ""
    envelope: dict | None = None
    verdict: Verdict | None = None
    attempts: list = field(default_factory=list)
    results: list = field(default_factory=list)  # NodeResult per attempt

    @property
    def label(self) -> str:
        return STAGE_LABEL.get(self.name, self.name)


def _node_summary(result: NodeResult) -> dict:
    return {
        "ok": result.ok,
        "error": result.error,
        "returncode": result.returncode,
        "usage": result.usage,
        "input_tokens": input_tokens(result.usage or {}),
        "output_tokens": int((result.usage or {}).get("output_tokens") or 0),
        "cost": result.cost,
        "model_ids": list(result.model_ids),
        "duration_sec": result.duration_sec,
        "stream": dict(getattr(result, "stream", {}) or {}),
    }


def _takes_progress(call) -> bool:
    """True when the node callable accepts progress_path (the real call_node does; test fakes need not)."""
    try:
        return "progress_path" in inspect.signature(call).parameters
    except (TypeError, ValueError):
        return False


def run_stage(ctx: "RunContext", name: str, cfg, system_prompt: str, base_request: str, stages=None) -> Stage:
    """Call one node, check its envelope, and send repairs up to cfg.retries times.

    The stage is appended to `stages` first, so a partial stage survives an exception.
    """
    stage = Stage(name=name)
    if stages is not None:
        stages.append(stage)
    schema = envelope_schema(ctx.rules, ctx.brand)
    write_text(ctx.dir / f"{name}.system.md", system_prompt)
    write_json(ctx.dir / f"{name}.schema.json", schema)
    request = base_request
    for n in range(1, cfg.retries + 2):
        tag = f"{name}.{n}"
        write_text(ctx.dir / f"{tag}.request.txt", request)
        progress = f"{tag}.progress.log" if _takes_progress(ctx.call) else None
        extra = {"progress_path": ctx.dir / progress} if progress else {}
        result = ctx.call(ctx.binary, system_prompt, request, cfg, schema, **extra)
        stage.results.append(result)
        raw = result.raw if result.raw is not None else {"error": result.error, "stderr": result.stderr}
        write_json(ctx.dir / f"{tag}.out.json", raw)
        attempt = {"n": n, "request": f"{tag}.request.txt", "node": _node_summary(result)}
        if progress:
            attempt["progress"] = progress
        stage.attempts.append(attempt)
        if not result.ok or result.envelope is None:
            stage.exit_code = EXIT_NODE
            stage.error = result.error or "노드가 봉투를 내지 않았습니다."
            attempt["checks"] = None
            break
        # A transport slip (a doubly escaped body) is undone before judging; out.json keeps the original.
        envelope, notes = normalize_envelope(result.envelope)
        if notes:
            attempt["normalized"] = notes
        write_json(ctx.dir / f"{tag}.json", envelope)
        verdict = check_envelope(envelope, ctx.rules, ctx.brand)
        write_json(ctx.dir / f"{tag}.issues.json", [issue.to_dict() for issue in verdict.issues])
        if verdict.html is not None:
            write_text(ctx.dir / f"{tag}.html", verdict.html)
        stage.envelope, stage.verdict = envelope, verdict
        attempt["checks"] = {
            "ok": verdict.ok,
            "errors": sum(i.severity == ERROR for i in verdict.issues),
            "warnings": sum(i.severity == WARN for i in verdict.issues),
        }
        if verdict.ok:
            stage.ok = True
            break
        errors = [issue for issue in verdict.issues if issue.severity == ERROR]
        if errors and all(renderer_issue(issue) for issue in errors):
            # Only renderer-side errors are left. When manuscript errors come with them (an empty
            # paragraph is both L040 and H008), the repair goes ahead without the H lines.
            stage.exit_code = EXIT_ENV
            stage.error = RENDERER_STOP
            break
        if n <= cfg.retries:
            request = repair_request(base_request, verdict.issues, envelope)
    else:
        stage.exit_code = EXIT_CHECKS_FAILED
        stage.error = f"수리 요청을 {cfg.retries}번 보냈지만 형식 오류가 남았습니다."
    _write_canonical(ctx.dir, name, len(stage.attempts))
    return stage


def _write_canonical(folder: Path, name: str, last: int) -> None:
    """Copy the last attempt's files to the plain names (draft.json, draft.html, ...)."""
    for suffix in ("request.txt", "out.json", "json", "issues.json", "html"):
        source = folder / f"{name}.{last}.{suffix}"
        if source.is_file():
            (folder / f"{name}.{suffix}").write_bytes(source.read_bytes())


# Run


@dataclass
class RunContext:
    root: Path
    dir: Path
    rules: object
    brand: object
    binary: object
    call: object


# The interpreter name the printed commands start with. The skill runs preflight with python, and
# when that cannot run it (missing, too old, or the Microsoft Store stub) with py (the Windows
# launcher that python.org installs put on PATH by default) and then python3 (macOS and Linux),
# passing --python, so every later command uses the name that actually ran.
PYTHONS = ("python", "py", "python3")
DEFAULT_PYTHON = "python"


def root_kind_here(root: Path) -> str:
    """How this process would find root on its own (config.find_root), or 'given' when it would not."""
    try:
        found, kind = find_root()
        same = Path(found).resolve() == Path(root).resolve()
    except OSError:  # a current folder deleted under the shell, on POSIX
        return "given"
    return kind if same else "given"


# A brand value no registered brand has: check and run's --brand, and the topics command's brand (unknown_brand).
UNKNOWN_BRAND = "브랜드 id {id}는 등록되어 있지 않습니다. 등록된 브랜드는 {names}입니다."
NO_BRAND_ID = "브랜드 id로 쓸 수 없는 값입니다. 등록된 브랜드는 {names}입니다."


def unknown_brand(value: str, brands) -> str:
    """The sentence for a brand value no registered brand has, naming the registered ones ("없음" for none).

    The value is printed only when it is a brand id's shape in full (naming.BRAND_ID, UNKNOWN_BRAND): it comes
    from a command line and may hold anything, a line break or a terminal escape among it, so any other value
    is not printed (NO_BRAND_ID).
    """
    names = ", ".join(f"{b.name}({b.id})" for b in brands) or "없음"
    if isinstance(value, str) and BRAND_ID.fullmatch(value):
        return UNKNOWN_BRAND.format(id=value, names=names)
    return NO_BRAND_ID.format(names=names)


def decide(root, request_file, request_text: str, brands, brand_id, log, deliver=None,
           python=DEFAULT_PYTHON) -> tuple:
    """(brand, topic, exit code) for one request. EXIT_OK means a run can go ahead.

    Otherwise what the person should do has been logged: the --brand commands to pick from, or the
    missing topic. check and run both decide here, so they never disagree. The topic is the request
    as written, the brand's name included (Tom, 2026-10-02): the name is set aside only to judge
    whether a topic is there (resolve.topic_kind).
    """
    if brand_id:
        brand = next((b for b in brands if b.id == brand_id), None)
        if brand is None:
            log(unknown_brand(brand_id, brands))
            return None, "", EXIT_BRAND
        candidates = [brand]
    else:
        brand, candidates = resolve_brand(request_text, brands)
    if brand is None:
        # Several candidates, or none: either way the person picks, and the pick goes back as --brand
        # with the request file unchanged. The tool matches ids, names and aliases letter for letter,
        # so a spelling a person recognizes (a nickname, say) can still find nothing.
        pool = candidates or brands
        if not pool:
            log("요청에서 브랜드를 찾지 못했습니다. 등록된 브랜드가 없습니다. brands 폴더에 브랜드를 먼저 만들어 주십시오.")
            return None, "", EXIT_BRAND
        names = ", ".join(f"{b.name}({b.id})" for b in pool)
        if candidates:
            log(f"요청에서 브랜드를 하나로 정하지 못했습니다. 후보는 {names}입니다.")
        else:
            log(f"요청에서 브랜드를 찾지 못했습니다. 등록된 브랜드는 {names}입니다. "
                "도구는 브랜드 id, 이름, 별칭과 글자가 같을 때만 찾습니다.")
        log("사용자에게 어느 브랜드인지 확인한 뒤 아래 명령 가운데 하나로 다시 실행하십시오. 요청 파일은 그대로 씁니다.")
        for b in pool:
            log(f"- {b.name} {run_command(root, request_file, b.id, deliver, python)}")
        return None, "", EXIT_BRAND
    if topic_kind(request_text, brand) == TOPIC_MISSING:
        log(f"요청에 주제가 없습니다. {brand.name} 말고 무엇에 관한 글인지 사용자에게 묻고, "
            "받은 답을 요청 원문 다음 줄에 그대로 덧붙여 다시 실행하십시오.")
        return brand, "", EXIT_TOPIC
    return brand, request_text, EXIT_OK


def topic_due(topic: str, brand: Brand) -> bool:
    """True when a run of this request gets one of the brand's topics (load_topics, claim_topic).

    Only a request that hands the choice of topic over gets one (MGM, 2026-10-05). A request with one
    word of its own besides ('곰팡이 말고 아무거나') names a topic (resolve.topic_kind) and keeps it; that
    is judged first, so such a request does not look for the topic file at all. Then the brand must
    have a topics.json, whatever stands under that name (a folder there is a topic file that cannot be
    read), or a brand.toml that still has topic_cards, which load_topics stops at.
    """
    if topic_kind(topic, brand) != TOPIC_HANDED_OVER:
        return False
    return brand.legacy_cards or os.path.lexists(topics.path_of(brand.dir))


def load_topics(brand: Brand, rules: Rules, log: Callable[[str], object]) -> topics.TopicFile | None:
    """The brand's topic file, or None after logging why its topics cannot be handed out.

    Why, in this order: brand.toml still has topic_cards (LEGACY_CARDS, whatever topics.json holds);
    topics.json cannot be read (the sentence of topics.TopicFileError, and topics.NOT_OPENED for a file
    gone by the time it is read, or a link that leads nowhere); or topics.problems finds what keeps the
    topics from being handed out. The brand's banned phrases (brand.banned_phrases, lint L030) go along
    with the rules, and the sentences are logged under one head: TOPICS_REFUSED, or TOPICS_REFUSED_RULES
    when the colon rule of rules/form.toml does not compile (topics.COLON_RULE_UNREADABLE). check and
    run both ask this before anything starts, so check does not promise a topic that run would refuse.
    Only a request that will get a topic is asked (topic_due), so the brand's other requests and the
    other brands go on.
    """
    if brand.legacy_cards:
        log(LEGACY_CARDS)
        return None
    try:
        found = topics.load(brand.dir)
    except topics.TopicFileError as exc:
        log(str(exc))
        return None
    if found is None:
        log(topics.NOT_OPENED)
        return None
    problems = topics.problems(found, rules, brand.banned_phrases)
    if problems:
        log(TOPICS_REFUSED_RULES if topics.COLON_RULE_UNREADABLE in problems else TOPICS_REFUSED)
        for problem in problems:
            log(f"- {problem}")
        return None
    return found


def _used_up(found: topics.TopicFile, tally: dict[str, int]) -> bool:
    """True when claims.assign has no topic to give (claims.UsedUp); tally is claims.counts for found.

    That is a file without an approved topic, whatever when_used_up says, and under stop one whose
    approved topics are all RESERVED or WRITTEN.
    """
    if not any(listed.approved for listed in found.topics):
        return True
    return found.when_used_up == topics.STOP and tally[claims.LEFT] + tally[claims.FAILED] == 0


def cards_pending(root: Path, brand_id: str, waiting: int, python: str = DEFAULT_PYTHON) -> list[str]:
    """The lines check and run stop with while waiting card claims (claims.pending_cards) are not imported.

    CARDS_PENDING, then the topics command with --import-cards (topics_command), which imports them.
    """
    return [CARDS_PENDING.format(n=waiting), f"{topics_command(root, brand_id, python)} --import-cards"]


class TopicClaimError(Exception):
    """A topic could not be claimed. The message is the sentence the report and the summary show."""


def claim_topic(root: Path, brand: Brand, found: topics.TopicFile, run_name: str, now: datetime) -> claims.Claim:
    """Claim one approved topic of found for the run named run_name; the claims.Claim comes back.

    The claim is a file under the runs folder that only one run can create (claims.assign). It records
    run_name, the run folder's name, by which the topic's state is read from the run's meta.json later.
    No topic to give (claims.UsedUp: runs started since check took the last ones) is TopicClaimError
    with TOPICS_USED_UP. The errors claims.assign raises by design are TopicClaimError with CLAIM_FAILED
    and then the error: an OSError (a claims folder that cannot be made or written, say), a RuntimeError
    (it gave up) and a ValueError (a topic file it refuses, which load_topics has refused before, so a
    run does not meet it). Any other error is raised as it is.
    """
    try:
        return claims.assign(root, brand.id, found, run_name, now=now)
    except claims.UsedUp as exc:
        raise TopicClaimError(TOPICS_USED_UP) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise TopicClaimError(f"{CLAIM_FAILED} {exc}") from exc


def topic_record(claimed: claims.Claim) -> dict:
    """meta.json's and report.json's assigned_topic for a claim (claims.Claim).

    attempt counts the topic's claims, this one included. times is 1 and the attempts before this one
    that ended written; above 1 the report and the summary add TOPIC_REUSED. remaining is the approved
    topics LEFT or FAILED right after the claim (a moment's count). claim is the claim file.
    """
    return {"id": claimed.id, "topic": claimed.topic, "attempt": claimed.attempt, "times": claimed.times,
            "remaining": claimed.remaining, "claim": claimed.path.as_posix()}


def topic_place(record: dict) -> str:
    """How a claimed topic is named, by its id: '주제 h003번', and '주제 h003번, 2번째 시도' from its second attempt on."""
    place = topics.LABEL.format(id=record["id"])
    return place if record["attempt"] < 2 else f"{place}, {record['attempt']}번째 시도"


def topic_line(topic: str, brand: Brand, root: Path, found: topics.TopicFile | None = None, *,
               tally: dict[str, int] | None = None) -> str:
    """How check names the topic it decided on. The claims are only read (claims.counts), never made.

    found is the topic file load_topics gave check for a request that gets a topic (topic_due), and None
    for any other. Such a request is told how the run picks its topic (claims.assign: approved topics
    left or failed, in the order of the file, and under reuse then the one claimed longest ago), how many
    are left or failed, and what tools/claims.py promises of runs started at once: sharing one runs
    folder, and no more of them than that, they get topics that all differ (CHECK_STOP); under reuse, no
    more of them than there are approved topics not reserved now (CHECK_REUSE), since a reserved topic
    comes round too and one started now could get the topic another run is writing. Nothing is said of
    runs on other runs folders. tally is claims.counts for found as the caller read it, so that the line
    and what the caller judged from it come from one reading; without it the claims are read here. A
    request that hands the topic over without found is left to the node, and a request that names its
    topic is told it on one line, as before topics were assigned.
    """
    if topic_kind(topic, brand) != TOPIC_HANDED_OVER:
        return f"주제 {' '.join(topic.split())}"
    if found is None:
        return "주제 노드가 브랜드 문서를 보고 정합니다. 요청이 주제 고르기를 맡겼습니다."
    tally = claims.counts(root, brand.id, found) if tally is None else tally
    free = tally[claims.LEFT] + tally[claims.FAILED]
    if found.when_used_up == topics.REUSE:
        approved = sum(1 for listed in found.topics if listed.approved)
        return CHECK_REUSE.format(name=brand.name, free=free, count=max(0, approved - tally[claims.RESERVED]))
    return CHECK_STOP.format(name=brand.name, free=free)


DELIVER_BRAND = "brand"      # save the final HTML into brands/{id}/blogs/
DELIVER_SESSION = "session"  # show the final text in the session; save nothing into brands/
DELIVERIES = (DELIVER_BRAND, DELIVER_SESSION)
# Posts in brands/{id}/blogs/ are shared through git (Tom, 2026-09-30). Until one is committed the
# monorepo's SessionStart hook sees a dirty tree and skips git pull, so the person is told at once.
COMMIT_NOTE = ("커밋해서 올리면 팀과 공유됩니다. 쓰지 않을 글이면 지워 주세요. 커밋하지도 지우지도 않으면 다음 세션을 열 때 "
               "git pull이 건너뛰어집니다.")


def mirror_line(path: str) -> str:
    """Said when a run copied a changed section of the monorepo's CLAUDE.md into a durumi document."""
    return f"문서 옮김 {path} (모노레포 CLAUDE.md의 절이 바뀌어 도구가 옮겼습니다. 커밋해서 올려 주세요.)"


def mirror(root, log) -> list[str]:
    """Copy the monorepo's mirrored sections in before the documents are read (deploy.MIRRORED).

    Returns the documents that changed. A failure is logged and the run goes on with the documents
    as they are.
    """
    try:
        results = sync_mirrored(root)
    except OSError as exc:
        log(f"모노레포 CLAUDE.md의 절을 옮기지 못해 지금 문서 그대로 씁니다. {exc}")
        return []
    for result in results:
        if result["problem"]:
            log(f"{result['problem']} 지금 문서 그대로 씁니다.")
        elif result["changed"]:
            log(mirror_line(result["path"]))
    return [result["path"] for result in results if result["changed"]]


def deliver_mode(root, kind) -> str:
    """Where a finished post goes, decided from how the root was found (config.find_root's kind).

    A request made in the monorepo (the working tree found from the session, DURUMI_ROOT, or a
    --root naming the working tree) saves into brands/{id}/blogs/. A request from any other session
    (the working tree reached only through a deploy copy's record, or no working tree at all) is
    shown in the session and saved nowhere in brands/.
    """
    if kind in ("live", "env", "given") and not is_deploy_copy(root) and not in_plugin_cache(root):
        return DELIVER_BRAND
    return DELIVER_SESSION


def delivery_for(root, kind) -> str:
    """deliver_mode for a command line's kind, when no --deliver was given.

    A --root alone ('given') says nothing about where the request came from, so where this process
    stands decides (root_kind_here): the working tree found around the shell saves into brands/, a
    shell outside the monorepo shows the post in the session. A printed command that lost its
    --deliver on the way therefore never saves a post from outside into the working tree.
    """
    return deliver_mode(root, root_kind_here(root) if kind == "given" else kind)


def save_to_brand(brand, final_path: Path, name: str | None = None) -> Path:
    """Copy the final HTML into brands/{id}/blogs/ as `name` (default its own), never over an older post."""
    folder = Path(brand.dir) / "blogs"
    folder.mkdir(parents=True, exist_ok=True)
    named = Path(name or final_path.name)
    stem, suffix, n = named.stem, named.suffix, 1
    data = final_path.read_bytes()  # read first: a failed read must not leave an empty post behind
    while True:
        target = folder / (f"{stem}{suffix}" if n == 1 else f"{stem}-{n}{suffix}")
        try:
            handle = open(target, "xb")  # claims the name; an older post is never touched
        except FileExistsError:
            n += 1
            continue
        try:
            with handle:
                handle.write(data)
        except BaseException:
            target.unlink(missing_ok=True)  # a cut-off post must not sit under a finished post's name
            raise
        return target


def deliver_post(mode, brand, final_path: Path, doc, name: str | None = None) -> dict | None:
    """Hand the finished post over as `mode` says. None when no delivery was asked for."""
    if mode == DELIVER_BRAND:
        try:
            return {"mode": mode, "path": save_to_brand(brand, final_path, name).as_posix()}
        except OSError as exc:
            return {"mode": mode, "error": f"{type(exc).__name__}: {exc}"}
    if mode == DELIVER_SESSION:
        return {"mode": mode, "markdown": as_markdown(doc)}
    return None


def check(root, request_file, *, brand_id=None, log=print, deliver=None, python=DEFAULT_PYTHON) -> int:
    """Decide brand and topic for one request without calling a node, and print the run command.

    The skill runs this in the foreground first, so an ambiguous brand or a missing topic is settled
    with the person in the same turn, before the long run starts in the background. `deliver`
    (DELIVERIES) rides along into every run command printed, and so does python (PYTHONS).

    A request that will get a topic of the brand's topics.json (topic_due) is told how the topic is
    picked and how many are left (topic_line), under reuse with none left that the post gets a topic
    given before (CHECK_REUSE_NEXT), then which topics aim at one keyword (topics.keyword_notes) and how
    many are reserved with no result yet (CHECK_RESERVED), all from one reading of the claims. It stops
    here, as the run would, when the topics cannot be handed out (load_topics), while claims the topic
    cards left wait to be imported (cards_pending), or when there is none to give (TOPICS_USED_UP, then
    CHECK_RESERVED when topics are reserved, which may be why); for these four it names the brand first,
    as it does when it goes on. It stops too, with that sentence alone, when the claims or the card
    claims cannot be read or the brand id cannot name their folder (CLAIMS_UNREAD). Wherever
    CHECK_RESERVED is printed, the next line is the command that shows those topics (topics_command).
    The rule files are read for that request only: any other check reads none, as before topics were
    assigned, and leaves them to the run. Nothing is created.
    """
    root = Path(root)
    try:
        brands = list_brands(root)
    except ConfigError as exc:
        log(f"설정을 읽지 못했습니다. {exc}")
        return EXIT_ENV
    try:
        _, request_text = read_request(request_file)
    except OSError as exc:
        log(f"요청 파일을 읽지 못했습니다. {request_file} ({exc})")
        return EXIT_ENV
    except UnicodeDecodeError:
        log(f"요청 파일을 UTF-8로 읽을 수 없습니다. {request_file}")
        return EXIT_ENV
    brand, topic, code = decide(root, request_file, request_text, brands, brand_id, log, deliver=deliver,
                                python=python)
    if code != EXIT_OK:
        return code
    head = f"브랜드 {brand.name}({brand.id})"
    found = None
    if topic_due(topic, brand):
        try:
            rules = load_rules(root)  # the topics are judged by them (topics.problems)
        except (ConfigError, OSError) as exc:
            log(f"설정을 읽지 못했습니다. {exc}")
            return EXIT_ENV
        said: list[str] = []  # load_topics says why it gives no file, which comes after the brand
        found = load_topics(brand, rules, said.append)
        if found is None:
            for line in (head, *said):
                log(line)
            return EXIT_ENV
        try:
            waiting = claims.pending_cards(root, brand.id, found)
        except (OSError, ValueError) as exc:  # whether a card claim waits is not known
            log(f"{CLAIMS_UNREAD} {exc}")
            return EXIT_ENV
        if waiting:
            for line in (head, *cards_pending(root, brand.id, waiting, python)):
                log(line)
            return EXIT_ENV
    if found is None:
        lines = [topic_line(topic, brand, root)]
    else:
        try:
            tally = claims.counts(root, brand.id, found)  # the one reading the lines below come from
        except (OSError, ValueError) as exc:  # the claims folder cannot be read, or the brand id cannot name one
            log(f"{CLAIMS_UNREAD} {exc}")
            return EXIT_ENV
        # The line names the topics command; the command follows whole, for a session that has not seen it.
        reserved = ([CHECK_RESERVED.format(n=tally[claims.RESERVED]), topics_command(root, brand.id, python)]
                    if tally[claims.RESERVED] else [])
        if _used_up(found, tally):
            for line in (head, TOPICS_USED_UP, *reserved):
                log(line)
            return EXIT_ENV
        again = ([CHECK_REUSE_NEXT] if found.when_used_up == topics.REUSE and
                 tally[claims.LEFT] + tally[claims.FAILED] == 0 else [])
        lines = [topic_line(topic, brand, root, found, tally=tally), *again, *topics.keyword_notes(found), *reserved]
    log(head)
    for line in lines:
        log(line)
    log("준비가 끝났습니다. 아래 실행 명령을 Bash 도구로 백그라운드에서 실행하십시오.")
    log(run_command(root, request_file, brand.id, deliver, python))
    return EXIT_OK


def run(root, request_file, *, brand_id=None, call=None, claude=None, finder=None, now=None, log=print,
        root_kind=None, deliver=None, python=DEFAULT_PYTHON) -> int:
    """Run the whole pipeline for one request file. Returns the exit code.

    deliver (DELIVERIES) says where a finished post goes besides the run folder; None hands it
    nowhere else. The command line decides it (see deliver_mode). deliver and python (PYTHONS) also
    ride along into the --brand commands printed when the brand is not decided.

    A request that hands the topic over to a brand with a topic file (topic_due) is judged as check
    judges it before claude is looked for: a topic file that cannot be used (load_topics), claims the
    topic cards left that wait to be imported (cards_pending) or card claims that cannot be read
    (CLAIMS_UNREAD), and a topic file with no topic to give (TOPICS_USED_UP) end the run with exit code 6
    and no run folder. Claims that cannot be read for that last judgment are left to the claim, which
    then fails too. The request claims one topic (claim_topic) once the run folder holds request.txt and
    meta.json, meta.json is written again with it (assigned_topic), and both
    nodes are told it beside the request as written. A claim that fails (TopicClaimError, the last
    topic taken meanwhile among them) ends the run with exit code 6 before any node is called; the
    report and the summary say why.

    brand_id picks the brand explicitly (after an ambiguous request); otherwise it is resolved from the text.
    call(binary, system_prompt, request, cfg, schema) -> NodeResult defaults to node.call_node.
    claude is (path, version); when None it is found with finder (default node.find_claude).
    root_kind is how the command line found root (config.find_root kinds, or 'given'), for meta.json.
    """
    root = Path(root)
    call = call_node if call is None else call
    started = now_kst(now)

    try:
        rules = load_rules(root)
        brands = list_brands(root)
    except ConfigError as exc:
        log(f"설정을 읽지 못했습니다. {exc}")
        return EXIT_ENV
    try:
        raw_request, request_text = read_request(request_file)
    except OSError as exc:
        log(f"요청 파일을 읽지 못했습니다. {request_file} ({exc})")
        return EXIT_ENV
    except UnicodeDecodeError:
        log(f"요청 파일을 UTF-8로 읽을 수 없습니다. {request_file}")
        return EXIT_ENV

    brand, topic, code = decide(root, request_file, request_text, brands, brand_id, log, deliver=deliver,
                                python=python)
    if code != EXIT_OK:
        return code
    found = None
    if topic_due(topic, brand):
        found = load_topics(brand, rules, log)
        if found is None:
            return EXIT_ENV
        try:
            waiting = claims.pending_cards(root, brand.id, found)
        except (OSError, ValueError) as exc:  # whether a card claim waits is not known, and a claim must not go out
            log(f"{CLAIMS_UNREAD} {exc}")
            return EXIT_ENV
        if waiting:
            for line in cards_pending(root, brand.id, waiting, python):
                log(line)
            return EXIT_ENV
        try:
            used_up = _used_up(found, claims.counts(root, brand.id, found))
        except (OSError, ValueError):
            used_up = False  # claims that cannot be read fail the claim too, which the report then names
        if used_up:
            log(TOPICS_USED_UP)
            return EXIT_ENV

    if claude is None:
        try:
            claude = (find_claude if finder is None else finder)()
        except NodeError as exc:
            log(str(exc))
            return EXIT_ENV
    binary, version = claude

    mirrored = mirror(root, log)  # before the documents are read, so the nodes get the current section
    try:
        configs = {name: load_node(root, name) for name, _ in STAGES}
        assemblies = {name: assemble(root, configs[name], brand, rules) for name, _ in STAGES}
    except ConfigError as exc:
        log(f"노드 설정이나 컨텍스트를 준비하지 못했습니다. {exc}")
        return EXIT_ENV

    try:
        folder = unique_dir(runs_root(root), run_id(started, brand.id))
    except (OSError, ValueError) as exc:
        log(f"실행 폴더를 만들지 못했습니다. {exc}")
        return EXIT_ENV
    # Printed at once (durumi.py line-buffers stdout), so whoever waits can follow the progress logs.
    log(f"실행 폴더 {folder.as_posix()} (진행 기록은 이 폴더에 draft.1.progress.log처럼 단계와 시도마다 쌓입니다)")
    # The printed run command always carries --root ('given'). Whether a copy there was fallen back to
    # or chosen is what this process finds on its own (DURUMI_ROOT, the shell's folder, a copy's
    # record); preflight may have reached the root another way (--project), so only the warning uses it.
    found_kind = root_kind_here(root) if root_kind in (None, "given") else root_kind
    warning = copy_warning(root, found_kind)
    if warning:
        log(warning)
    if deliver == DELIVER_BRAND and (is_deploy_copy(root) or in_plugin_cache(root)):
        # A copy's brands/ folder is not the working tree: nobody can commit it, and the next deploy or
        # plugin update deletes it. The post is shown in the session instead.
        log("사본에는 글을 브랜드 폴더에 저장하지 않고 세션에 보여 드립니다. 사본의 brands는 커밋할 수 없고 다음 배포나 플러그인 "
            "갱신 때 지워집니다.")
        deliver = DELIVER_SESSION
    ctx = RunContext(root=root, dir=folder, rules=rules, brand=brand, binary=binary, call=call)

    # Everything below runs inside the run folder. An unexpected error (a full disk, a failed write,
    # a node call that raises) still ends with report.json, report.md and meta.json and exit code 6.
    meta = {
        "run_id": folder.name,
        "started_at": started.isoformat(timespec="seconds"),
        "brand": {"id": brand.id, "name": brand.name, "palettes": list(brand.palettes) if brand.palettes else None},
        "assigned_topic": None,  # topic_record once a topic is claimed
    }
    stages: list[Stage] = []
    final_path = None
    delivered = None
    fatal = ""
    assigned = None  # the claimed topic's sentence, handed to the nodes
    try:
        write_text(folder / "request.txt", request_text)
        meta.update({
            "request": {"path": str(Path(request_file)), "sha256": sha256_bytes(raw_request), "topic": topic,
                        "topic_kind": topic_kind(topic, brand)},
            "root": {"path": root.resolve().as_posix(), "kind": root_kind,
                     "copy": is_deploy_copy(root) or in_plugin_cache(root)},
            "deliver": deliver,
            "claude": {"path": str(binary), "version": version},
            "python": platform.python_version(),
            "rules": {name: sha256_bytes((root / "rules" / name).read_bytes())
                      for name in ("palettes.toml", "form.toml") if (root / "rules" / name).is_file()},
            "nodes": {name: dataclasses.asdict(cfg) for name, cfg in configs.items()},
            "node_env_removed": removed_env_names(),
            "parts": {name: assemblies[name].parts for name in configs},
        })
        write_json(folder / "meta.json", meta)  # before the claim, so a run whose claim fails leaves the whole record
        if found is not None:
            claimed = claim_topic(root, brand, found, folder.name, started)
            meta["assigned_topic"] = topic_record(claimed)
            assigned = claimed.topic
            log(f"배정 주제 {assigned} ({topic_place(meta['assigned_topic'])})")  # at once, for whoever watches the run
            write_json(folder / "meta.json", meta)

        base_request = request_text if assigned is None else assigned_request(request_text, assigned)
        draft = run_stage(ctx, "draft", configs["draft"], assemblies["draft"].text, base_request, stages)
        if draft.ok:
            revise = run_stage(ctx, "revise", configs["revise"], assemblies["revise"].text,
                               revise_request(request_text, draft.envelope, assigned), stages)
            if revise.ok:
                write_json(folder / "final.json", revise.envelope)
                write_text(folder / "final.html", revise.verdict.html)
                final_path = folder / final_name(started, revise.verdict.doc.slug)
                write_text(final_path, revise.verdict.html)
                delivered = deliver_post(deliver, brand, final_path, revise.verdict.doc,
                                         name=blog_name(started, revise.verdict.doc.slug))
        exit_code = next((s.exit_code for s in stages if s.exit_code != EXIT_OK), EXIT_OK)
    except TopicClaimError as exc:  # raised before the draft node, so no stage has begun
        fatal = str(exc)
        exit_code = EXIT_ENV
    except Exception as exc:  # noqa: BLE001  the report must still be written
        fatal = f"예기치 못한 오류로 멈췄습니다. {type(exc).__name__}: {exc}"
        exit_code = EXIT_ENV
        final_path = final_path if final_path is not None and final_path.is_file() else None
        if stages and not stages[-1].error:
            stages[-1].ok = stages[-1].ok and final_path is not None
            stages[-1].exit_code = EXIT_ENV
            stages[-1].error = fatal

    finished = now_kst() if now is None else started
    try:
        report = build_report(meta, stages, assemblies, final_path, exit_code, finished, rules, brand=brand)
        report["delivered"] = delivered
        report["mirrored"] = mirrored
        if fatal:
            report["error"] = fatal
        write_json(folder / "report.json", report)
        write_text(folder / "report.md", report_markdown(report))
        meta["finished_at"] = finished.isoformat(timespec="seconds")
        meta["exit_code"] = exit_code
        write_json(folder / "meta.json", meta)
    except Exception as exc:  # noqa: BLE001
        if fatal:
            log(fatal)
        log(f"보고서를 쓰지 못했습니다. {type(exc).__name__}: {exc} (실행 폴더 {folder.as_posix()})")
        # The post may be finished and saved all the same; say so, or the person reruns a 20 minute job.
        for line in _salvage_lines(final_path, delivered):
            log(line)
        return EXIT_ENV
    for line in summary_lines(report, folder):
        log(line)
    return exit_code


def _salvage_lines(final_path, delivered) -> list[str]:
    """What a run left behind, for when its report could not be written."""
    lines = []
    if final_path is not None and Path(final_path).is_file():
        lines.append(f"보고서는 없지만 글은 끝났습니다. 최종 파일 {Path(final_path).as_posix()}")
    delivered = delivered or {}
    if delivered.get("path"):
        lines += [f"블로그 저장 {delivered['path']}", COMMIT_NOTE]
    elif delivered.get("markdown"):
        lines += ["", "아래는 완성한 글입니다. 브랜드 폴더에는 저장하지 않았습니다.", "", delivered["markdown"].rstrip("\n")]
    return lines


# Report


STATUS = {EXIT_OK: "success", EXIT_CHECKS_FAILED: "checks_failed", EXIT_NODE: "node_error", EXIT_ENV: "environment_error"}
STATUS_SENTENCE = {
    EXIT_OK: "성공했습니다.",
    EXIT_CHECKS_FAILED: "형식 오류가 남아 멈췄습니다.",
    EXIT_NODE: "노드 오류로 멈췄습니다.",
    EXIT_ENV: "렌더러, 규칙, 실행 환경의 문제로 멈췄습니다.",
}

METRIC_LABELS = (
    ("title_chars", "제목 글자 수(공백 제외)"),
    ("body_chars", "본문 글자 수(공백 제외)"),
    ("blocks", "블록 수"),
    ("paragraphs", "문단 수"),
    ("sentences", "문장 수"),
    ("paragraph_sentences_max", "문단당 문장 수 최대"),
    ("paragraph_sentences_avg", "문단당 문장 수 평균"),
    ("subheadings", "소제목 수"),
    ("subheading_chars", "소제목 글자 수"),
    ("images", "이미지 자리 수"),
    ("max_blocks_between_images", "이미지 사이 최대 블록 수"),
    ("max_blocks_without_image", "이미지 없이 이어진 최대 블록 수"),
    ("links", "링크 자리 수"),
    ("bold", "굵게 수"),
    ("color", "글자 색 수"),
    ("highlight", "형광펜 수"),
    ("bold_ratio", "굵게 글자 비율"),
    ("color_ratio", "글자 색 글자 비율"),
    ("highlight_ratio", "형광펜 글자 비율"),
    ("commas_per_sentence", "문장당 쉼표"),
    ("exclamations", "느낌표 수"),
    ("quotes", "인용 수"),
    ("list_items", "목록 항목 수"),
    ("checklist_items", "체크리스트 항목 수"),
    ("tables", "표 수"),
    ("emoji", "이모지"),
)


def _stage_report(stage: Stage) -> dict:
    verdict = stage.verdict
    issues = verdict.issues if verdict else []
    return {
        "name": stage.name,
        "ok": stage.ok,
        "exit_code": stage.exit_code,
        "error": stage.error,
        "attempts": stage.attempts,
        "issues": [issue.to_dict() for issue in issues],
        "slug": verdict.doc.slug if verdict and verdict.doc else None,
        "title": verdict.doc.title if verdict and verdict.doc else None,
    }


def _totals(stages) -> dict:
    results = [r for s in stages for r in s.results]
    costs = [r.cost for r in results if isinstance(r.cost, (int, float))]
    models = sorted({m for r in results for m in r.model_ids})
    return {
        "calls": len(results),
        "input_tokens": sum(input_tokens(r.usage or {}) for r in results),
        "output_tokens": sum(int((r.usage or {}).get("output_tokens") or 0) for r in results),
        "cost": round(sum(costs), 4) if costs else None,
        # Calls cut before a result carry no cost; the sum above leaves them out, and says so.
        "cost_unknown_calls": len(results) - len(costs),
        "duration_sec": round(sum(r.duration_sec or 0 for r in results), 3),
        "model_ids": models,
    }


def form_counts(draft_metrics: dict, final_metrics: dict, draft_doc=None, final_doc=None) -> dict:
    """Formatting elements whose count (or the palettes) the revise node changed: {key: [draft, final]}."""
    changed = {}
    for key, _, _ in FORM_COUNTS:
        before, after = draft_metrics.get(key), final_metrics.get(key)
        if isinstance(before, int) and isinstance(after, int) and before != after:
            changed[key] = [before, after]
    if draft_doc is not None and final_doc is not None and tuple(draft_doc.palettes) != tuple(final_doc.palettes):
        changed["palettes"] = [list(draft_doc.palettes), list(final_doc.palettes)]
    return changed


def form_count_phrases(changed: dict) -> list[str]:
    phrases = []
    for key, label, unit in FORM_COUNTS:
        if key in changed:
            before, after = changed[key]
            phrases.append(f"{label} {before}{unit}에서 {after}{unit}")
    if "palettes" in changed:
        before, after = changed["palettes"]
        phrases.append(f"팔레트 {', '.join(before)}에서 {', '.join(after)}")
    return phrases


def build_report(meta, stages, assemblies, final_path, exit_code, finished, rules, brand=None) -> dict:
    """brand (config.Brand) lets the fact check pick the sentences that name the brand."""
    docs = {s.name: s.verdict.doc for s in stages if s.ok and s.verdict}
    draft_doc, final_doc = docs.get("draft"), docs.get("revise")
    metrics = {}
    for key, doc in (("draft", draft_doc), ("final", final_doc)):
        if doc is not None:
            metrics[key] = measure(doc, rules)
    comparison = None
    if draft_doc is not None and final_doc is not None:
        comparison = compare(draft_doc, final_doc)
        comparison["form_counts"] = form_counts(metrics["draft"], metrics["final"], draft_doc, final_doc)
    omitted = []
    seen = set()
    for name in assemblies:
        for part in assemblies[name].parts:
            for section in part["omitted_sections"]:
                head, sep, rest = section.partition(" > ")
                if sep and head.endswith(part["path"]):
                    section = rest  # drop the H1, which only repeats the document path
                entry = f"{part['path']} > {section}"
                if entry not in seen:
                    seen.add(entry)
                    omitted.append(entry)
    return {
        "run_id": meta["run_id"],
        "exit_code": exit_code,
        "status": STATUS.get(exit_code, str(exit_code)),
        "started_at": meta["started_at"],
        "finished_at": finished.isoformat(timespec="seconds"),
        "brand": meta["brand"],
        "assigned_topic": meta.get("assigned_topic"),
        "final_file": final_path.name if final_path else None,
        "stages": [_stage_report(s) for s in stages],
        "measure": metrics,
        "compare": comparison,
        "placeholders": placeholders(final_doc) if final_doc is not None else None,
        "factcheck": factcheck(draft_doc, final_doc, brand),
        "omitted_sections": omitted,
        "totals": _totals(stages),
    }


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "없음"
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in value.items()) if value else "없음"
    return str(value)


def _warnings(stage: dict) -> list:
    return [Issue(**i) for i in stage["issues"] if i["severity"] != ERROR]


def _normalized(report: dict) -> list:
    """(stage label, attempt number, note) for every transport repair (normalize.normalize_envelope)."""
    return [(STAGE_LABEL.get(s["name"], s["name"]), a["n"], note)
            for s in report["stages"] for a in s["attempts"] for note in a.get("normalized") or []]


def _numbers_line(kind: str, numbers: list) -> str:
    if numbers:
        return f"- {kind} 숫자는 {', '.join(numbers)}입니다."
    return f"- {kind} 숫자는 없습니다."


def report_markdown(report: dict) -> str:
    lines = [f"# 두루미 실행 보고서 {report['run_id']}", ""]
    brand = report["brand"]
    lines += [
        f"- 실행은 {STATUS_SENTENCE.get(report['exit_code'], '끝났습니다.')}",
        f"- 브랜드는 {brand['name']}({brand['id']})입니다.",
        f"- 최종 파일은 {report['final_file']}입니다." if report["final_file"] else "- 최종 파일은 만들지 않았습니다.",
        f"- 시작 {report['started_at']}, 끝 {report['finished_at']}",
    ]
    assigned = report.get("assigned_topic")
    if assigned:
        lines.append(f"- 배정 주제는 {assigned['topic']}입니다({topic_place(assigned)}).")
        if assigned["times"] > 1:
            lines.append(f"- {TOPIC_REUSED}")
    if report.get("error"):
        lines.append(f"- {report['error']}")
    delivered = report.get("delivered") or {}
    if delivered.get("path"):
        lines.append(f"- 글은 {delivered['path']}에 저장했습니다. {COMMIT_NOTE}")
    elif delivered.get("error"):
        lines.append(f"- 글을 브랜드 폴더에 저장하지 못했습니다. {delivered['error']}")
    elif delivered.get("mode") == DELIVER_SESSION:
        lines.append("- 글은 세션에 보여 드리고 브랜드 폴더에는 저장하지 않았습니다.")
    lines += ["", "", "## 형식 판정", "",
              "기계가 규칙 파일(rules/*.toml)과 브랜드 설정에 비추어 판정한 결과입니다. 오류가 있으면 같은 노드에 수리 요청을 보내고, 한도를 넘으면 멈춥니다. 경고는 막지 않고 적어 둡니다.",
              "", "| 단계 | 시도 | 결과 | 오류 | 경고 |", "| --- | --- | --- | --- | --- |"]
    for stage in report["stages"]:
        label = STAGE_LABEL.get(stage["name"], stage["name"])
        for attempt in stage["attempts"]:
            checks = attempt.get("checks")
            if checks is None:
                result, errors, warnings = "노드 오류", "-", "-"
            else:
                result = "통과" if checks["ok"] else "실패"
                errors, warnings = checks["errors"], checks["warnings"]
            lines.append(f"| {label} | {attempt['n']} | {result} | {errors} | {warnings} |")
    fixes = _normalized(report)
    if fixes:
        lines += ["", "기계가 봉투의 인코딩을 고친 시도입니다. 글은 바꾸지 않았고 노드의 원래 출력은 out.json에 그대로 있습니다.", ""]
        lines += [f"- {label} {n}번째 시도. {note}" for label, n, note in fixes]
    for stage in report["stages"]:
        label = STAGE_LABEL.get(stage["name"], stage["name"])
        if stage["error"]:
            lines += ["", f"{label} 단계에서 멈췄습니다. {stage['error']}"]
        errors = [Issue(**i) for i in stage["issues"] if i["severity"] == ERROR]
        if errors:
            lines += ["", f"### {label} 마지막 시도의 오류", ""]
            lines += [f"- {format_issue(issue)}" for issue in errors]
        warnings = _warnings(stage)
        if warnings:
            lines += ["", f"### {label} 마지막 시도의 경고와 알림", ""]
            lines += [f"- {format_issue(issue)}" for issue in warnings]

    lines += ["", "", "## 품질 판정", "",
              "품질 판정은 사람의 몫입니다. 기계는 글이 좋은지 판정하지 않습니다. 아래 숫자는 읽는 사람이 참고하도록 잰 것이며, 어떤 기준값으로도 막지 않습니다."]
    metrics = report["measure"]
    if metrics:
        keys = [k for k in ("draft", "final") if k in metrics]
        head = {"draft": "초안", "final": "최종"}
        lines += ["", "| 지표 | " + " | ".join(head[k] for k in keys) + " |",
                  "| --- |" + " --- |" * len(keys)]
        for key, label in METRIC_LABELS:
            values = [_fmt(metrics[k].get(key, "")) for k in keys]
            lines.append(f"| {label} | " + " | ".join(values) + " |")
    else:
        lines += ["", "통과한 원고가 없어 재지 않았습니다."]
    cmp = report["compare"]
    if cmp:
        lines += ["", "### 퇴고에서 달라진 점", "",
                  f"- 본문 변경 비율은 {cmp['change_ratio']:g}입니다(0은 그대로, 1은 전부 바뀜).",
                  f"- 본문 글자 수는 초안 {cmp['draft_body_chars']}자에서 최종 {cmp['final_body_chars']}자가 되었습니다.",
                  _numbers_line("새로 생긴", cmp["numbers_added"]),
                  _numbers_line("사라진", cmp["numbers_removed"])]
        if cmp["title_changed"]:
            lines.append(f"- 제목이 바뀌었습니다. 초안은 '{cmp['draft_title']}', 최종은 '{cmp['final_title']}'입니다.")
        else:
            lines.append("- 제목은 그대로입니다.")
        phrases = form_count_phrases(cmp.get("form_counts") or {})
        if phrases:
            lines.append(f"- 수가 달라진 서식 요소는 {', '.join(phrases)}입니다.")
        else:
            lines.append("- 서식 요소의 수와 팔레트는 그대로입니다.")

    slots = report["placeholders"]
    lines += ["", "", "## 채울 자리", ""]
    if slots is None:
        lines.append("최종 원고가 없습니다.")
    else:
        lines.append(f"붙여 넣은 뒤 사람이 채울 자리는 이미지 {len(slots['images'])}곳, 링크 {len(slots['links'])}곳입니다.")
        if slots["images"] or slots["links"]:
            lines.append("")
        lines += [f"- 이미지 {desc}" for desc in slots["images"]]
        lines += [f"- 링크 {target}" for target in slots["links"]]

    lines += ["", ""] + factcheck_lines(report.get("factcheck") or {})

    lines += ["", "", "## 빠진 절", "",
              "컨텍스트 문서에서 본문이 플레이스홀더뿐이라 시스템 프롬프트에 넣지 않은 절입니다."]
    if report["omitted_sections"]:
        lines.append("")
        lines += [f"- {entry}" for entry in report["omitted_sections"]]
    else:
        lines += ["", "없습니다."]

    lines += ["", "", "## 노드 호출", "",
              "이벤트와 최대 침묵은 흘려 받은 기록에서 잰 값입니다. 최대 침묵에는 마지막 이벤트 뒤 호출이 끝날 때까지의 시간도 들어갑니다.",
              "", "| 단계 | 시도 | 모델 | 입력 토큰 | 출력 토큰 | 비용(달러) | 시간(초) | 이벤트 | 최대 침묵(초) |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for stage in report["stages"]:
        label = STAGE_LABEL.get(stage["name"], stage["name"])
        for attempt in stage["attempts"]:
            node = attempt["node"]
            cost = "-" if node["cost"] is None else f"{node['cost']:g}"
            models = ", ".join(node["model_ids"]) or "-"
            stream = node.get("stream") or {}
            events = stream.get("events", "-")
            gap = f"{stream['max_gap_sec']:g}" if isinstance(stream.get("max_gap_sec"), (int, float)) else "-"
            lines.append(f"| {label} | {attempt['n']} | {models} | {node['input_tokens']} | "
                         f"{node['output_tokens']} | {cost} | {node['duration_sec']:g} | {events} | {gap} |")
    totals = report["totals"]
    cost = "-" if totals["cost"] is None else f"{totals['cost']:g}"
    lines.append(f"| 합계 | {totals['calls']} |  | {totals['input_tokens']} | {totals['output_tokens']} | "
                 f"{cost} | {totals['duration_sec']:g} |  |  |")
    notes = _stream_notes(report)
    if notes:
        lines += [""] + notes
    unknown = totals.get("cost_unknown_calls") or 0
    if unknown:
        lines += ["", f"비용을 모르는 호출 {unknown}번은 합계에서 빠졌습니다. 결과를 받기 전에 끊긴 호출입니다."]
    return "\n".join(lines) + "\n"


def _stream_notes(report: dict) -> list[str]:
    """Things about how a call ended that the table cannot show (node.NodeResult.stream)."""
    notes = []
    for stage in report["stages"]:
        label = STAGE_LABEL.get(stage["name"], stage["name"])
        for attempt in stage["attempts"]:
            stream = attempt["node"].get("stream") or {}
            where = f"- {label} {attempt['n']}번째 호출"
            if stream.get("killed") and stream.get("result_seen"):
                notes.append(f"{where}은 결과를 낸 뒤 끝나지 않아 두루미가 끊었습니다. 받은 결과는 그대로 썼습니다.")
            if stream.get("ended") == "exited" or stream.get("pipes_left"):
                notes.append(f"{where}에서는 노드가 끝난 뒤에도 다른 프로세스가 출력 파이프를 쥐고 있었습니다.")
            if stream.get("progress_error"):
                notes.append(f"{where}의 진행 기록은 온전하지 않습니다. {stream['progress_error']}")
    return notes


def summary_lines(report: dict, folder: Path) -> list[str]:
    lines = [f"두루미 실행이 {STATUS_SENTENCE.get(report['exit_code'], '끝났습니다.')}"]
    assigned = report.get("assigned_topic")
    if assigned:
        # remaining was counted right after the claim, and the line says so: by the time the run ends,
        # runs started since may have claimed more.
        lines.append(f"배정 주제 {assigned['topic']} ({topic_place(assigned)}, 예약 직후 남은 주제 {assigned['remaining']}개)")
        if assigned["times"] > 1:
            lines.append(TOPIC_REUSED)
    lines += [stage["error"] for stage in report["stages"] if stage["exit_code"] == EXIT_ENV and stage["error"]]
    if report.get("error") and report["error"] not in lines:
        lines.append(report["error"])
    if report["final_file"]:
        lines.append(f"최종 파일 {(folder / report['final_file']).as_posix()}")
    delivered = report.get("delivered") or {}
    if delivered.get("path"):
        lines.append(f"블로그 저장 {delivered['path']}")
        lines.append(COMMIT_NOTE)
    elif delivered.get("error"):
        lines.append(f"블로그를 브랜드 폴더에 저장하지 못했습니다. {delivered['error']} 최종 파일은 실행 폴더에 있습니다.")
    lines += [mirror_line(path) for path in report.get("mirrored") or []]
    checks = []
    for stage in report["stages"]:
        label = STAGE_LABEL.get(stage["name"], stage["name"])
        state = "통과" if stage["ok"] else {EXIT_NODE: "노드 오류", EXIT_ENV: "멈춤"}.get(stage["exit_code"], "실패")
        checks.append(f"{label} {state}(시도 {len(stage['attempts'])}회)")
    warnings = sum(len(_warnings(s)) for s in report["stages"])
    # A run that stopped before the draft node (a topic that could not be claimed, a failed write)
    # has no stage to list, and says that instead of a line with nothing after its label.
    lines.append(f"형식 판정 {', '.join(checks)}, 남은 경고와 알림 {warnings}건" if checks else NO_STAGES)
    fixes = _normalized(report)
    if fixes:
        lines.append(f"봉투 인코딩 보정 {len(fixes)}건(보고서의 형식 판정 절)")
    slots = report["placeholders"]
    if slots is not None:
        lines.append(f"채울 자리 이미지 {len(slots['images'])}곳, 링크 {len(slots['links'])}곳")
    final = report["measure"].get("final")
    if final:
        lines.append(f"주요 지표 본문 {final['body_chars']}자(공백 제외), 블록 {final['blocks']}개, "
                     f"소제목 {final['subheadings']}개, 문장 {final['sentences']}개")
    phrases = form_count_phrases((report.get("compare") or {}).get("form_counts") or {})
    if phrases:
        lines.append(f"퇴고에서 달라진 수 {', '.join(phrases)}")
    checked = factcheck_summary(report.get("factcheck") or {})
    if checked:
        lines.append(checked)
    omitted = report["omitted_sections"]
    lines.append(f"빠진 절 {len(omitted)}개" + (f" ({omitted[0]} 등)" if len(omitted) > 1 else
                                                (f" ({omitted[0]})" if omitted else "")))
    totals = report["totals"]
    cost = "알 수 없음" if totals["cost"] is None else f"{totals['cost']:g}달러"
    unknown = totals.get("cost_unknown_calls") or 0
    if unknown:
        cost += f"(비용을 모르는 호출 {unknown}번 제외)"
    lines.append(f"노드 {', '.join(totals['model_ids']) or '모델 정보 없음'}, 호출 {totals['calls']}번, "
                 f"입력 토큰 {totals['input_tokens']}, 출력 토큰 {totals['output_tokens']}, "
                 f"비용 {cost}, 시간 {totals['duration_sec']:g}초")
    lines.append(f"보고서 {(folder / 'report.md').as_posix()}")
    if delivered.get("markdown"):
        lines += ["", "아래는 완성한 글입니다. 브랜드 폴더에는 저장하지 않았고, 스마트에디터에 붙여 넣을 HTML은 위 최종 파일입니다.",
                  "", delivered["markdown"].rstrip("\n")]
    return lines


# Preflight


def copy_warning(root, kind=None) -> str | None:
    """A warning when root is a copy (plugin cache or deploy copy) rather than the working tree, else None.

    Found on its own, a copy is the root only when the working tree it came from was not found;
    named with DURUMI_ROOT or --root ('env', 'given'), it was chosen. Either way the run reads the
    copy's documents, and its runs go to the user's folder (runs_root), not into the copy.
    """
    if in_plugin_cache(root):
        what = "Claude Code 플러그인 캐시 안의 사본"
    elif is_deploy_copy(root):
        what = "배포본"
    else:
        return None
    why = "지정한 루트가" if kind in ("env", "given") else "원본 폴더를 찾지 못해 루트가"
    return (f"{why} {what}입니다. 이 사본의 도구와 문서로 돌고, 실행 기록은 {runs_root(root).as_posix()}에 "
            "남기며, 글은 브랜드 폴더에 저장하지 않습니다. 원본 폴더로 돌리려면 모노레포 안에서 부르거나 DURUMI_ROOT에 "
            "모노레포의 projects/durumi 폴더를 적어 주십시오.")


def _copy_behind(me: Path, root: Path, cli: str, where: str, python: str) -> list[str]:
    """A notice when this code runs from a deploy copy whose skill or tools differ from root's.

    The installed plugin's skill and preflight run from the copy while check and run use root's
    tools, whichever way root was found (from outside the monorepo, or /durumi:durumi inside it).
    The command carries --root like check and run, so the shell's current folder cannot pick the copy.
    """
    try:
        if (not is_deploy_copy(me) or me.resolve() == Path(root).resolve() or copy_warning(root)
                or not code_differs(me, root)):
            return []  # a root that is a copy itself has its own warning, and deploy refuses to run from it
    except OSError:
        return []
    again = f"PYTHONUTF8=1 {python} \"{cli}\" deploy --root \"{where}\""
    if not in_plugin_cache(me) and me.parent.resolve() != default_out(root).resolve():
        again += f" --out \"{me.parent.resolve().as_posix()}\""
    return ["- 알림 배포본의 스킬이나 도구가 원본 폴더와 다릅니다. 스킬과 준비 점검은 배포본에서, 확인과 실행은 원본 폴더에서 "
            f"돌아갑니다. 사용자에게 알리고, 사용자가 원하면 이 명령으로 배포본을 다시 만듭니다. {again}"]


def preflight(root, *, finder=None, now=None, kind=None, python=DEFAULT_PYTHON, self_root=None, tag=None) -> str:
    """Readiness report printed before a run. Never calls a node.

    Headings start at level 3 because the skill injects this under its own '## 준비 상황'.

    kind is how the command line found root (config.find_root kinds, or 'given' for --root); when
    None it is worked out again from the environment. python (PYTHONS) starts the printed commands.
    self_root is the folder this code runs from (config.SELF_ROOT); for a 'source' root it is the
    deploy copy, whose code is compared with the working tree's.
    """
    root = Path(root)
    finder = find_claude if finder is None else finder
    lines = ["### 두루미 준비 점검", ""]
    if kind is None:
        found, kind = find_root()
        try:
            same = found.resolve() == root.resolve()
        except OSError:
            same = False
        kind = kind if same else "given"
    kind_text = {"env": "환경 변수 DURUMI_ROOT", "live": "모노레포 안의 원본 폴더", "source": "배포본에 적힌 원본 폴더",
                 "self": "tools 폴더의 상위 폴더", "given": "직접 지정"}
    lines.append(f"- 루트 {root.resolve().as_posix()} ({kind_text.get(kind, kind)})")
    warning = copy_warning(root, kind)
    if warning:
        lines.append(f"- 경고 {warning}")
    cli, where = _cli_and_root(root)
    stale = stale_project_skill(root)
    if stale is not None:
        # Told to the person, not run by the model: sync-skill rewrites a tracked file of the monorepo.
        lines.append(f"- 경고 모노레포 스킬이 플러그인 스킬과 다릅니다({stale.as_posix()}). 사용자에게 알리고, 사용자가 원하면 "
                     f"이 명령으로 다시 만듭니다. PYTHONUTF8=1 {python} \"{cli}\" sync-skill --root \"{where}\"")
    try:
        for target in stale_mirrored(root):
            rel = target.relative_to(root.resolve()).as_posix()
            lines.append(f"- 알림 {rel}의 절이 모노레포 CLAUDE.md와 달라서, 실행할 때 도구가 옮깁니다.")
    except (OSError, ValueError):
        pass  # the run copies it anyway, or says why it could not
    lines += _copy_behind(Path(SELF_ROOT if self_root is None else self_root), root, cli, where, python)
    try:
        binary, version = finder()
        lines.append(f"- claude {version} ({Path(binary).as_posix()})")
    except NodeError as exc:
        lines.append(f"- claude를 찾지 못했습니다. {exc}")
    lines.append(f"- 파이썬 {platform.python_version()}")

    try:
        rules = load_rules(root)
        brands = list_brands(root)
        configs = [load_node(root, name) for name, _ in STAGES]
    except ConfigError as exc:
        lines += ["", f"설정을 읽지 못했습니다. {exc}"]
        return "\n".join(lines) + "\n"

    lines += ["", "#### 노드", ""]
    for cfg in configs:
        lines.append(f"- {STAGE_LABEL.get(cfg.name, cfg.name)}({cfg.name}) 모델 {cfg.model}, effort {cfg.effort}, "
                     f"제한 {cfg.timeout_sec}초, 멈춤 판정 {cfg.stall_sec}초, 예산 {cfg.max_budget_usd:g}달러, "
                     f"수리 {cfg.retries}번")
        lines.append(f"  include {', '.join(cfg.include)}")

    lines += ["", "#### 브랜드", ""]
    for brand in brands:
        palettes = f"고정 팔레트 {', '.join(brand.palettes)}" if brand.palettes else "팔레트는 노드가 고름"
        lines.append(f"- {brand.id} {brand.name}, 별칭 {', '.join(brand.aliases) or '없음'}, {palettes}")
    if not brands:
        lines.append("- 등록된 브랜드가 없습니다.")

    lines += ["", "#### 팔레트", ""]
    lines.append(f"- 고를 수 있는 팔레트 {', '.join(rules.palettes)} (한 글에 최대 {rules.form.get('max_palettes', 2)}개)")
    lines.append(f"- 플레이스홀더 색 {rules.placeholder}")

    lines += ["", "#### 빈 절", ""]
    docs = sorted((root / "context").glob("*.md")) + [b.doc_path for b in brands]
    total = 0
    for path in docs:
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
        try:
            _, omitted, _ = prepare_document(path.read_bytes(), rel)
        except (OSError, ConfigError) as exc:
            lines.append(f"- {rel} 읽지 못했습니다. {exc}")
            continue
        total += len(omitted)
        if omitted:
            lines.append(f"- {rel} {len(omitted)}개")
    lines.append(f"- 합계 {total}개 (본문이 플레이스홀더뿐이라 노드에 넣지 않는 절)")

    lines += ["", "#### 문서 검사", ""]
    if warning:
        # Every H1 names the document's path in the monorepo, which a copy elsewhere never matches.
        lines.append("- 사본이라 건너뜁니다. 문서의 H1은 모노레포 기준 경로라서 사본에서는 맞지 않으니 원본 폴더에서 check-docs로 "
                     "검사합니다.")
    else:
        reports = check_paths(repo_root_of(root), runtime_doc_paths(root))
        failed = [r for r in reports if not r.ok]
        lines.append(f"- 검사 {len(reports)}개, 통과 {len(reports) - len(failed)}개, 문제 있음 {len(failed)}개")
        for report in failed:
            rel = (report.path.relative_to(root).as_posix() if report.path.is_relative_to(root)
                   else report.path.as_posix())
            lines.append(f"  {rel} 문제 {len(report.problems)}건")

    mode = delivery_for(root, kind)
    if mode == DELIVER_BRAND:
        why = {"live": "이 세션은 모노레포 안이라", "env": "루트를 DURUMI_ROOT로 정한 두루미 폴더로 잡아서"}.get(
            kind, "루트를 직접 정한 두루미 폴더로 잡아서")
        where_to = f"{why} 완성한 글을 brands/{{브랜드 id}}/blogs/에 저장합니다"
    else:
        why = "루트가 사본이라" if warning else "이 세션은 모노레포 밖이라"
        where_to = f"{why} 완성한 글을 브랜드 폴더에 저장하지 않고 요약 끝에 찍어 보여 줍니다"
    tag = tag or heredoc_tag()
    lines += ["", "#### 다음 단계", "",
              "- 확인 명령은 아래 세 줄입니다. 가운데 줄 {요청 원문} 자리에 사용자의 요청 원문을 고치지 않고 그대로 넣습니다.",
              "",
              f"PYTHONUTF8=1 {python} \"{cli}\" check - --root \"{where}\" --deliver {mode}{_python_flag(python)} "
              f"<<'{tag}'",
              "{요청 원문}",
              tag,
              "",
              "- 확인 명령은 노드를 부르지 않고 곧바로 끝납니다. 요청 원문을 운영체제 임시 폴더에 저장하고 브랜드와 주제를 판정한 뒤, "
              "준비가 끝나면 그 파일로 돌릴 실행 명령을 알려 줍니다.",
              "- 브랜드를 하나로 정하지 못하면 후보마다 --brand를 붙인 실행 명령을 알려 주고, 주제가 없으면 무엇을 물을지 알려 줍니다.",
              f"- {where_to}(--deliver {mode}).",
              "- 따옴표 친 heredoc이라 셸이 요청 원문의 글자를 바꾸지 않고, 끝 표시는 준비 점검마다 새로 정해서 요청 원문의 어떤 "
              f"줄도 heredoc을 끊지 못합니다. 실행 기록은 {runs_root(root).as_posix()}에 남습니다."]
    return "\n".join(lines) + "\n"


HEREDOC_TAG = "DURUMI_REQUEST"


def heredoc_tag() -> str:
    """A fresh end marker for the request heredoc, like DURUMI_REQUEST_1F3A9C0B.

    A request line equal to a fixed, documented marker would end the heredoc early and run the
    lines after it as shell commands under the skill's pre-approved python prefix.
    """
    return f"{HEREDOC_TAG}_{secrets.token_hex(4).upper()}"


def _python_flag(python: str) -> str:
    """--python for a printed command that prints commands itself, when it is not the default."""
    return "" if python == DEFAULT_PYTHON else f" --python {python}"


def _cli_and_root(root) -> tuple[str, str]:
    """tools/durumi.py of this root and the root itself, as the commands print them.

    The commands carry --root, so a run later uses exactly the root preflight found, whatever the
    shell's current folder is by then.
    """
    root = Path(root).resolve()
    return (root / "tools" / "durumi.py").as_posix(), root.as_posix()


def run_command(root, request_path, brand_id: str | None = None, deliver: str | None = None,
                python: str = DEFAULT_PYTHON) -> str:
    """The exact shell command that runs the pipeline for one request file."""
    cli, where = _cli_and_root(root)
    command = f'PYTHONUTF8=1 {python} "{cli}" run "{Path(request_path).as_posix()}" --root "{where}"'
    if brand_id:
        command += f" --brand {brand_id}"
    return f"{command} --deliver {deliver}" if deliver else command


def topics_command(root, brand_id: str, python: str = DEFAULT_PYTHON) -> str:
    """The exact shell command that shows a brand's topics and their states (the topics command of tools/durumi.py).

    check prints it under CHECK_RESERVED: a session that runs the skill knows the commands it is shown, and
    that line only names this one. It starts as run_command does and carries --root alike. The topics
    command prints no command and saves no post, so neither --python nor --deliver rides along. The brand
    id stands unquoted, as in run_command; check has read the brand's claims by then, so the id is one
    claims.claims_dir takes (naming.BRAND_ID).
    """
    cli, where = _cli_and_root(root)
    return f'PYTHONUTF8=1 {python} "{cli}" topics {brand_id} --root "{where}"'


def show(root, name: str) -> tuple[int, str]:
    """Report of a past run. `latest` picks the newest run folder.

    Anything else must be a run folder name (naming.run_id), so the argument cannot reach outside
    runs/ (no '..', no absolute or network path).
    """
    runs = runs_root(root)
    # Run folders only: '_topics' (and '_cards', which the topic cards left) sorts after every digit, so
    # counting it would make it the newest run.
    folders = sorted(p for p in runs.glob("*") if p.is_dir() and RUN_NAME.fullmatch(p.name)) if runs.is_dir() else []
    if name == "latest":
        # The newest folder may be a run still going, or one that was killed before its report.
        done = [p for p in folders if (p / "report.md").is_file()]
        if not done:
            return 1, "보고서가 있는 실행 기록이 없습니다." + (f" 아직 끝나지 않은 실행 {folders[-1].name}" if folders else "")
        folder = max(done, key=lambda p: p.name)
        newer = [p.name for p in folders if p.name > folder.name]
        note = ("(더 새로운 실행이 아직 끝나지 않았거나 보고서 없이 멈춰서, 보고서가 있는 가장 최근 실행을 보여 드립니다. "
                f"더 새로운 실행 {', '.join(newer)})\n\n") if newer else ""
        return 0, note + (folder / "report.md").read_bytes().decode("utf-8", "replace")
    elif not RUN_NAME.fullmatch(name or ""):
        return 1, f"실행 기록 이름이 아닙니다. {name}\n이름은 20260930-143012-브랜드 모양이거나 latest입니다."
    else:
        folder = runs / name
    report = folder / "report.md"
    if not report.is_file():
        recent = ", ".join(p.name for p in folders[-5:]) or "없음"
        return 1, f"실행 기록을 찾지 못했습니다. {name}\n최근 실행은 {recent}입니다."
    return 0, report.read_bytes().decode("utf-8", "replace")  # a report cut off by a full disk still shows


if __name__ == "__main__":  # pragma: no cover
    sys.exit("tools/durumi.py를 실행해 주십시오.")
