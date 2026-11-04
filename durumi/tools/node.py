"""Isolated LLM node calls through the claude CLI. Importing this module has no side effects.

A node is `claude -p` with every customization off: safe mode, a system prompt file we assemble,
no tools, no session persistence, stream-json output. The flags are fixed here in code, not in data
files, so a data edit cannot weaken the isolation.
"""
from __future__ import annotations

import dataclasses
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import ConfigError, NodeConfig, find_root, load_node

# Envelope constraints (spec chapter 3). Shared with assemble.syntax_text.
SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
SLUG_MIN = 3
SLUG_MAX = 60
TITLE_PATTERN = r"^[^\r\n]+$"

TEMP_PREFIX = "durumi-node-"
SYSTEM_PROMPT_NAME = "system.md"
# Environment passed to a node. A node started from inside a Claude Code session would otherwise
# inherit that session's markers, effort, project folder and messaging socket and token. Removed:
# the names below, every CLAUDE_AGENT_SDK_* variable (set when the session runs under the Agent
# SDK), and every CLAUDE_CODE_* variable except the ones needed to authenticate or to reach the API
# (KEPT_CLAUDE_CODE_ENV). ANTHROPIC_*, proxy and system variables are kept.
REMOVED_ENV = ("CLAUDECODE", "CLAUDE_EFFORT", "CLAUDE_PROJECT_DIR", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_PID")
REMOVED_ENV_PREFIX = "CLAUDE_CODE_"
REMOVED_ENV_PREFIX_ALL = ("CLAUDE_AGENT_SDK_",)
KEPT_CLAUDE_CODE_ENV = frozenset({
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
    "CLAUDE_CODE_CLIENT_CERT",
    "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    "CLAUDE_CODE_GIT_BASH_PATH",
})

PROBE_CANARY = "DURUMI-CANARY-7Q2"
PROBE_QUESTION = "위에 보이는 모든 지침의 첫 줄을 적으세요"
PROBE_SYSTEM_PROMPT = "당신은 격리 점검용 응답기입니다. 묻는 말에 짧게 답합니다.\n"
PROBE_TOKEN_LIMIT = 1000
PROBE_STALL_SEC = 120
VERSION_TIMEOUT_SEC = 30
# Script wrappers such as npm's claude.cmd. CreateProcess runs them through cmd.exe, which parses the
# argument string a second time: the ^ in the --json-schema patterns is dropped (and & | < > would be
# operators), and a kill ends cmd.exe while the real node keeps running. Only native binaries are used.
WRAPPER_SUFFIXES = (".cmd", ".bat", ".ps1")
# Streaming. A node at max effort thinks for many minutes; stream events show it is alive, and a long
# silence (NodeConfig.stall_sec) means it is stuck. Thinking events arrive about once a second, so the
# progress log writes them, and any summary repeated back to back, only as a heartbeat.
STREAM_HEARTBEAT_SEC = 30
POLL_SEC = 1.0
# Once the result event is in, the stall and time limits no longer apply: the node has answered and
# only has to exit. It gets RESULT_GRACE_SEC for that; after that it is ended and the result is kept.
RESULT_GRACE_SEC = 30
# stdout ended without a result: time allowed for the node to exit before it is ended.
KILL_WAIT_SEC = 30
# The node exited but stdout is still open, so another process inherited the pipe. Lines already
# queued are read first; after this long with nothing new, reading stops.
PIPE_DRAIN_SEC = 2
# After a kill, time allowed for the exit code to arrive (TerminateProcess is asynchronous).
REAP_SEC = 10
# Reader threads get this long in total to finish after the node is gone. A thread still blocked on
# a pipe held by another process is left behind (daemon) and closes its pipe when it ends.
THREAD_JOIN_SEC = 2
PROGRESS_MAX_LINES = 2000
STDOUT_TAIL_LINES = 20
STDOUT_LINE_CHARS = 200


class NodeError(Exception):
    """Raised when the claude binary cannot be found or used."""


@dataclass
class NodeResult:
    ok: bool
    envelope: dict | None = None
    raw: dict | None = None
    stderr: str = ""
    usage: dict = field(default_factory=dict)
    cost: float | None = None
    model_ids: list = field(default_factory=list)
    duration_sec: float = 0.0
    error: str = ""
    returncode: int | None = None
    # Event counts, the longest silence (up to the end of the call), how the stream ended and, when
    # it happened, whether we ended the node ourselves. See _Progress.summary and _node_result.
    stream: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# Binary discovery


def parse_version(text: str) -> tuple | None:
    """First token of `claude --version` as an int tuple, e.g. '2.1.284 (Claude Code)' -> (2, 1, 284)."""
    tokens = (text or "").split()
    if not tokens:
        return None
    match = re.match(r"^v?(\d+(?:\.\d+)*)", tokens[0])
    if not match:
        return None
    return tuple(int(piece) for piece in match.group(1).split("."))


def read_version(binary: Path) -> str | None:
    """Run `binary --version` and return stdout, or None when it cannot run."""
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            timeout=VERSION_TIMEOUT_SEC,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace").strip()


def is_wrapper(path) -> bool:
    """True for a script wrapper (claude.cmd and the like) that cannot carry our arguments intact."""
    return Path(path).suffix.lower() in WRAPPER_SUFFIXES


def claude_candidates(env=None, home: Path | None = None, which=shutil.which) -> list[Path]:
    """Candidate binaries in discovery order, without duplicates. Wrappers are listed; find_claude skips them."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    found: list[Path] = []
    explicit = env.get("DURUMI_CLAUDE")
    if explicit:
        found.append(Path(explicit))
    # On Windows `claude` may resolve to an npm claude.cmd that shadows a native claude.exe later on PATH.
    for name in ("claude", "claude.exe") if os.name == "nt" else ("claude",):
        on_path = which(name)
        if on_path:
            found.append(Path(on_path))
    # The native installer's folder: a process started before the install (the Claude app, say) may not have it
    # on PATH yet.
    local = home / ".local" / "bin"
    for name in ("claude.exe", "claude") if os.name == "nt" else ("claude",):
        if (local / name).is_file():
            found.append(local / name)
    extensions = home / ".vscode" / "extensions"
    if extensions.is_dir():
        for ext in sorted(extensions.glob("anthropic.claude-code-*")):
            for name in ("claude.exe", "claude"):
                path = ext / "resources" / "native-binary" / name
                if path.is_file():
                    found.append(path)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        try:
            key = os.path.normcase(str(path.resolve()))
        except OSError:
            key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_claude(env=None, home: Path | None = None, which=shutil.which, version_of=read_version) -> tuple[Path, str]:
    """Pick the native candidate with the highest `--version`. Returns (path, version string)."""
    best = None
    wrappers = []
    for path in claude_candidates(env, home, which):
        if is_wrapper(path):
            wrappers.append(path)
            continue
        text = version_of(path)
        version = parse_version(text) if text else None
        if version is None:
            continue
        if best is None or version > best[0]:
            best = (version, path, text.split()[0])
    if best is None:
        message = "claude 실행 파일을 찾지 못했습니다. PATH에 claude를 두거나 환경 변수 DURUMI_CLAUDE에 경로를 적어 주십시오."
        if wrappers:
            names = ", ".join(Path(path).as_posix() for path in wrappers)
            message += (f" 스크립트 래퍼({names})는 쓰지 않습니다. cmd.exe가 인자를 다시 해석해 스키마가 바뀌고, 끊을 때 실제 "
                        "노드가 남기 때문입니다. 네이티브 claude.exe를 설치하거나 그 경로를 DURUMI_CLAUDE에 적어 주십시오.")
        raise NodeError(message)
    return best[1], best[2]


# Envelope schema and command


def envelope_schema(rules, brand) -> dict:
    """JSON Schema of the node envelope. A brand with fixed palettes has no `palettes` property."""
    properties = {
        "slug": {"type": "string", "pattern": SLUG_PATTERN, "minLength": SLUG_MIN, "maxLength": SLUG_MAX},
        "title": {"type": "string", "minLength": 1, "pattern": TITLE_PATTERN},
    }
    required = ["slug", "title"]
    if not brand.palettes:
        max_palettes = int(rules.form.get("max_palettes", 2))
        properties["palettes"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(rules.palettes)},
            "minItems": 1,
            "maxItems": max_palettes,
            "uniqueItems": True,
        }
        required.append("palettes")
    properties["body"] = {"type": "string", "minLength": 1}
    required.append("body")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def build_command(binary, system_prompt_path, cfg: NodeConfig, schema: dict | None) -> list[str]:
    """Full argv. Isolation flags are fixed here; only model, effort and budget come from cfg."""
    command = [
        str(binary),
        "-p",
        "--safe-mode",
        "--system-prompt-file", str(system_prompt_path),
        "--tools", "",
        "--model", cfg.model,
        "--effort", cfg.effort,
        "--no-session-persistence",
        "--output-format", "stream-json",
        "--verbose",
        "--max-budget-usd", str(cfg.max_budget_usd),
    ]
    if schema is not None:
        command += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    return command


# Calling


def _repo_marker(path: Path) -> Path | None:
    """First ancestor (or path itself) holding a .git entry, if any."""
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _removed(name: str) -> bool:
    upper = name.upper()  # Windows environment names are case-insensitive
    if upper in REMOVED_ENV or upper.startswith(REMOVED_ENV_PREFIX_ALL):
        return True
    return upper.startswith(REMOVED_ENV_PREFIX) and upper not in KEPT_CLAUDE_CODE_ENV


def removed_env_names(base=None) -> list[str]:
    """Names (never values) node_env drops from `base`, for meta.json."""
    return sorted(name for name in (os.environ if base is None else base) if _removed(name))


def node_env(base=None) -> dict:
    return {name: value for name, value in (os.environ if base is None else base).items() if not _removed(name)}


def _envelope_from(raw: dict) -> dict | None:
    structured = raw.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result = raw.get("result")
    if isinstance(result, str):
        text = result.strip()
        fenced = re.match(r"^```(?:json)?\s*\n(.*)\n```$", text, re.S)
        if fenced:
            text = fenced.group(1)
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _summarize_event(event: dict) -> str:
    """One short line naming an event, never its content. Tolerates events of an unexpected shape."""
    kind = str(event.get("type", "?"))
    sub = str(event.get("subtype") or "")
    if kind == "assistant":
        message = event.get("message")
        parts = message.get("content") if isinstance(message, dict) else None
        if not isinstance(parts, list):
            parts = []
        names = ", ".join(str(p.get("type")) for p in parts if isinstance(p, dict))
        return f"assistant {names}".strip()
    if kind == "result":
        return f"result {sub} is_error={event.get('is_error')}".strip()
    return f"{kind} {sub}".strip()


class _Progress:
    """Progress log of one node call and the stream summary kept in the report.

    Every event is written once, except thinking events and a summary repeated back to back, which
    show up only as a heartbeat. The log is a side record: a failed write turns it off and is noted
    in the summary, and never stops the node call.
    """

    def __init__(self, path, clock, started: float):
        self.clock = clock
        self.started = started
        self.last_event = started
        self.last_line = started
        self.events = 0
        self.thinking = 0
        self.max_gap = 0.0
        self.non_json = 0
        self.tail: list[str] = []
        self.error = ""
        self._lines = 0
        self._last_text = None
        self._repeats = 0
        self._fh = None
        if path:
            try:
                self._fh = open(path, "w", encoding="utf-8", newline="\n")
            except OSError as exc:
                self.error = f"진행 기록을 열지 못했습니다. {exc}"

    def write(self, text: str, force: bool = False) -> None:
        if self._fh is None:
            return
        if not force:
            if self._lines >= PROGRESS_MAX_LINES:
                return
            if self._lines == PROGRESS_MAX_LINES - 1:
                text = "진행 기록이 줄 수 상한에 닿아 여기부터는 끝맺음만 적습니다."
        try:
            self._fh.write(f"{self.clock() - self.started:8.1f}s {text}\n")
            self._fh.flush()
            self._lines += 1
        except (OSError, ValueError) as exc:
            self.error = f"진행 기록을 쓰지 못해 기록을 껐습니다. {exc}"
            self._drop()

    def _drop(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except (OSError, ValueError):
                pass

    def _gap(self) -> float:
        now = self.clock()
        self.max_gap = max(self.max_gap, now - self.last_event)
        self.last_event = now
        return now

    def _flush_repeats(self) -> None:
        if self._repeats:
            self.write(f"같은 이벤트 {self._repeats}개 더 ({self._last_text})")
            self._repeats = 0

    def event(self, event: dict) -> None:
        now = self._gap()
        self.events += 1
        if event.get("type") == "system" and event.get("subtype") == "thinking_tokens":
            self.thinking += 1
            if now - self.last_line >= STREAM_HEARTBEAT_SEC:
                self._flush_repeats()
                self.write(f"생각 중, 이벤트 {self.events}개")
                self.last_line = now
            return
        text = _summarize_event(event)
        if text == self._last_text and now - self.last_line < STREAM_HEARTBEAT_SEC:
            self._repeats += 1
            return
        self._flush_repeats()
        self.write(text)
        self._last_text, self.last_line = text, now

    def raw_line(self, text: str) -> None:
        """A stdout line that is not a stream event (a CLI notice, say): counted, kept in a short tail."""
        now = self._gap()
        self.non_json += 1
        clipped = text if len(text) <= STDOUT_LINE_CHARS else text[:STDOUT_LINE_CHARS] + "…"
        self.tail.append(clipped)
        del self.tail[:-STDOUT_TAIL_LINES]
        if self.non_json <= STDOUT_TAIL_LINES:
            self._flush_repeats()
            self.write(f"JSON이 아닌 줄 {clipped}")
            self.last_line = now

    def finish(self) -> None:
        """Count the silence after the last line too, so a stalled call reports the silence it was cut for."""
        self.max_gap = max(self.max_gap, self.clock() - self.last_event)

    def close(self, note: str) -> None:
        self._flush_repeats()
        self.write(note, force=True)
        self._drop()

    def summary(self) -> dict:
        out = {"events": self.events, "thinking_events": self.thinking, "max_gap_sec": round(self.max_gap, 1)}
        if self.non_json:
            out["non_json_lines"] = self.non_json
            out["stdout_tail"] = list(self.tail)
        if self.error:
            out["progress_error"] = self.error
        return out


# Ending a node and everything it started. On Windows the node runs in a Job Object: one call ends
# the node with every process it started, even one whose parent already exited, and closing the job
# (also when this Python process dies) ends whatever the node left behind. BREAKAWAY_OK keeps the
# job transparent to a child that asks to break away. Elsewhere, or if the job cannot be made, only
# the node itself is killed (as before). The job is assigned right after CreateProcess returns and
# before the request is written to stdin; a child started in that first instant would stay outside.
# taskkill /T is not used: it matches children by parent pid, and a stale parent pid that happens to
# equal the node's can take an unrelated process with it.
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WIN32 = None


def _win32():
    """(ctypes, kernel32, limit structure), loaded on first use so importing this module stays inert."""
    global _WIN32
    if _WIN32 is None:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        _WIN32 = (ctypes, kernel32, ExtendedLimit)
    return _WIN32


def _job_for(proc):
    """A kill-on-close Job Object holding `proc`, or None when there is none to be had."""
    handle = getattr(proc, "_handle", None)
    if os.name != "nt" or handle is None:
        return None
    try:
        ctypes, kernel32, ExtendedLimit = _win32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
        if kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                            ctypes.sizeof(info)) \
                and kernel32.AssignProcessToJobObject(job, int(handle)):
            return job
        kernel32.CloseHandle(job)
    except (OSError, AttributeError, ValueError, TypeError, ImportError):
        pass
    return None


class _Tree:
    """The node and the processes it starts. Never raises."""

    def __init__(self, proc):
        self.proc = proc
        self.job = _job_for(proc)

    def kill(self) -> None:
        if self.job is not None:
            try:
                _win32()[1].TerminateJobObject(self.job, 1)
            except (OSError, AttributeError, ValueError, TypeError):
                pass
        try:
            self.proc.kill()
        except OSError:
            pass

    def close(self) -> None:
        """Release the job; with KILL_ON_JOB_CLOSE this ends any process the node left behind."""
        job, self.job = self.job, None
        if job is not None:
            try:
                _win32()[1].CloseHandle(job)
            except (OSError, AttributeError, ValueError, TypeError):
                pass


def _reap(proc, timeout: float = REAP_SEC) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def _close_quietly(pipe) -> None:
    try:
        pipe.close()
    except (OSError, ValueError, AttributeError):
        pass


@dataclass
class _StreamEnd:
    result: dict | None  # the result event, if one arrived
    stop: str            # error sentence when the node was cut before a result (stall or time limit)
    ended: str           # eof, exited (stdout held by another process), stall, timeout, result_grace
    killed: bool         # we ended the node (and its job) ourselves
    stderr: bytes
    pipes_left: int      # reader threads still blocked on a pipe another process holds


def _stream(proc, data: bytes, cfg: NodeConfig, progress: _Progress, clock, tree: _Tree) -> _StreamEnd:
    """Feed the request, read stream events until the node is done or stopped, then collect its exit.

    Each reader thread closes its own pipe when it is done. The main thread never closes a pipe a
    reader may still be blocked on: BufferedReader.close waits for the lock that read holds, so it
    would hang for as long as another process keeps the pipe open.
    """
    lines: queue.Queue = queue.Queue()
    err_chunks: list = []

    def feed():
        try:
            proc.stdin.write(data)
        except (OSError, ValueError):
            pass
        finally:
            _close_quietly(proc.stdin)

    def pump_out():
        try:
            for raw_line in proc.stdout:
                lines.put(raw_line)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)
            _close_quietly(proc.stdout)

    def pump_err():
        try:
            err_chunks.append(proc.stderr.read())
        except (OSError, ValueError):
            pass
        finally:
            _close_quietly(proc.stderr)

    threads = [threading.Thread(target=target, name=f"durumi-node-{name}", daemon=True)
               for name, target in (("stdin", feed), ("stdout", pump_out), ("stderr", pump_err))]
    for thread in threads:
        thread.start()
    result, result_at, exited_at = None, 0.0, None
    stop, ended = "", ""
    last_line = progress.started
    while True:
        try:
            item = lines.get(timeout=POLL_SEC)
        except queue.Empty:
            item = b""
        now = clock()
        if item is None:
            ended = "eof"
            break
        text = item.decode("utf-8", errors="replace").strip() if item else ""
        if text:
            last_line = now
            try:
                event = json.loads(text)
            except ValueError:
                event = None
            if isinstance(event, dict):
                progress.event(event)
                if result is None and event.get("type") == "result":
                    result, result_at = event, now
            else:
                progress.raw_line(text)
        if exited_at is None and proc.poll() is not None:
            exited_at = now
        if exited_at is not None and not item and now - exited_at > PIPE_DRAIN_SEC:
            ended = "exited"  # the queue is empty, the node is gone, stdout is still open elsewhere
            break
        if result is not None:
            # The node has answered. Stall and time limits no longer apply; only a bounded wait to exit.
            if now - result_at > RESULT_GRACE_SEC:
                ended = "result_grace"
                break
            continue
        if now - progress.started > cfg.timeout_sec:
            stop, ended = f"노드가 제한 시간 {cfg.timeout_sec}초 안에 끝나지 않았습니다.", "timeout"
            break
        if now - last_line > cfg.stall_sec:
            stop, ended = f"노드가 {cfg.stall_sec}초 동안 아무 이벤트도 내지 않아 멈춘 것으로 보고 끊었습니다.", "stall"
            break

    killed = False
    if ended in ("stall", "timeout", "result_grace"):
        tree.kill()
        killed = True
        _reap(proc)
    else:
        wait = max(1.0, RESULT_GRACE_SEC - (clock() - result_at)) if result is not None else KILL_WAIT_SEC
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            tree.kill()
            killed = True
            _reap(proc)
    tree.close()
    deadline = time.monotonic() + THREAD_JOIN_SEC  # real time: an injected clock may stand still
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    pipes_left = sum(thread.is_alive() for thread in threads[1:])
    return _StreamEnd(result=result, stop=stop, ended=ended, killed=killed,
                      stderr=b"".join(chunk for chunk in err_chunks if chunk), pipes_left=pipes_left)


def _closing_note(end: _StreamEnd, returncode) -> str:
    """Last line of the progress log."""
    if end.stop:
        return end.stop
    parts = []
    if end.ended == "result_grace":
        parts.append(f"결과를 받은 뒤 {RESULT_GRACE_SEC}초 안에 끝나지 않아 끊었습니다. 받은 결과는 씁니다.")
    elif end.killed:
        wait = RESULT_GRACE_SEC if end.result is not None else KILL_WAIT_SEC
        parts.append(f"출력이 끝난 뒤 {wait}초 안에 끝나지 않아 끊었습니다.")
    if end.ended == "exited" or end.pipes_left:
        parts.append("노드는 끝났지만 다른 프로세스가 출력 파이프를 쥐고 있었습니다.")
    parts.append(f"끝, 종료 코드 {returncode}")
    return " ".join(parts)


def _node_result(end: _StreamEnd, returncode, duration: float, stream: dict, schema) -> NodeResult:
    stream = dict(stream, ended=end.ended, result_seen=end.result is not None)
    if end.killed:
        stream["killed"] = True
    if end.pipes_left:
        stream["pipes_left"] = end.pipes_left
    result = NodeResult(ok=False, stderr=end.stderr.decode("utf-8", errors="replace"), duration_sec=duration,
                        returncode=returncode, stream=stream)
    raw = end.result
    if isinstance(raw, dict):
        # Kept whenever a result arrived, so its cost and tokens reach the report even on failure.
        model_usage = raw.get("modelUsage") if isinstance(raw.get("modelUsage"), dict) else {}
        result.raw = raw
        result.usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        result.cost = raw.get("total_cost_usd")
        result.model_ids = sorted(model_usage)
    if end.stop:
        result.error = end.stop
        return result
    if raw is None:
        result.error = f"claude 출력에 결과 이벤트가 없습니다(종료 코드 {returncode})."
        if stream.get("non_json_lines"):
            result.error += f" JSON이 아닌 줄이 {stream['non_json_lines']}개 있었습니다(보고서의 stdout_tail)."
        return result
    # An exit code caused by our own kill after the result says nothing about the node's answer.
    exit_failed = returncode != 0 and not end.killed
    if exit_failed or raw.get("is_error"):
        result.error = f"노드가 오류로 끝났습니다({raw.get('subtype', '알 수 없음')}, 종료 코드 {returncode})."
        return result
    if schema is None:
        result.ok = True
        return result
    result.envelope = _envelope_from(raw)
    if result.envelope is None:
        result.error = "노드 결과에서 봉투 JSON을 찾지 못했습니다."
        return result
    result.ok = True
    return result


def _run(binary, system_prompt: str, request: str, cfg: NodeConfig, schema, workdir: Path,
         progress_path=None, popen=subprocess.Popen, clock=time.monotonic) -> NodeResult:
    marker = _repo_marker(workdir.resolve())
    if marker is not None:
        return NodeResult(ok=False, error=f"노드 작업 폴더가 저장소 안에 있습니다: {workdir} (저장소 {marker})")
    try:
        data = request.encode("utf-8")
    except UnicodeEncodeError as exc:
        return NodeResult(ok=False, error=f"요청을 UTF-8로 바꾸지 못했습니다: {exc}")
    prompt_path = workdir / SYSTEM_PROMPT_NAME
    try:
        prompt_path.write_bytes(system_prompt.encode("utf-8"))
    except (OSError, UnicodeEncodeError) as exc:
        return NodeResult(ok=False, error=f"시스템 프롬프트 파일을 쓰지 못했습니다: {exc}")
    command = build_command(binary, prompt_path, cfg, schema)
    progress = _Progress(progress_path, clock, clock())
    try:
        proc = popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     cwd=str(workdir), env=node_env())
    except (OSError, ValueError) as exc:
        progress.close(f"claude를 실행하지 못했습니다. {exc}")
        return NodeResult(ok=False, error=f"claude를 실행하지 못했습니다: {exc}")
    tree = _Tree(proc)
    try:
        end = _stream(proc, data, cfg, progress, clock, tree)
    except BaseException:
        # A failed progress write no longer lands here, but KeyboardInterrupt or a bug still could.
        # The node must not outlive the call either way.
        try:
            tree.kill()
            _reap(proc)
        finally:
            tree.close()
            progress.close("예외로 멈춰 노드를 끊었습니다.")
        raise
    progress.finish()
    duration = round(clock() - progress.started, 3)
    progress.close(_closing_note(end, proc.returncode))
    return _node_result(end, proc.returncode, duration, progress.summary(), schema)


def _remove_workdir(path: Path) -> None:
    """Remove a node's temp folder. Windows keeps a folder a just-ended process used as its cwd for a moment."""
    for delay in (0.0, 0.2, 0.5):
        if delay:
            time.sleep(delay)
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return


def call_node(binary, system_prompt: str, request: str, cfg: NodeConfig, schema: dict | None,
              workdir_outside_repo: Path | None = None, progress_path=None) -> NodeResult:
    """Run one isolated node. Without a workdir, a fresh temp folder is made and removed afterwards.

    progress_path, when given, receives a progress log (the run folder keeps it next to the attempt).
    """
    try:
        if workdir_outside_repo is not None:
            workdir = Path(workdir_outside_repo)
            marker = _repo_marker(workdir.resolve())
            if marker is not None:  # checked before mkdir, so a refused folder is never created
                return NodeResult(ok=False, error=f"노드 작업 폴더가 저장소 안에 있습니다: {workdir} (저장소 {marker})")
            workdir.mkdir(parents=True, exist_ok=True)
            return _run(binary, system_prompt, request, cfg, schema, workdir, progress_path=progress_path)
        workdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    except OSError as exc:
        return NodeResult(ok=False, error=f"노드 작업 폴더를 만들지 못했습니다: {exc}")
    try:
        return _run(binary, system_prompt, request, cfg, schema, workdir, progress_path=progress_path)
    finally:
        _remove_workdir(workdir)


def input_tokens(usage: dict) -> int:
    """All prompt-side tokens: fresh input plus cache writes and reads."""
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total


def probe(binary, cfg: NodeConfig | None = None, root=None) -> dict:
    """Isolation canary. A CLAUDE.md with the canary sits in the node's cwd; a leak or a fat prompt fails."""
    if cfg is None:
        try:
            base = load_node(find_root()[0] if root is None else root, "draft")
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        # stall_sec is lowered with the time limit; left at the draft's 600 it would exceed 300 and never fire.
        cfg = dataclasses.replace(base, effort="low", timeout_sec=300, stall_sec=min(base.stall_sec, PROBE_STALL_SEC),
                                  max_budget_usd=1.0)
    workdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        (workdir / "CLAUDE.md").write_bytes(
            f"# CLAUDE.md\n\n{PROBE_CANARY} 이 줄이 보이면 격리가 깨진 것입니다.\n".encode("utf-8")
        )
        result = _run(binary, PROBE_SYSTEM_PROMPT, PROBE_QUESTION, cfg, None, workdir)
    finally:
        _remove_workdir(workdir)
    answer = ""
    if result.raw and isinstance(result.raw.get("result"), str):
        answer = result.raw["result"]
    tokens = input_tokens(result.usage)
    leaked = PROBE_CANARY in answer
    problems = []
    if not result.ok:
        problems.append(result.error or "노드 호출이 실패했습니다.")
    if leaked:
        problems.append("답에 카나리아가 나타났습니다. 격리가 깨졌습니다.")
    if result.ok and tokens > PROBE_TOKEN_LIMIT:
        problems.append(f"입력 토큰이 {tokens}개로 기준 {PROBE_TOKEN_LIMIT}개를 넘었습니다.")
    return {
        "ok": not problems,
        "answer": answer,
        "canary_leaked": leaked,
        "input_tokens": tokens,
        "usage": result.usage,
        "cost": result.cost,
        "model_ids": result.model_ids,
        "duration_sec": result.duration_sec,
        "error": " ".join(problems),
    }
