"""Fit check: is the candidate (profile/) a credible applicant for this role?

The title rules (scraping/scraper.py) can't see that a plain "Software Engineer,
Payments" wants six years of distributed systems, or that a research role needs a
PhD. This reads each job description:

1. Rules, free: asks for more than MAX_YEARS_REQUIRED years with no new-grad route,
   or rules out sponsorship -> poor.
2. Claude for the rest (`claude -p` on your own Claude login; model and effort from
   profile/bot.toml): the posting against profile/facts.md -> good / stretch / poor
   with a short reason. Six jobs per call, to go easy on usage.

The verdict goes in the row's notes as "fit: <verdict> — <reason>"; the dashboard
hides "poor" by default. Screened rows are skipped unless recheck=True (do that after
facts.md changes). Job descriptions are cached in .jobbot/jd/.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from jobbot import ats, eligibility, linkcheck, rank, tracker
from jobbot import profile as person
from jobbot.common import fit_tag, job_identity, with_fit_tag
from jobbot.paths import PROJECT_DIR
from jobbot.settings import salary_bounds

JD_DIR = PROJECT_DIR / ".jobbot" / "jd"
SCREEN_STATUSES = {"to_review", "to_apply"}
BATCH = 6
CLAUDE_WORKERS = 2
FETCH_WORKERS = 8
GAP_SECONDS = 2.0
TIMEOUT_SECONDS = 600
HEAD_CHARS, REQ_CHARS = 1500, 4500

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript|svg)\b.*?</\1>", re.I | re.S)
_LD_JSON_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# "5+ years of experience", "3-5 years of professional experience", "4 yrs experience".
YEARS_RE = re.compile(
    r"(?<![\d.$])(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b"
    r"(?=[^.;\n]{0,80}?\bexperience)", re.I)
_PREFERRED_RE = re.compile(r"prefer|nice[ -]to[ -]have|bonus|ideally|a plus", re.I)
_REQUIRED_RE = re.compile(r"required|requirement|minimum|basic qualification|must[ -]have|what we need|"
                          r"you have|you bring|(?<!preferred )qualifications", re.I)
REQ_HEADING_RE = re.compile(
    r"\b(?:qualifications|requirements|what you(?:'ll| will)? (?:bring|need)|you (?:have|bring|might be)|"
    r"what we(?:'re| are)? looking for|what we need to see|about you|who you are|you should have|"
    r"must have)\b", re.I)

SCHEMA = {
    "type": "object",
    "properties": {"jobs": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "verdict": {"type": "string", "enum": ["good", "stretch", "poor"]},
            "reason": {"type": "string"},
        },
        "required": ["id", "verdict", "reason"],
    }}},
    "required": ["jobs"],
}

# The person-specific part (what they're after, what counts as good / stretch / poor) is
# profile/search.toml [fit] guide; this is the frame around it.
SYSTEM_TEMPLATE = """You screen job postings for one candidate, {name}, so their list only holds
roles where they are a credible applicant: someone a recruiter would plausibly move to a
phone screen.

<candidate_facts>
{facts}
</candidate_facts>

{guide}

Judge only from the posting and the facts. Don't give credit for skills the facts don't
show. When a posting is vague about level, go by the title, the scope it describes and its
wording ("you've shipped…", "you've led…"). If the description is unavailable, judge from
the title and company, start the reason with "no description:", and use "stretch" unless the
title alone rules the candidate out (senior / founding / lead scope, a specialist domain, a
non-US place): don't hide a role on no information.

reason: at most 12 words, specific to the job, unless the notes above allow more."""


@dataclass
class Progress:
    running: bool = False
    stage: str = ""          # reading | screening
    total: int = 0
    done: int = 0
    counts: dict = field(default_factory=dict)
    started: str = ""
    finished: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Job descriptions
# ---------------------------------------------------------------------------

def plain(markup: str) -> str:
    """HTML (Greenhouse double-escapes its content) -> text, one line per block."""
    text = html.unescape(markup or "")
    if "&lt;" in text or "&amp;" in text:
        text = html.unescape(text)
    text = _SCRIPT_RE.sub(" ", text)
    text = re.sub(r"<\s*(?:br|/p|/li|/h\d|/div|/tr|li)\b[^>]*>", "\n", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


class _Pacer:
    """At most one request every GAP_SECONDS per host."""

    def __init__(self) -> None:
        self.next_ok: dict[str, float] = {}
        self.lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_ok.get(host, 0.0))
            self.next_ok[host] = at + GAP_SECONDS
        if at > now:
            time.sleep(at - now)


_PACER = _Pacer()


def _get(url: str, accept: str = "application/json", max_bytes: int | None = None) -> str:
    _PACER.wait(url)
    status, _, body = linkcheck._fetch(url, accept=accept, max_bytes=max_bytes)
    return body if status == 200 else ""


def _board(kind: str, slug: str) -> dict[str, str]:
    """Every job description on one company board: {job key: text}."""
    if kind == "greenhouse":
        body = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
        jobs = json.loads(body).get("jobs", []) if body else []
        return {str(j["id"]): plain(j.get("content", "")) for j in jobs}
    if kind == "ashby":
        body = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        jobs = json.loads(body).get("jobs", []) if body else []
        return {j["id"].lower(): j.get("descriptionPlain") or plain(j.get("descriptionHtml", ""))
                for j in jobs if j.get("id")}
    body = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = {}
    for j in (json.loads(body) if body else []):
        parts = [j.get("descriptionPlain", "")]
        parts += [f"{lst.get('text', '')}:\n{plain(lst.get('content', ''))}" for lst in j.get("lists", [])]
        parts.append(j.get("additionalPlain", ""))
        out[j.get("id", "").lower()] = "\n".join(p for p in parts if p)
    return out


_JD_WORDS_RE = re.compile(r"experience|qualification|responsib|requirement|you will|you'll|skills|degree", re.I)


def _usable(text: str) -> str:
    """The text if it reads like a job description; "" for page shells, titles and menus."""
    text = (text or "").strip()
    return text if len(text) >= 300 and _JD_WORDS_RE.search(text) else ""


def _greenhouse_job(job_id: str) -> str:
    """A Greenhouse job shown on a company's own site (?gh_jid=): the embed URL redirects to
    one naming the board, then the job API gives the description."""
    _PACER.wait("https://boards.greenhouse.io/")
    _, final, _ = linkcheck._fetch(f"https://boards.greenhouse.io/embed/job_app?token={job_id}",
                                   accept="text/html", max_bytes=2000)
    slug = (parse_qs(urlsplit(final).query).get("for") or [""])[0]
    if not slug:
        return ""
    body = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}")
    return plain(json.loads(body).get("content", "")) if body else ""


def _workable_job(url: str) -> str:
    m = re.search(r"apply\.workable\.com/([^/]+)/j/([A-Z0-9]+)", url)
    if not m:
        return ""
    body = _get(f"https://apply.workable.com/api/v2/accounts/{m.group(1)}/jobs/{m.group(2)}")
    if not body:
        return ""
    d = json.loads(body)
    return plain("\n".join(d.get(k, "") or "" for k in ("description", "requirements", "benefits")))


def _microsoft_job(url: str) -> str:
    m = re.search(r"/job/(\d+)", url)
    if not m:
        return ""
    body = _get(f"https://apply.careers.microsoft.com/api/apply/v2/jobs/{m.group(1)}?domain=microsoft.com")
    return plain(json.loads(body).get("job_description", "")) if body else ""


def _ld_json_description(page: str) -> str:
    """The JobPosting description many career sites embed for search engines."""
    for raw in _LD_JSON_RE.findall(page):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and "JobPosting" in str(item.get("@type", "")):
                parts = [item.get(k, "") for k in ("description", "qualifications", "experienceRequirements",
                                                    "educationRequirements")]
                text = plain("\n".join(p for p in parts if isinstance(p, str)))
                if len(text) > 300:
                    return text
    return ""


def _page_text(url: str) -> str:
    page = _get(url, accept="text/html,application/xhtml+xml", max_bytes=2_000_000)
    if not page:
        return ""
    return _ld_json_description(page) or plain(page)[:20000]


def _workday_text(url: str) -> str:
    from jobbot.assist import _workday_text as fetch  # assist pulls in the bot; import only when needed
    _PACER.wait(url)
    return fetch(url)


def _cache_path(jid: str):
    return JD_DIR / (hashlib.sha1(jid.encode()).hexdigest()[:16] + ".txt")


def _locate(row: dict, gh_slugs: dict[str, str], boards: dict) -> tuple[str, Callable[[], str]]:
    """(host to queue on, function returning the job text)."""
    url, company = row["jd_url"], row["company"]
    kind = ats.provider(url)
    ident = job_identity(url)
    segs = [s for s in urlsplit(url).path.split("/") if s]

    def from_board(kind: str, slug: str, key: str) -> Callable[[], str]:
        def get() -> str:
            if (kind, slug) not in boards:
                boards[(kind, slug)] = _board(kind, slug)
            return boards[(kind, slug)].get(key, "")
        return get

    if kind == "greenhouse" and ident.startswith("gh:"):
        slug = linkcheck._greenhouse_slug(url, company, gh_slugs)
        if slug:
            return "greenhouse", from_board("greenhouse", slug, ident[3:])
        return "greenhouse", lambda: _greenhouse_job(ident[3:])
    if kind in ("ashby", "lever") and segs and _UUID_RE.search(url):
        return kind, from_board(kind, segs[0], _UUID_RE.search(url).group(0).lower())
    host = urlsplit(url).netloc.lower() or "unknown"
    if kind == "workday":
        return host, lambda: _workday_text(url)
    if "apply.workable.com" in host:
        return host, lambda: _workable_job(url)
    if host == "apply.careers.microsoft.com":
        return host, lambda: _microsoft_job(url)
    return host, lambda: _page_text(url)


def read_descriptions(todo: list[tuple[str, dict]], progress: Progress | None = None) -> dict[str, str]:
    """{job id: description text}; cached on disk, fetched host by host otherwise."""
    texts: dict[str, str] = {}
    per_host: dict[str, list[tuple[str, Callable[[], str]]]] = defaultdict(list)
    gh_slugs, _ = linkcheck._company_config()
    boards: dict = {}
    for jid, row in todo:
        path = _cache_path(jid)
        if path.exists():
            cached = _usable(path.read_text(encoding="utf-8"))
            if cached:
                texts[jid] = cached
                continue
            path.unlink()  # cached before the junk check: fetch again
        host, get = _locate(row, gh_slugs, boards)
        per_host[host].append((jid, get))
    lock = threading.Lock()

    def work(items: list[tuple[str, Callable[[], str]]]) -> None:
        for jid, get in items:
            try:
                text = _usable(get())
            except Exception:  # unreadable page: Claude judges from the title
                text = ""
            with lock:
                texts[jid] = text
                if progress:
                    progress.done += 1
            if text:
                JD_DIR.mkdir(parents=True, exist_ok=True)
                _cache_path(jid).write_text(text, encoding="utf-8")

    if progress:
        progress.done = len(texts)
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for items in per_host.values():
            pool.submit(work, items)
    return texts


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _preferred(text: str, start: int) -> bool:
    """Is this mention under a "preferred / nice to have" heading or on such a line?"""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if _PREFERRED_RE.search(text[line_start:line_end if line_end != -1 else None]):
        return True
    before = text[max(0, start - 600):start]
    last = lambda rx: max((m.start() for m in rx.finditer(before)), default=-1)
    return last(_PREFERRED_RE) > last(_REQUIRED_RE)


def min_years(text: str) -> int | None:
    """Fewest years of experience the posting requires (ignoring "preferred"), or None."""
    found = [int(m.group(1)) for m in YEARS_RE.finditer(text or "")
             if int(m.group(1)) <= 20 and not _preferred(text, m.start())]
    return min(found) if found else None


def hedged(text: str) -> bool:
    """Says it "may not" sponsor (Roblox: "may not be able to … support future H-1B sponsorship").

    Not a refusal, so the role stays visible, but never better than "stretch"; the bot
    still sends these applications to Needs you. Only matters if you need sponsorship.
    """
    from jobbot.apply.engine import NO_SPONSOR_JD_RE
    if not person.needs_sponsorship():
        return False
    m = NO_SPONSOR_JD_RE.search(text or "")
    return bool(m and re.search(r"\bmay not\b", m.group(0), re.I))


def with_hedge(verdict: str, reason: str) -> tuple[str, str]:
    return ("stretch" if verdict == "good" else verdict), f"may not sponsor H-1B · {reason}"


def rule_verdict(row: dict, text: str) -> tuple[str, str] | None:
    """("poor", reason) when the description alone rules the job out; None otherwise."""
    from jobbot.apply.engine import NO_SPONSOR_JD_RE
    rules = eligibility._rules()
    m = rules.NO_SPONSOR_RE.search(text)
    if m and re.search(r"clearance|citizen", m.group(0), re.I):
        if not rules.US_CITIZEN:
            return "poor", "needs US citizenship or a security clearance"
    elif rules.NEEDS_SPONSORSHIP and (m or (NO_SPONSOR_JD_RE.search(text) and not hedged(text))):
        return "poor", "posting rules out visa sponsorship"
    years = min_years(text)
    head = f"{row['role_title']} {text[:3000]}"
    if years is not None and years > rules.MAX_YEARS_REQUIRED and not rules.NEW_GRAD_HINTS.search(head):
        return "poor", f"asks for {years}+ years of experience"
    return None


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def trim(text: str) -> str:
    """The opening plus the requirements section: what a recruiter screens on."""
    if len(text) <= HEAD_CHARS + REQ_CHARS:
        return text
    m = REQ_HEADING_RE.search(text, HEAD_CHARS // 2)
    if not m or m.start() <= HEAD_CHARS:
        return text[:HEAD_CHARS + REQ_CHARS]
    return text[:HEAD_CHARS] + "\n[…]\n" + text[m.start():m.start() + REQ_CHARS]


class ScreenError(RuntimeError):
    pass


def _system_prompt() -> str:
    years = eligibility._rules().MAX_YEARS_REQUIRED
    guide = person.search().get("fit", {}).get("guide", "").strip()
    guide = guide.replace("{years_plus}", str(years + 1)).replace("{years}", str(years))
    return SYSTEM_TEMPLATE.format(name=person.full_name(), facts=person.path("facts.md").read_text(encoding="utf-8"),
                                  guide=guide)


def screen(batch: list[tuple[str, dict, str]], cfg: dict) -> dict[str, tuple[str, str]]:
    """Claude's verdicts for up to BATCH jobs: {job id: (verdict, reason)}."""
    from jobbot.apply.draft import find_claude
    claude = find_claude()
    if not claude:
        raise ScreenError("Claude Code CLI not found; set JOBBOT_CLAUDE to its path")
    ids = {f"j{i + 1}": jid for i, (jid, _, _) in enumerate(batch)}
    jobs = [{"id": f"j{i + 1}", "company": row["company"], "title": row["role_title"],
             "location": row["location"], "description": trim(text) or "(unavailable)"}
            for i, (_, row, text) in enumerate(batch)]
    prompt = ("Screen each posting below. Return one entry per id.\n\n"
              + json.dumps(jobs, indent=1, ensure_ascii=False))
    cmd = [claude, "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
           "--model", str(cfg.get("fit_model", "sonnet")), "--effort", str(cfg.get("fit_effort", "medium")),
           "--json-schema", json.dumps(SCHEMA), "--system-prompt", _system_prompt()]
    with tempfile.TemporaryDirectory(prefix="jobbot-fit-") as cwd:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=TIMEOUT_SECONDS, cwd=cwd)
        except subprocess.TimeoutExpired as e:
            raise ScreenError("Claude took longer than 10 minutes") from e
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise ScreenError(f"unexpected CLI output: {(proc.stderr or proc.stdout)[:200]}") from e
    out = data.get("structured_output")
    if data.get("is_error") or not isinstance(out, dict):
        raise ScreenError(f"no structured answer ({data.get('subtype')}: {str(data.get('result'))[:150]})")
    verdicts = {}
    for item in out.get("jobs", []):
        jid = ids.get(item.get("id", ""))
        if jid and item.get("verdict") in ("good", "stretch", "poor"):
            reason = item.get("reason", "").strip()
            if item["verdict"] == "poor":  # "· lead with …" is a tailoring hint; pointless here
                reason = re.sub(r"\s*·\s*lead with\b.*$", "", reason, flags=re.I)
            verdicts[jid] = (item["verdict"], reason[:140])
    return verdicts


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def due_rows(rows: list[dict], recheck: bool = False, days: int | None = 30) -> list[tuple[str, dict]]:
    """Open rows that pass the title rules and haven't been screened, best-ranked first."""
    floor, target = salary_bounds()
    today = date.today()
    due = []
    for row in rows:
        if row["status"] not in SCREEN_STATUSES or not row["jd_url"].startswith("http"):
            continue
        if not recheck and fit_tag(row["notes"])[0]:
            continue
        v = rank.view(row, today, floor, target)
        if not v["eligible"] or v["below_floor"] or (days and v["age_days"] > days):
            continue
        due.append((v["score"], tracker.job_id(row), row))
    due.sort(key=lambda t: -t[0])
    return [(jid, row) for _, jid, row in due]


def save(verdicts: dict[str, tuple[str, str]]) -> None:
    if not verdicts:
        return
    with tracker.LOCK:
        rows = tracker.load_rows()
        for row in rows:
            v = verdicts.get(tracker.job_id(row))
            if v:
                row["notes"] = with_fit_tag(row["notes"], *v)
        tracker.save_rows(rows)


def run(*, recheck: bool = False, limit: int | None = None, days: int | None = 30, use_claude: bool = True,
        apply: bool = True, progress: Progress | None = None, log: Callable[[str], None] = print) -> dict:
    """Screen due rows: rules on every description, then Claude on what's left."""
    from jobbot.apply.engine import load_bot_config
    cfg = load_bot_config()
    progress = progress or Progress()
    todo = due_rows(tracker.load_rows(), recheck, days)
    if limit:
        todo = todo[:limit]
    progress.running, progress.stage, progress.total, progress.done = True, "reading", len(todo), 0
    progress.counts, progress.error = {}, ""
    progress.started, progress.finished = time.strftime("%H:%M:%S"), ""
    try:
        log(f"Reading {len(todo)} job descriptions…")
        texts = read_descriptions(todo, progress)
        missing = sum(1 for jid, _ in todo if not texts.get(jid))

        results: dict[str, tuple[str, str]] = {}
        rest = []
        for jid, row in todo:
            v = rule_verdict(row, texts.get(jid, ""))
            if v:
                results[jid] = v
            else:
                rest.append((jid, row, texts.get(jid, "")))
        if apply:
            save(results)
        log(f"Rules: {len(results)} ruled out from the description; {len(rest)} left for Claude"
            + (f" ({missing} without a readable description)" if missing else "") + ".")

        progress.stage, progress.done = "screening", len(results)
        progress.counts = dict(Counter(v for v, _ in results.values()))
        failures: list[str] = []
        if use_claude and rest:
            batches = [rest[i:i + BATCH] for i in range(0, len(rest), BATCH)]
            hedges = {jid for jid, _, text in rest if hedged(text)}
            lock = threading.Lock()
            stop = threading.Event()

            def work(batch: list[tuple[str, dict, str]]) -> None:
                if stop.is_set():
                    return
                try:
                    got = screen(batch, cfg)
                except ScreenError as e:
                    with lock:
                        failures.append(str(e))
                        if len(failures) >= 2:  # usage limit or CLI trouble: don't keep hammering
                            stop.set()
                    log(f"  Claude batch failed: {e}")
                    return
                got = {jid: (with_hedge(*v) if jid in hedges else v) for jid, v in got.items()}
                if apply:
                    save(got)
                with lock:
                    results.update(got)
                    progress.done += len(batch)
                    for v, _ in got.values():
                        progress.counts[v] = progress.counts.get(v, 0) + 1
                log(f"  {progress.done}/{progress.total} screened")

            with ThreadPoolExecutor(max_workers=CLAUDE_WORKERS) as pool:
                list(pool.map(work, batches))
        progress.error = failures[0] if failures else ""
    except Exception as e:  # keep the dashboard's status line honest
        progress.error = f"{type(e).__name__}: {e}"
        raise
    finally:
        progress.running, progress.finished = False, time.strftime("%H:%M:%S")

    counts = Counter(v for v, _ in results.values())
    summary = {"screened": len(results), "good": counts["good"], "stretch": counts["stretch"],
               "poor": counts["poor"], "unscreened": len(todo) - len(results), "error": progress.error}
    progress.counts = dict(counts)
    return {**summary, "results": results}
