"""Check tracked postings: is the job still open, and when was it posted?

Greenhouse, Lever and Ashby: one request per company board, then a lookup per job.
Workday: one detail request per job (exact start date, and whether it still takes
applications). Everything else: load the page and look for a 404, a redirect to
the careers home page, or "no longer accepting applications"-style text.

One request every 2-3 seconds per host; different hosts run in parallel.
Results are cached in .jobbot/link_checks.json so rows are re-checked weekly,
not on every run. Dead postings are set to status `closed`, never deleted: a
deleted row would be re-imported from scraping/postings/ on the next sync.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from jobbot import ats, tracker
from jobbot.common import day_from_epoch, day_from_iso, job_identity, parse_date
from jobbot.paths import PROJECT_DIR

CACHE_PATH = PROJECT_DIR / ".jobbot" / "link_checks.json"
def _companies_path() -> Path:
    from jobbot import profile as person
    return person.path("companies.json")
CHECK_STATUSES = {"to_review", "to_apply"}
GAP_SECONDS = (2.0, 3.0)
USER_AGENT = "jobbot/1.0 (personal job search)"
MAX_WORKERS = 8

# Text that means the posting is gone. Matched against the page with <script>
# and <style> removed, so app bundles that merely contain these strings don't count.
CLOSED_PHRASES = (
    "no longer accepting applications", "this job is no longer available",
    "job is no longer available", "position is no longer available",
    "this position has been filled", "position has been filled",
    "this job has expired", "job posting has expired", "this posting has closed",
    "the job you are looking for is no longer", "job you're looking for is no longer",
    "this requisition is no longer", "no longer open for applications",
    "this job is closed", "job has been closed",
)
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_LOCALE_RE = re.compile(r"[a-z]{2}-[A-Z]{2}")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


@dataclass
class Result:
    state: str              # open | closed | unknown
    posted: str = ""        # YYYY-MM-DD when the board reports it
    reason: str = ""


@dataclass
class Task:
    jids: list[str]
    run: Callable[[], dict[str, Result]]


@dataclass
class Progress:
    running: bool = False
    total: int = 0
    done: int = 0
    closed: int = 0
    started: str = ""
    finished: str = ""
    summary: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _fetch(url: str, accept: str = "application/json", max_bytes: int | None = None,
           body: dict | None = None) -> tuple[int, str, str]:
    """(status, final URL, text). JSON APIs are read whole: some boards are several MB."""
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(max_bytes) if max_bytes else resp.read()
            return resp.status, resp.geturl(), raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""


# ---------------------------------------------------------------------------
# Per-source checks
# ---------------------------------------------------------------------------

def _board_task(kind: str, slug: str, wanted: list[tuple[str, str]]) -> Task:
    """One board request answers every tracked job on that company's board."""
    def run() -> dict[str, Result]:
        if kind == "greenhouse":
            status, _, body = _fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            parse = lambda d: {str(j["id"]): day_from_iso(j.get("first_published") or j.get("updated_at") or "")
                               for j in d.get("jobs", [])}
        elif kind == "ashby":
            status, _, body = _fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            parse = lambda d: {j["id"].lower(): day_from_iso(j.get("publishedAt") or "")
                               for j in d.get("jobs", []) if j.get("id")}
        else:  # lever
            status, _, body = _fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json")
            parse = lambda d: {j["id"].lower(): day_from_epoch(j.get("createdAt"))
                               for j in d if j.get("id")}
        if status == 404:
            return {jid: Result("closed", reason=f"{kind} board '{slug}' no longer exists") for jid, _ in wanted}
        if status != 200:
            return {jid: Result("unknown", reason=f"{kind} board HTTP {status}") for jid, _ in wanted}
        listed = parse(json.loads(body))
        return {jid: (Result("open", posted=listed[key]) if key in listed
                      else Result("closed", reason=f"not on {slug}'s {kind} board"))
                for jid, key in wanted}
    return Task([jid for jid, _ in wanted], run)


def _workday(url: str, site_fallback: str) -> Result:
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    if "job" not in segs:
        return _generic(url)
    i = segs.index("job")
    before = [s for s in segs[:i] if not _LOCALE_RE.fullmatch(s)]
    site = before[-1] if before else site_fallback
    if not site:
        return _generic(url)
    tenant = parts.netloc.split(".")[0]
    api = f"https://{parts.netloc}/wday/cxs/{tenant}/{site}"
    status, _, body = _fetch(f"{api}/" + "/".join(segs[i:]))
    if status in (404, 410):
        return Result("closed", reason="Workday: job not found")
    if status == 403:
        # Workday answers expired postings with 403 "permission denied". Confirm by
        # searching the board for the requisition id, so a real block never closes jobs.
        req_id = segs[-1].rsplit("_", 1)[-1].split("-")[0] if "_" in segs[-1] else ""
        if not req_id:
            return Result("unknown", reason="Workday HTTP 403")
        time.sleep(random.uniform(*GAP_SECONDS))
        s2, _, b2 = _fetch(f"{api}/jobs", body={"limit": 1, "offset": 0, "searchText": req_id,
                                               "appliedFacets": {}})
        if s2 == 200 and json.loads(b2).get("total", 1) == 0:
            return Result("closed", reason="Workday: no longer listed")
        return Result("unknown", reason="Workday HTTP 403")
    if status != 200:
        return Result("unknown", reason=f"Workday HTTP {status}")
    info = json.loads(body).get("jobPostingInfo") or {}
    posted = day_from_iso(info.get("startDate") or "")
    if not info:
        return Result("closed", reason="Workday: no posting info")
    if info.get("canApply") is False:
        return Result("closed", posted=posted, reason="Workday: not accepting applications")
    return Result("open", posted=posted)


def _redirected_away(original: str, final: str) -> bool:
    """A job link that lands on a shorter page without the job's id was redirected to a listing."""
    o, f = urlsplit(original), urlsplit(final)
    o_segs = [s for s in o.path.split("/") if s]
    f_segs = [s for s in f.path.split("/") if s]
    if not o_segs or o.path.rstrip("/") == f.path.rstrip("/"):
        return False
    return len(f_segs) < len(o_segs) and o_segs[-1].lower() not in final.lower()


def _generic(url: str) -> Result:
    status, final, body = _fetch(url, accept="text/html,application/xhtml+xml", max_bytes=800_000)
    if status in (404, 410):
        return Result("closed", reason=f"page returns HTTP {status}")
    if urlsplit(final).path.rstrip("/").endswith(("/oops", "/404", "/not-found", "/job-not-found")):
        return Result("closed", reason="redirects to an error page")
    if status != 200:
        return Result("unknown", reason=f"HTTP {status}")
    text = _SCRIPT_RE.sub(" ", body).lower()
    for phrase in CLOSED_PHRASES:
        if phrase in text:
            return Result("closed", reason=f'page says "{phrase}"')
    if _redirected_away(url, final):
        return Result("closed", reason="redirects to a listing page")
    return Result("open", reason="page loads")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _company_config() -> tuple[dict[str, str], dict[str, str]]:
    """(company -> greenhouse slug, company -> workday site) from companies.json."""
    gh, wd = {}, {}
    try:
        cfg = json.loads(_companies_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return gh, wd
    for key, entries in cfg.items():
        if key.startswith("_") or not isinstance(entries, list):
            continue
        for e in entries:
            if e.get("ats_provider") == "greenhouse" and e.get("ats_slug"):
                gh[e["company"]] = e["ats_slug"]
            if e.get("ats_provider") == "workday" and e.get("career_url"):
                wd[e["company"]] = urlsplit(e["career_url"]).path.strip("/").split("/")[0]
    return gh, wd


def _greenhouse_slug(url: str, company: str, gh_slugs: dict[str, str]) -> str:
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    if "greenhouse.io" in parts.netloc:
        q = parse_qs(parts.query)
        if "for" in q:
            return q["for"][0]
        if "jobs" in segs and segs.index("jobs") > 0:
            return segs[segs.index("jobs") - 1]
    return gh_slugs.get(company, "")


def _plan(todo: list[tuple[str, dict]]) -> dict[str, list[Task]]:
    """Group checks into per-host task lists."""
    gh_slugs, wd_sites = _company_config()
    boards: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    per_host: dict[str, list[Task]] = defaultdict(list)
    board_hosts = {"greenhouse": "boards-api.greenhouse.io", "ashby": "api.ashbyhq.com",
                   "lever": "api.lever.co"}

    for jid, row in todo:
        url, company = row["jd_url"], row["company"]
        kind = ats.provider(url)
        ident = job_identity(url)
        segs = [s for s in urlsplit(url).path.split("/") if s]
        if kind == "greenhouse" and ident.startswith("gh:"):
            slug = _greenhouse_slug(url, company, gh_slugs)
            if slug:
                boards[("greenhouse", slug)].append((jid, ident[3:]))
                continue
        elif kind in ("ashby", "lever") and segs and _UUID_RE.search(url):
            boards[(kind, segs[0])].append((jid, _UUID_RE.search(url).group(0).lower()))
            continue
        host = urlsplit(url).netloc.lower() or "unknown"
        if kind == "workday":
            fn = lambda url=url, site=wd_sites.get(company, ""): _workday(url, site)
        else:
            fn = lambda url=url: _generic(url)
        per_host[host].append(Task([jid], lambda jid=jid, fn=fn: {jid: fn()}))

    for (kind, slug), wanted in boards.items():
        per_host[board_hosts[kind]].append(_board_task(kind, slug, wanted))
    return per_host


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8")).get("checks", {})
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "checks": cache}, indent=0), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def due_rows(rows: list[dict], cache: dict, today: date, max_age_days: int,
             check_all: bool) -> list[tuple[str, dict]]:
    """Open rows not checked within max_age_days, never-checked first."""
    due = []
    for row in rows:
        if row["status"] not in CHECK_STATUSES or not row["jd_url"].startswith("http"):
            continue
        jid = tracker.job_id(row)
        last = parse_date((cache.get(jid) or {}).get("date", ""))
        if check_all or last is None or (today - last).days >= max_age_days:
            due.append((last.toordinal() if last else 0, jid, row))
    due.sort(key=lambda t: t[0])
    return [(jid, row) for _, jid, row in due]


def run(*, check_all: bool = False, max_age_days: int = 7, limit: int | None = None,
        apply: bool = True, progress: Progress | None = None,
        log: Callable[[str], None] = print) -> dict:
    """Check due rows, then mark dead ones closed and fill in posted dates."""
    progress = progress or Progress()
    today = date.today()
    cache = _load_cache()
    todo = due_rows(tracker.load_rows(), cache, today, max_age_days, check_all)
    if limit:
        todo = todo[:limit]
    per_host = _plan(todo)

    progress.running, progress.total, progress.done, progress.closed = True, len(todo), 0, 0
    progress.started, progress.finished = time.strftime("%H:%M:%S"), ""
    results: dict[str, Result] = {}
    lock = threading.Lock()
    log(f"Checking {len(todo)} links across {len(per_host)} sites…")

    def work(host: str, tasks: list[Task]) -> None:
        for i, task in enumerate(tasks):
            if i:
                time.sleep(random.uniform(*GAP_SECONDS))
            try:
                out = task.run()
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
                out = {jid: Result("unknown", reason=f"{type(e).__name__}: {str(e)[:60]}") for jid in task.jids}
            with lock:
                results.update(out)
                progress.done += len(out)
                progress.closed += sum(r.state == "closed" for r in out.values())
                if progress.done % 100 < len(out):
                    log(f"  {progress.done}/{progress.total} checked, {progress.closed} closed")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for host, tasks in per_host.items():
            pool.submit(work, host, tasks)

    stamp = today.isoformat()
    for jid, r in results.items():
        cache[jid] = {"date": stamp, **asdict(r)}
    _save_cache(cache)

    closed = posted = 0
    if apply and results:
        with tracker.LOCK:
            rows = tracker.load_rows()
            for row in rows:
                r = results.get(tracker.job_id(row))
                if not r:
                    continue
                if r.posted and row["date_posted"] != r.posted:
                    row["date_posted"] = r.posted
                    posted += 1
                if r.state == "closed" and row["status"] in CHECK_STATUSES:
                    row["status"] = "closed"
                    row["notes"] = tracker.join_notes(row["notes"], f"closed: link check {stamp} ({r.reason})")
                    closed += 1
            tracker.save_rows(rows)

    states = Counter(r.state for r in results.values())
    summary = {
        "checked": len(results), "open": states["open"], "closed": states["closed"],
        "unknown": states["unknown"], "marked_closed": closed, "posted_dates_filled": posted,
        "closed_reasons": Counter(r.reason.split(" '")[0].split(" (")[0] for r in results.values()
                                  if r.state == "closed").most_common(6),
    }
    progress.running, progress.finished, progress.summary = False, time.strftime("%H:%M:%S"), summary
    return summary
