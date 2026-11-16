"""Runs the application bot from the dashboard, so applying doesn't need a terminal.

The bot needs Playwright, which lives in .venv; the dashboard runs on plain python3. So a
run is `.venv/bin/python -u -m jobbot apply [--dry-run] [--id …]` in a subprocess, its
output kept here for the dashboard to show. One run at a time; roles picked while a run is
going are queued and run right after it.

Whether a form is actually submitted is still decided by the bot: only handlers listed in
profile/bot.toml `live_handlers`, within the daily caps. Everything else is a dry run.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from jobbot.paths import PROJECT_DIR

VENV_PYTHON = PROJECT_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
HANDLERS = ["greenhouse", "ashby", "lever"]
LOG_PATH = PROJECT_DIR / ".jobbot" / "bot_runs.log"


@dataclass
class State:
    running: bool = False
    started: str = ""
    finished: str = ""
    dry_run: bool = False
    ids: list[str] = field(default_factory=list)
    lines: deque = field(default_factory=lambda: deque(maxlen=200))
    returncode: int | None = None
    queued: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {"running": self.running, "started": self.started, "finished": self.finished,
                "dry_run": self.dry_run, "count": len(self.ids), "lines": list(self.lines)[-60:],
                "returncode": self.returncode, "queued": len(self.queued), "installed": VENV_PYTHON.exists()}


STATE = State()
_LOCK = threading.Lock()


def start(ids: list[str] | None = None, dry_run: bool = False) -> tuple[bool, str]:
    """Start a bot run (ids=None: every To apply row). Queues the ids if one is running."""
    if not VENV_PYTHON.exists():
        return False, "The bot isn't installed yet (no .venv). Ask Claude to run setup, or see README."
    with _LOCK:
        if STATE.running:
            new = [i for i in (ids or []) if i not in STATE.queued]
            STATE.queued += new
            return True, f"A run is going; {len(new)} role(s) queued for right after it." if ids else \
                "A run is already going."
        STATE.running, STATE.dry_run, STATE.ids = True, dry_run, list(ids or [])
        STATE.started, STATE.finished, STATE.returncode = time.strftime("%H:%M:%S"), "", None
        STATE.lines.clear()
    cmd = [str(VENV_PYTHON), "-u", "-m", "jobbot", "apply"] + (["--dry-run"] if dry_run else [])
    for i in ids or []:
        cmd += ["--id", i]
    threading.Thread(target=_work, args=(cmd,), daemon=True).start()
    return True, "Started."


def _work(cmd: list[str]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd[3:])}\n")
            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}  # Windows consoles default to cp1252
            proc = subprocess.Popen(cmd, cwd=PROJECT_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, encoding="utf-8", errors="replace",
                                    text=True, bufsize=1)
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    STATE.lines.append(line)
                    log.write(line + "\n")
            STATE.returncode = proc.wait()
    except OSError as e:
        STATE.lines.append(f"couldn't start the bot: {e}")
        STATE.returncode = -1
    finally:
        with _LOCK:
            STATE.running, STATE.finished = False, time.strftime("%H:%M:%S")
            queued, STATE.queued = STATE.queued, []
    if queued:
        start(queued, STATE.dry_run)


# ---------------------------------------------------------------------------
# The bot's visible browser window
# ---------------------------------------------------------------------------

BOT_BROWSER_APPS = ("Google Chrome for Testing", "Chromium")  # what Playwright's browser is called on a Mac


def show_bot_window() -> bool:
    """Bring the bot's browser window in front of your other windows (macOS). Never starts it."""
    import sys
    if sys.platform != "darwin":
        return False  # Windows: it's the "Chrome for Testing" button on the taskbar
    for app in BOT_BROWSER_APPS:
        script = f'if application "{app}" is running then tell application "{app}" to activate'
        if subprocess.run(["osascript", "-e", script], capture_output=True).returncode == 0 and \
                subprocess.run(["pgrep", "-f", app], capture_output=True).returncode == 0:
            return True
    return False


# ---------------------------------------------------------------------------
# bot.toml settings the dashboard may change (your own clicks)
# ---------------------------------------------------------------------------

def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(f'"{v}"' for v in value) + "]"
    return str(int(value))


def set_bot_settings(changes: dict) -> dict:
    """Write live_handlers / auto_run_on_pick / daily caps into bot.toml, keeping its comments."""
    allowed = {"live_handlers", "auto_run_on_pick", "daily_limit", "per_company_per_day",
               "accept_privacy_notices", "accept_other_agreements", "accept_arbitration_agreements",
               "solve_checks_myself"}
    from jobbot import profile as person
    target = person.PROFILE_DIR / "bot.toml"
    source = person.path("bot.toml")  # before setup: start from the example's settings
    text = source.read_text(encoding="utf-8") if source.exists() else ""
    for key, value in changes.items():
        if key not in allowed:
            continue
        if key == "live_handlers":
            value = [h for h in value if h in HANDLERS]
        line = f"{key} = {_toml_value(value)}"
        pattern = re.compile(rf"^{key}\s*=\s*[^#\n]*?(\s*#.*)?$", re.M)
        if pattern.search(text):
            text = pattern.sub(lambda m: line + (("  " + m.group(1).strip()) if m.group(1) else ""), text, count=1)
        else:
            text = text.rstrip("\n") + f"\n{line}\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return bot_settings()


def bot_settings() -> dict:
    from jobbot.apply.draft import find_claude
    from jobbot.apply.engine import load_bot_config
    cfg = load_bot_config()
    return {"live_handlers": cfg.get("live_handlers", []), "auto_run_on_pick": bool(cfg.get("auto_run_on_pick", False)),
            "daily_limit": cfg.get("daily_limit", 50), "per_company_per_day": cfg.get("per_company_per_day", 2),
            "handlers": HANDLERS, "bot_installed": VENV_PYTHON.exists(), "claude_found": bool(find_claude()),
            **{k: bool(cfg.get(k, False)) for k in ("accept_privacy_notices", "accept_other_agreements",
                                                     "accept_arbitration_agreements", "solve_checks_myself")}}
