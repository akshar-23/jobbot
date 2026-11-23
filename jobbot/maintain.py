"""Install, publish the template, and update from it.

  python -m jobbot install            create .venv with Playwright + Chromium for the bot
  python -m jobbot template DIR       copy the shared code (no personal files) into DIR,
                                      a separate git repo you push as the template
  python -m jobbot update [--from URL]  pull the latest shared code from the template repo

The template holds only the code and the example profile. Personal files (profile/,
tracker.csv, resumes/, cover_letters/, applications/, scraping/postings/ and state/)
never go into it, and `template` refuses to finish if your name, email, phone or links
show up in what it copied.

Updates don't merge histories (a repo made with "Use this template" shares none with the
template); they check out the shared paths from the template's latest commit.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from jobbot.paths import PROJECT_DIR

# Everything shared between users. Personal files are everything else.
ENGINE_PATHS = [
    "jobbot", "extension", "scraping/scraper.py", "tests", ".github", ".claude/skills", "profile.example",
    "requirements.txt", "requirements-bot.txt", "README.md", "CLAUDE.md", ".gitignore", ".gitattributes",
    "Start jobbot.bat", "Install jobbot.bat", "Update jobbot.bat",
    "Start jobbot.command", "Install jobbot.command", "Update jobbot.command",
]
SKIP = {"__pycache__", ".DS_Store", ".pytest_cache"}
TEMPLATE_REMOTE = "template"


def _run(cmd: list[str], cwd: Path = PROJECT_DIR, check: bool = True) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

def install() -> int:
    from jobbot.apply.draft import find_claude
    from jobbot.runner import VENV_PYTHON
    print("Setting up the application bot (a private Python environment with a browser it can drive).")
    if not VENV_PYTHON.exists():
        _run([sys.executable, "-m", "venv", str(PROJECT_DIR / ".venv")])
    _run([str(VENV_PYTHON), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    _run([str(VENV_PYTHON), "-m", "pip", "install", "--quiet", "-r", str(PROJECT_DIR / "requirements-bot.txt")])
    _run([str(VENV_PYTHON), "-m", "playwright", "install", "chromium"])
    claude = find_claude()
    print("\nDone.")
    print(f"  Claude Code: {'found at ' + claude if claude else 'NOT FOUND: install it and sign in (see README.md)'}")
    print("  Next: start jobbot (the Start file, or `python -m jobbot serve`) and fill in the setup page.")
    return 0 if claude else 1


# ---------------------------------------------------------------------------
# template
# ---------------------------------------------------------------------------

def _personal_strings() -> list[str]:
    """Values from your profile that must never appear in the template."""
    from jobbot import profile as person
    if not person.is_set_up():
        return []
    a = person.answers()
    ident, links, addr = a.get("identity", {}), a.get("links", {}), a.get("address", {})
    values = [ident.get("first_name", "") + " " + ident.get("last_name", ""), ident.get("first_name", ""),
              ident.get("last_name", ""), ident.get("email", ""), ident.get("email", "").split("@")[0],
              ident.get("phone", ""), addr.get("line1", ""), *[v for v in links.values() if isinstance(v, str)],
              a.get("education", {}).get("school", ""), a.get("education", {}).get("undergrad_school", ""),
              a.get("current_job", {}).get("company", ""), *a.get("misc", {}).get("previously_interviewed_at", [])]
    values += [str(t) for t in person.bot().get("forbidden_terms", [])]  # NDA words
    digits = re.sub(r"\D", "", ident.get("phone", ""))
    if len(digits) >= 7:
        values.append(digits[-7:])
    return [v.strip() for v in values if v and len(v.strip()) >= 4]


def _copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        for item in src.rglob("*"):
            if any(part in SKIP for part in item.parts) or item.is_dir():
                continue
            target = dst / item.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def template(target: str) -> int:
    out = Path(target).expanduser().resolve()
    if out == PROJECT_DIR.resolve() or PROJECT_DIR.resolve() in out.parents:
        print("Choose a folder outside this project, e.g. ../jobbot-template")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    for rel in ENGINE_PATHS:  # replace shared paths wholesale so deleted files go too
        dest = out / rel
        if dest.is_dir():
            shutil.rmtree(dest)
        elif dest.exists():
            dest.unlink()
        if (PROJECT_DIR / rel).exists():
            _copy(PROJECT_DIR / rel, dest)
    # Leak check: nothing from your profile may appear in the copied files.
    leaks = []
    secrets = [s.lower() for s in _personal_strings()]
    for f in out.rglob("*"):
        if f.is_file() and ".git" not in f.parts:
            try:
                text = f.read_text(encoding="utf-8").lower()
            except (UnicodeDecodeError, OSError):
                continue
            leaks += [f"{f.relative_to(out)}: {s!r}" for s in secrets if s in text]
    if leaks:
        print("Stopped: personal details found in the template copy:\n  " + "\n  ".join(leaks[:20]))
        print("Remove them from the shared files (or the example profile) and run this again.")
        return 1
    if not (out / ".git").exists():
        _run(["git", "init", "-b", "main"], cwd=out)
    # Stage the shared paths only: anything else in that folder (a tracker.csv from a test run,
    # the nightly scrape's output) is not part of the template.
    _run(["git", "add", "-A", "--", *[rel for rel in ENGINE_PATHS if (out / rel).exists()]], cwd=out)
    gone = [rel for rel in ENGINE_PATHS if not (out / rel).exists()]
    if gone:  # shared files deleted since the last export
        _run(["git", "add", "-u", "--", *gone], cwd=out, check=False)
    changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=out).returncode != 0
    if changed:
        _run(["git", "commit", "-q", "-m", "Update jobbot from the development copy"], cwd=out)
    print(f"\nTemplate ready in {out}" + ("" if changed else " (no changes)") + ".")
    print("First time: create an empty GitHub repo, then in that folder:\n"
          "  git remote add origin <its URL> && git push -u origin main\n"
          "and turn on Settings → General → Template repository. After that: git push.")
    return 0


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

def update(url: str | None = None) -> int:
    remotes = subprocess.run(["git", "remote"], cwd=PROJECT_DIR, capture_output=True, text=True).stdout.split()
    if TEMPLATE_REMOTE not in remotes:
        if not url:
            print("First time: run  python -m jobbot update --from <the template repo's URL>")
            return 1
        _run(["git", "remote", "add", TEMPLATE_REMOTE, url])
    elif url:
        _run(["git", "remote", "set-url", TEMPLATE_REMOTE, url])
    dirty = subprocess.run(["git", "status", "--porcelain", "--", *ENGINE_PATHS], cwd=PROJECT_DIR,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print("You have uncommitted changes to jobbot's own files; commit or discard them first:\n" + dirty)
        return 1
    _run(["git", "fetch", TEMPLATE_REMOTE, "main"])
    listed = subprocess.run(["git", "ls-tree", "--name-only", "FETCH_HEAD"], cwd=PROJECT_DIR,
                            capture_output=True, text=True).stdout.split("\n")
    paths = [p for p in ENGINE_PATHS if p.split("/")[0] in listed]
    _run(["git", "checkout", "FETCH_HEAD", "--", *paths])
    changes = subprocess.run(["git", "status", "--short", "--", *paths], cwd=PROJECT_DIR,
                             capture_output=True, text=True).stdout.strip()
    if not changes:
        print("Already up to date.")
        return 0
    _run(["git", "commit", "-q", "-m", "Update jobbot from the template", "--", *paths])
    print("Updated:\n" + changes + "\nRestart jobbot to use the new version.")
    return 0
