"""Paths and data loading for durumi. Importing this module has no side effects."""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
SELF_ROOT = TOOLS_DIR.parent
HEX = re.compile(r"^#[0-9A-F]{6}$")


class ConfigError(Exception):
    """Raised when data files are missing or malformed."""


@dataclass(frozen=True)
class Palette:
    key: str
    label: str
    text: str
    highlight: str
    text_pantone: str
    highlight_pantone: str


@dataclass(frozen=True)
class Rules:
    palettes: dict
    placeholder: str
    form: dict


@dataclass(frozen=True)
class Brand:
    id: str
    name: str
    aliases: tuple
    palettes: tuple | None
    banned_phrases: tuple
    watch_phrases: tuple
    dir: Path
    doc_path: Path
    # brand.toml still has topic_cards, which brands/{id}/topics.json replaced; the cards are not read
    # (pipeline.load_topics stops the requests that hand the topic over until the key is gone).
    legacy_cards: bool = False


@dataclass(frozen=True)
class NodeConfig:
    name: str
    model: str
    effort: str
    timeout_sec: int
    max_budget_usd: float
    retries: int
    include: tuple
    stall_sec: int = 600  # no stream event for this long means the node is stuck


def is_durumi_root(path: Path) -> bool:
    manifest = Path(path) / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("name") == "durumi"
    except (OSError, ValueError):
        return False


def _working_tree(path: Path) -> bool:
    """A durumi root that people edit: not a deploy copy, not a copy in the plugin cache."""
    return is_durumi_root(path) and not is_deploy_copy(path) and not in_plugin_cache(path)


def _in_project(project) -> Path | None:
    """The durumi root inside a Claude Code project folder (the monorepo), or the folder itself."""
    for candidate in (Path(project) / "projects" / "durumi", Path(project)):
        if _working_tree(candidate):
            return candidate.resolve()
    return None


SOURCE_MARKER = ".durumi-source.json"  # written into a deploy copy by tools/deploy.py


def deployed_source(root) -> Path | None:
    """The folder a deploy copy was built from, when it still is a durumi root on this machine."""
    try:
        source = json.loads((Path(root) / SOURCE_MARKER).read_text(encoding="utf-8")).get("source")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(source, str) and source and is_durumi_root(Path(source)):
        return Path(source).resolve()
    return None


def find_root(env=None, cwd=None, project=None, self_root=None) -> tuple[Path, str]:
    """Return (root, kind). kind is 'env', 'live' (monorepo working tree), 'source' or 'self'.

    Order: DURUMI_ROOT; the project folder given on the command line (both skills pass
    ${CLAUDE_PROJECT_DIR}, which Claude Code substitutes, so this is the usual path from the
    monorepo whatever the shell's current folder is); CLAUDE_PROJECT_DIR in the environment (set for
    hooks, but absent from commands the Bash tool runs); a walk up from the current directory; the
    source folder a deploy copy records ('source'), so the installed plugin used outside the
    monorepo still runs on the working tree; and last this tools folder's parent ('self'). A 'self'
    root that is a deploy copy or lies in the plugin cache keeps its runs elsewhere (see runs_root).
    'live' is only ever a working tree: a copy met in the project folder or on the walk is passed
    over, so a shell opened in dist/marketplace/durumi still finds the source folder above it.
    """
    self_root = SELF_ROOT if self_root is None else Path(self_root)
    env = os.environ if env is None else env
    explicit = env.get("DURUMI_ROOT")
    if explicit and is_durumi_root(Path(explicit)):
        return Path(explicit).resolve(), "env"
    for folder in (project, env.get("CLAUDE_PROJECT_DIR")):
        if folder:
            found = _in_project(folder)
            if found is not None:
                return found, "live"
    start = Path.cwd() if cwd is None else Path(cwd)
    for folder in (start, *start.parents):
        if _working_tree(folder):
            return folder.resolve(), "live"
        live = folder / "projects" / "durumi"
        if _working_tree(live):
            return live.resolve(), "live"
    source = deployed_source(self_root)
    if source is not None:
        return source, "source"
    return self_root, "self"


def is_deploy_copy(root) -> bool:
    return (Path(root) / SOURCE_MARKER).is_file()


def runs_root(root) -> Path:
    """Where run folders go: root/runs, except for a root that is rebuilt or removed as a whole.

    A deploy copy is emptied by every deploy and a plugin-cache copy by every update, so their runs
    go to the user's own folder instead (~/.durumi/runs).
    """
    root = Path(root)
    if is_deploy_copy(root) or in_plugin_cache(root):
        return Path.home() / ".durumi" / "runs"
    return root / "runs"


def in_plugin_cache(path) -> bool:
    """True when `path` lies in Claude Code's plugin cache (.claude/plugins/cache/...).

    Files written there (runs/) vanish when the plugin is updated and the old version folder is removed.
    """
    parts = [part.casefold() for part in Path(path).resolve().parts]
    return any(parts[i:i + 3] == [".claude", "plugins", "cache"] for i in range(len(parts) - 2))


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"파일이 없습니다: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 문법 오류입니다: {path} ({exc})") from exc


def _hex(value: str, where: str) -> str:
    value = str(value).upper()
    if not HEX.match(value):
        raise ConfigError(f"색은 6자리 hex여야 합니다: {where} = {value}")
    return value


def _read_rule_file(path: Path) -> dict:
    """One file of rules/ as TOML (_read_toml), and ConfigError naming it when it is not UTF-8 or cannot be read."""
    try:
        return _read_toml(path)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"규칙 파일을 UTF-8로 읽을 수 없습니다. {path}") from exc
    except OSError as exc:  # a folder in its place, no permission; a missing file is _read_toml's ConfigError
        # Callers put '설정을 읽지 못했습니다.' before it, so this says what failed in other words.
        raise ConfigError(f"규칙 파일을 열 수 없습니다. {path} ({exc})") from exc


def _rule_table(value, where: str, path: Path) -> dict:
    """value when it is a TOML table; ConfigError naming where and the file otherwise."""
    if not isinstance(value, dict):
        raise ConfigError(f"규칙 파일의 {where} 항목이 TOML 테이블이 아닙니다. {path}")
    return value


def _rule_value(table: dict, key: str, where: str, path: Path):
    """table[key]; ConfigError naming where and the file when the key is missing."""
    if key not in table:
        raise ConfigError(f"규칙 파일에 {where} 항목이 없습니다. {path}")
    return table[key]


def load_rules(root: Path) -> Rules:
    """rules/palettes.toml and rules/form.toml as Rules.

    So that check, run and the other commands end with a sentence instead of a traceback, these are a
    ConfigError: a file that is missing, cannot be read, is not UTF-8 or is not TOML; palettes or one
    palette that is not a table; a missing placeholder, or a palette without text or highlight (each of
    these names the file); a color that is not six hex digits (named by its key); no palette at all.
    What form.toml holds is not judged here.
    """
    root = Path(root)
    pal_path = root / "rules" / "palettes.toml"
    pal = _read_rule_file(pal_path)
    form = _read_rule_file(root / "rules" / "form.toml")
    palettes = {}
    for key, item in _rule_table(pal.get("palettes", {}), "palettes", pal_path).items():
        where = f"palettes.{key}"
        item = _rule_table(item, where, pal_path)
        palettes[key] = Palette(
            key=key,
            label=item.get("label", key),
            text=_hex(_rule_value(item, "text", f"{where}.text", pal_path), f"{where}.text"),
            highlight=_hex(_rule_value(item, "highlight", f"{where}.highlight", pal_path), f"{where}.highlight"),
            text_pantone=item.get("text_pantone", ""),
            highlight_pantone=item.get("highlight_pantone", ""),
        )
    if not palettes:
        raise ConfigError("팔레트가 하나도 없습니다: rules/palettes.toml")
    placeholder = _hex(_rule_value(pal, "placeholder", "placeholder", pal_path), "placeholder")
    return Rules(palettes=palettes, placeholder=placeholder, form=form)


def load_brand(root: Path, brand_id: str) -> Brand:
    """brands/{brand_id}/brand.toml as a Brand.

    A topic_cards key, whatever its value, only sets legacy_cards: the cards are not read, so that no
    value of it can stop check and run for every brand (list_brands reads them all). The brand's topics
    are in topics.json, which only a request that hands the topic over to the brand reads.
    """
    root = Path(root)
    bdir = root / "brands" / brand_id
    data = _read_toml(bdir / "brand.toml")
    if data.get("id") != brand_id:
        raise ConfigError(f"brand.toml의 id가 폴더 이름과 다릅니다: {bdir}")
    pal = data.get("palettes")
    return Brand(
        id=brand_id,
        name=data.get("name", brand_id),
        aliases=tuple(data.get("aliases", [])),
        palettes=tuple(pal) if pal else None,
        banned_phrases=tuple(data.get("banned_phrases", [])),
        watch_phrases=tuple(data.get("watch_phrases", [])),
        dir=bdir,
        doc_path=bdir / "brand.md",
        legacy_cards="topic_cards" in data,
    )


def list_brands(root: Path) -> list[Brand]:
    base = Path(root) / "brands"
    if not base.is_dir():
        return []
    return [load_brand(root, p.name) for p in sorted(base.iterdir()) if (p / "brand.toml").is_file()]


def load_node(root: Path, name: str) -> NodeConfig:
    data = _read_toml(Path(root) / "nodes" / f"{name}.toml").get("node", {})
    try:
        cfg = NodeConfig(
            name=data["name"],
            model=data["model"],
            effort=data.get("effort", "max"),
            timeout_sec=int(data.get("timeout_sec", 900)),
            max_budget_usd=float(data.get("max_budget_usd", 5.0)),
            retries=int(data.get("retries", 2)),
            include=tuple(data["include"]),
            stall_sec=int(data.get("stall_sec", 600)),
        )
    except KeyError as exc:
        raise ConfigError(f"노드 설정에 {exc} 항목이 없습니다: nodes/{name}.toml") from exc
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"노드 설정의 값이 숫자가 아닙니다: nodes/{name}.toml ({exc})") from exc
    # A zero or negative limit would cut every call at its first check.
    for key in ("timeout_sec", "stall_sec"):
        if getattr(cfg, key) <= 0:
            raise ConfigError(f"노드 설정의 {key}는 0보다 커야 합니다: nodes/{name}.toml")
    if cfg.retries < 0:
        raise ConfigError(f"노드 설정의 retries는 0 이상이어야 합니다: nodes/{name}.toml")
    return cfg
