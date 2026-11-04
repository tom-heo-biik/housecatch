"""Issue records shared by the parser, linters and document checker."""
from __future__ import annotations

from dataclasses import asdict, dataclass

ERROR = "error"
WARN = "warn"
INFO = "info"


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    message: str
    where: str = ""
    excerpt: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def excerpt(text: str, limit: int = 20) -> str:
    """Collapse whitespace and cut to `limit` characters for issue reports."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def has_errors(issues) -> bool:
    return any(issue.severity == ERROR for issue in issues)


def errors_only(issues) -> list:
    return [issue for issue in issues if issue.severity == ERROR]
