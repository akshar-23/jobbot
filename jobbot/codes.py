"""Security codes you type into the dashboard, and CAPTCHAs you solve, for applications waiting on one.

Greenhouse sometimes asks an applicant to confirm they're a person: after Submit it emails an
8-character code. jobbot never reads your email; the bot keeps that application open, the
dashboard asks you for the code, and the bot types what you entered. You do the checking;
the bot only saves you from switching windows.

The bot (a separate process) and the dashboard share files in .jobbot/codes/:
  <job id>.json   a request: company, role, where the code was sent, status, message
  <job id>.code   the code you entered, picked up (and deleted) by the bot
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from jobbot.paths import PROJECT_DIR

CODES_DIR = PROJECT_DIR / ".jobbot" / "codes"
CODE_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def _path(jid: str, suffix: str) -> Path:
    if not _ID_RE.match(jid or ""):
        raise ValueError("bad job id")
    return CODES_DIR / f"{jid}{suffix}"


def request(jid: str, company: str, title: str, sent_to: str, minutes: int, kind: str = "code") -> None:
    """The bot is waiting on you for this application: kind "code" (type the emailed code here) or
    "check" (solve the CAPTCHA yourself in the jobbot browser window)."""
    CODES_DIR.mkdir(parents=True, exist_ok=True)
    _path(jid, ".code").unlink(missing_ok=True)  # an old code from an earlier attempt is stale
    update(jid, company=company, title=title, sent_to=sent_to, status="waiting", message="", kind=kind,
           asked=time.time(), until=time.time() + minutes * 60)


def update(jid: str, **fields) -> None:
    p = _path(jid, ".json")
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data.update(fields, id=jid)
    CODES_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def clear(jid: str) -> None:
    _path(jid, ".json").unlink(missing_ok=True)
    _path(jid, ".code").unlink(missing_ok=True)


def waiting() -> list[dict]:
    """Requests the dashboard shows (expired ones are dropped)."""
    out = []
    if not CODES_DIR.exists():
        return out
    for p in sorted(CODES_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("until", 0) < time.time():
            p.unlink(missing_ok=True)
            continue
        data["minutes_left"] = max(0, int((data["until"] - time.time()) // 60))
        data["entered"] = _path(data["id"], ".code").exists()
        out.append(data)
    return out


def provide(jid: str, code: str) -> None:
    """You typed the code into the dashboard."""
    code = re.sub(r"[\s-]", "", code or "")
    if not CODE_RE.match(code):
        raise ValueError("That doesn't look like the code: letters and digits only, as in the email.")
    if not _path(jid, ".json").exists():
        raise ValueError("That application isn't waiting for a code any more.")
    _path(jid, ".code").write_text(code, encoding="utf-8")
    update(jid, status="checking", message="")


def take(jid: str) -> str | None:
    """The bot picks up a code you entered (once)."""
    p = _path(jid, ".code")
    if not p.exists():
        return None
    code = p.read_text(encoding="utf-8").strip()
    p.unlink(missing_ok=True)
    return code or None
