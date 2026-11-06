"""Structured errors and progress for the web UI's long actions (#95).

Every failing API call answers with the same envelope::

    {"code": "ai_timeout", "message": "...", "detail": "...",
     "request_id": "a1b2c3d4", "time": "2026-10-10T08:00:00+00:00",
     "error": "..."}          # ``error`` repeats ``message`` for old clients

``code`` is one of a small, stable set (see :data:`CODES`) so the browser can
say *why* something failed in plain words instead of a generic "error".
The full cause is logged server-side (redacted - no tokens, cookies, keys);
the client only receives the redacted ``detail``.

The browser-side mirror of :func:`classify_exception` lives in
``webui_static/suno-genre-core.js`` (``classifyError``); the two share these
codes so the HA card and the web page speak the same language.
"""

from __future__ import annotations

import errno
import logging
import re
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger("suno_dl.webui")

# code -> (HTTP status, plain-words cause class)
CODES: dict[str, tuple[int, str]] = {
    "ai_timeout": (504, "AI service timeout"),
    "ai_unreachable": (502, "AI service unreachable"),
    "ai_invalid_reply": (502, "AI service sent an invalid reply"),
    "ai_empty_reply": (502, "AI service sent an empty reply"),
    "file_permission": (403, "File or permission problem"),
    "bad_request": (400, "Bad request"),
    "not_found": (404, "Not found"),
    "forbidden_host": (403, "Host not allowed"),
    "busy": (409, "Another action is already running"),
    "cancelled": (409, "Cancelled"),
    "unexpected": (500, "Unexpected error"),
}

_MAX_DETAIL = 1200

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"), "[redacted-jwt]"),
    (
        re.compile(r"\b(sk|pk|rk|ghp|gho|ghs|github_pat|xox[abpr])[-_][A-Za-z0-9_-]{12,}"),
        "[redacted-key]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
            r"cookie|set-cookie|authorization|session|sess|__client[a-z_]*)\b(\s*[:=]\s*)([^\s,;&\"']+)"
        ),
        r"\1\2[redacted]",
    ),
    (
        re.compile(r"([?&](?:token|key|api_key|access_token|sig|signature)=)[^&\s]+", re.I),
        r"\1[redacted]",
    ),
)


def redact(text: object) -> str:
    """Mask tokens, keys, cookies and credentials in free text."""
    out = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def new_request_id() -> str:
    """Short id the user can quote and the server log carries."""
    return uuid.uuid4().hex[:8]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class ApiError(Exception):
    """A failure with a stable ``code`` and a plain-words ``message``."""

    code: str
    message: str
    detail: str = ""
    status: int | None = None

    def __post_init__(self) -> None:
        if self.code not in CODES:
            self.code = "unexpected"
        if self.status is None:
            self.status = CODES[self.code][0]
        Exception.__init__(self, self.message)

    @property
    def cause_class(self) -> str:
        return CODES[self.code][1]


def error_body(err: ApiError, request_id: str | None = None) -> dict[str, object]:
    """The JSON envelope sent for ``err`` (detail is redacted and capped)."""
    message = redact(err.message)
    return {
        "code": err.code,
        "message": message,
        "detail": redact(err.detail)[:_MAX_DETAIL],
        "request_id": request_id or new_request_id(),
        "time": now_iso(),
        "cause_class": err.cause_class,
        "error": message,
    }


_TIMEOUT_RE = re.compile(r"time(d)?[ -]?out|etimedout|deadline", re.I)
_PERM_RE = re.compile(
    r"eacces|eperm|permission|forbidden|read-only|readonly|not writable|enoent|enospc|"
    r"no such file|no space|disk full|unauthori[sz]ed",
    re.I,
)
_UNREACH_RE = re.compile(
    r"unreachable|econnrefused|econnreset|enotfound|no route|"
    r"connection (error|refused|reset|closed)|"
    r"failed to fetch|network|offline|no deployments|service unavailable|bad gateway|"
    r"internalservererror|name or service not known|agent .*not found",
    re.I,
)
_EMPTY_RE = re.compile(r"empty (reply|response)|junk|no (reply|response|speech)", re.I)
_INVALID_RE = re.compile(
    r"json|parse|did not return|invalid (reply|response)|unexpected token", re.I
)


def classify_exception(exc: BaseException) -> ApiError:
    """Map any exception to an :class:`ApiError` (cause class + safe detail)."""
    if isinstance(exc, ApiError):
        return exc
    text = f"{type(exc).__name__}: {exc}"
    detail = redact(text)
    if isinstance(exc, (TimeoutError, socket.timeout)) or _TIMEOUT_RE.search(text):
        return ApiError("ai_timeout", "The AI service took too long to answer.", detail)
    if isinstance(exc, PermissionError) or (
        isinstance(exc, OSError)
        and exc.errno in (errno.EACCES, errno.EPERM, errno.ENOENT, errno.ENOSPC, errno.EROFS)
    ):
        return ApiError(
            "file_permission",
            "A file could not be read or written (permission or missing path).",
            detail,
        )
    if isinstance(exc, (ConnectionError, socket.gaierror)):
        return ApiError("ai_unreachable", "The AI service could not be reached.", detail)
    if _PERM_RE.search(text):
        return ApiError(
            "file_permission",
            "A file could not be read or written (permission or missing path).",
            detail,
        )
    if _UNREACH_RE.search(text):
        return ApiError("ai_unreachable", "The AI service could not be reached.", detail)
    if _EMPTY_RE.search(text):
        return ApiError("ai_empty_reply", "The AI service sent an empty or unusable reply.", detail)
    if _INVALID_RE.search(text):
        return ApiError(
            "ai_invalid_reply", "The AI service sent a reply that could not be read.", detail
        )
    return ApiError("unexpected", "Something unexpected went wrong.", detail)


def classify_run_failure(
    returncode: int | None, output: list[str], *, cancelled: bool = False
) -> ApiError:
    """Explain a failed CLI run from its exit code and last output lines."""
    if cancelled:
        return ApiError(
            "cancelled", "The run was cancelled before it finished.", "cancelled from the web UI"
        )
    tail = "\n".join(output[-12:])
    err = classify_exception(RuntimeError(tail or f"exit code {returncode}"))
    if err.code == "unexpected":
        err = ApiError(
            "unexpected",
            f"The run stopped with exit code {returncode}.",
            redact(tail) or f"exit code {returncode}",
        )
    return err


_PROGRESS_RES = (
    re.compile(r"\[(\d+)\s*/\s*(\d+)\]"),
    re.compile(r"\b(\d+)\s+of\s+(\d+)\b", re.I),
    re.compile(r"\|\s*(\d+)\s*/\s*(\d+)\s*\["),  # tqdm: 12%|###| 5/42 [..]
    re.compile(r"\b(\d+)\s*/\s*(\d+)\b"),
)


def parse_progress(output: list[str]) -> dict[str, object] | None:
    """Latest ``done/total`` mentioned in a run's output, if any."""
    for line in reversed(output[-60:]):
        for pattern in _PROGRESS_RES:
            match = pattern.search(line)
            if match:
                done, total = int(match.group(1)), int(match.group(2))
                if total > 0 and done <= total:
                    return {"done": done, "total": total, "label": redact(line.strip())[:160]}
    return None
