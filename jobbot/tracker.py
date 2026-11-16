"""tracker.csv I/O, and the sync step that imports the nightly scraper's output."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import threading
from datetime import date
from pathlib import Path

from jobbot.common import (
    DATE_COLUMNS, TRACKER_COLUMNS, company_key, iso_or_original, job_identity, title_key, url_hash,
)
from jobbot.paths import OPEN_JOBS_PATH, POSTINGS_DIR, PROJECT_DIR, TRACKER_PATH

# When two rows describe the same job, the one further along wins.
STATUS_RANK = {
    "": 0, "to_review": 0, "closed": 1, "skipped": 1, "to_apply": 2, "needs_manual": 3,
    "applied": 4, "ghosted": 5, "rejected": 5, "interviewing": 6, "offer": 7,
}
OPEN_STATUSES = {"to_review", "to_apply"}
_ATS_ID_PREFIXES = ("gh:", "lever:", "ashby:", "wd:")

# Held around every load -> modify -> save of tracker.csv inside one process
# (dashboard requests and the background link check).
LOCK = threading.Lock()


def row_key(row: dict) -> str:
    """Rows with the same key are the same job.

    ATS job ids are unique on their own. Any other URL might be a generic careers
    page, so the title is part of the key there.
    """
    ident = job_identity(row.get("jd_url", ""))
    if ident.startswith(_ATS_ID_PREFIXES):
        return ident
    return f"{ident}|{company_key(row.get('company', ''))}|{title_key(row.get('role_title', ''))}"


def job_id(row: dict) -> str:
    return hashlib.sha1(row_key(row).encode("utf-8")).hexdigest()[:10]


def join_notes(*parts: str) -> str:
    seen: list[str] = []
    for part in parts:
        for piece in (part or "").split(";"):
            piece = piece.strip()
            if piece and piece not in seen:
                seen.append(piece)
    return "; ".join(seen)


def load_rows(path: Path = TRACKER_PATH) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [{c: (r.get(c) or "") for c in TRACKER_COLUMNS} for r in csv.DictReader(f)]


def save_rows(rows: list[dict], path: Path = TRACKER_PATH) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKER_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    try:
        os.replace(tmp, path)
    except PermissionError as e:  # Windows: the file is open in Excel
        raise RuntimeError(f"{path.name} is open in another program (Excel?). Close it and try again.") from e


def normalize_dates(rows: list[dict]) -> int:
    """Rewrite M/D/YYYY dates (left by Excel) as ISO. Returns rows changed."""
    changed = 0
    for row in rows:
        before = [row[c] for c in DATE_COLUMNS]
        for c in DATE_COLUMNS:
            row[c] = iso_or_original(row[c])
        changed += before != [row[c] for c in DATE_COLUMNS]
    return changed


def dedupe(rows: list[dict]) -> tuple[list[dict], int]:
    """Collapse rows for the same job, keeping the most advanced status and all notes.

    On a status tie the later row wins (companies rename postings; the later
    title is current), but the earliest date_added is kept.
    """
    index: dict[str, int] = {}
    out: list[dict] = []
    for row in rows:
        key = row_key(row)
        if key not in index:
            index[key] = len(out)
            out.append(row)
            continue
        kept = out[index[key]]
        later_wins = STATUS_RANK.get(row["status"], 0) >= STATUS_RANK.get(kept["status"], 0)
        winner, loser = (row, kept) if later_wins else (kept, row)
        merged = dict(winner)
        merged["notes"] = join_notes(winner["notes"], loser["notes"])
        dates = sorted(d for d in (winner["date_added"], loser["date_added"]) if d)
        merged["date_added"] = dates[0] if dates else ""
        out[index[key]] = merged
    return out, len(rows) - len(out)


def import_postings(rows: list[dict]) -> int:
    """Append rows from scraping/postings/*.csv that the tracker doesn't have yet."""
    if not POSTINGS_DIR.exists():
        return 0
    keys = {row_key(r) for r in rows}
    added = 0
    for path in sorted(POSTINGS_DIR.glob("*.csv")):
        for row in load_rows(path):
            key = row_key(row)
            if key not in keys:
                keys.add(key)
                rows.append(row)
                added += 1
    return added


def mark_closed(rows: list[dict]) -> int:
    """Mark open rows whose posting is no longer listed, using the nightly snapshot.

    Company-board rows are checked against that company's list (only when the full
    list was fetched). Simplify rows are checked against Simplify's active list.
    Rows added by hand are never touched.
    """
    if not OPEN_JOBS_PATH.exists():
        return 0
    state = json.loads(OPEN_JOBS_PATH.read_text(encoding="utf-8"))
    companies = {name: set(hashes) for name, hashes in (state.get("companies") or {}).items()}
    simplify = state.get("simplify_active")
    simplify = set(simplify) if simplify is not None else None
    stamp = state.get("generated_at", "")
    closed = 0
    for row in rows:
        if row["status"] not in OPEN_STATUSES or "src:manual" in row["notes"]:
            continue
        h = url_hash(row["jd_url"])
        if not h:
            continue
        if "src:simplify" in row["notes"]:
            gone = simplify is not None and h not in simplify
        else:
            gone = row["company"] in companies and h not in companies[row["company"]]
        if gone:
            row["status"] = "closed"
            row["notes"] = join_notes(row["notes"], f"closed: no longer listed {stamp}")
            closed += 1
    return closed


def git_pull() -> str:
    """Fetch the scraper's nightly commits. Never fatal: sync works on local files."""
    cmd = ["git", "-C", str(PROJECT_DIR), "pull", "--no-rebase", "--no-edit", "--autostash", "--quiet"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"git pull skipped ({e})"
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        return "git pull failed: " + (lines[-1] if lines else "unknown error")
    return "git pull ok"


def sync(pull: bool = True) -> dict:
    report: dict = {"git": git_pull() if pull else "git pull skipped (--no-pull)"}
    rows = load_rows()
    report["rows_before"] = len(rows)
    report["imported"] = import_postings(rows)
    report["dates_fixed"] = normalize_dates(rows)
    rows, report["duplicates_merged"] = dedupe(rows)
    report["closed"] = mark_closed(rows)
    report["rows_after"] = len(rows)
    save_rows(rows)
    report["synced_on"] = date.today().isoformat()
    return report
