#!/usr/bin/env python3
"""Nightly job scraper for the JobApps tracker.

Reads profile/companies.json, dispatches to ATS-specific fetchers, adds the
SimplifyJobs new-grad list, applies the filter rules from CLAUDE.md, dedupes
against tracker.csv and earlier postings files, and writes new rows (status
to_review) to scraping/postings/YYYY-MM-DD.csv.

It never edits tracker.csv. `python3 -m jobbot sync` imports the postings files
locally, so the nightly commit and local tracker edits can't conflict. It also
writes scraping/state/open_jobs.json (hashes of every posting still listed) so
sync can mark closed roles.

Provider support:
  greenhouse, lever, ashby, smartrecruiters -> public JSON APIs
  workday                                   -> public wday/cxs JSON endpoint
  custom: amazon                            -> public search.json endpoint
  custom: other                             -> logged + skipped (no parser yet)
  simplify (not in companies.json)          -> public listings.json on GitHub

Design notes:
  - stdlib only (urllib + csv + json + re) so the cron job has no install step.
  - 2-3s polite rate limit per domain. UA names a personal contact.
  - Postings with no description (workday/SR/simplify) are filtered on title + location only.
  - Uncertain signals (e.g. ambiguous location) -> flag in notes, do not drop.
"""

from __future__ import annotations

import csv
import json
import logging
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
# Which company boards to check: profile/companies.json (yours), else the old location.
COMPANIES_PATH = next((p for p in (PROJECT_DIR / "profile" / "companies.json", SCRIPT_DIR / "companies.json",
                                   PROJECT_DIR / "profile.example" / "companies.json") if p.exists()),
                      PROJECT_DIR / "profile" / "companies.json")
TRACKER_PATH = PROJECT_DIR / "tracker.csv"
POSTINGS_DIR = SCRIPT_DIR / "postings"
OPEN_JOBS_PATH = SCRIPT_DIR / "state" / "open_jobs.json"
LOG_PATH = SCRIPT_DIR / "log.txt"

sys.path.insert(0, str(PROJECT_DIR))
from jobbot.common import (  # noqa: E402
    TRACKER_COLUMNS, company_key, day_from_epoch, day_from_iso, day_from_text,
    day_from_workday, job_identity, parse_date, title_key, url_hash,
)
from jobbot import profile as person  # noqa: E402

# Who's searching: title rules, level, years, sources (profile/search.toml) and whether
# sponsorship / citizenship rules apply to them (profile/answers.toml).
SEARCH = person.search()
_TARGET, _TITLES, _SOURCES = SEARCH.get("target", {}), SEARCH.get("titles", {}), SEARCH.get("sources", {})
_AUTH = person.answers().get("work_authorization", {})
NEEDS_SPONSORSHIP = bool(_AUTH.get("requires_sponsorship", False))
US_CITIZEN = bool(_AUTH.get("us_citizen", False))

SIMPLIFY_LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
    "dev/.github/scripts/listings.json"
)
SIMPLIFY_ENABLED = bool(_SOURCES.get("simplify_new_grad", False))
SIMPLIFY_CATEGORIES = set(_SOURCES.get("simplify_categories", []))
SIMPLIFY_MAX_AGE_DAYS = 7
SIMPLIFY_BLOCKED_SPONSORSHIP = {"Does Not Offer Sponsorship", "U.S. Citizenship is Required"}

# The same role often shows up from two sources with different URLs. Treat a
# matching company + title added within this window as a duplicate.
TITLE_DUP_WINDOW_DAYS = 60

# Sites see a contact address with each request: the email in answers.toml.
USER_AGENT = (
    "JobApps-Scraper/1.0 (personal job search; "
    f"contact {person.answers().get('identity', {}).get('email', 'unknown')})"
)
REQUEST_TIMEOUT = 25
RATE_LIMIT_RANGE = (2.0, 3.0)
NEW_ROWS_WARN_THRESHOLD = 100

# ---------------------------------------------------------------------------
# Filter patterns (CLAUDE.md filter rules)
# ---------------------------------------------------------------------------

# Title rules for this person (profile/search.toml [titles]); None when a list is empty.
ENGINEERING_INDICATORS = person.words_regex(_TITLES.get("include", []))
NON_ENG_BLOCKERS = person.words_regex(_TITLES.get("not_target", []))
OFF_PROFILE_BLOCKERS = person.words_regex(_TITLES.get("outside_background", []))
NOT_TARGET_TRACK = person.words_regex(_TITLES.get("other_tracks", []))
# Company rules ([[company_rules]]): titles must start with one of a list, and/or need a
# new-grad signal (Amazon's software category is too broad without both).
COMPANY_RULES = {r["company"]: {"title_re": person.words_regex(r.get("title_starts_with", [])),
                                "new_grad": bool(r.get("require_new_grad_signal", False))}
                 for r in SEARCH.get("company_rules", []) if r.get("company")}

# Levels above max_level ("Engineer III", "SDE 3", Microsoft "IC3" when max_level is 2).
MAX_LEVEL = int(_TARGET.get("max_level", 2))
_ROMANS = ["i", "ii", "iii", "iv", "v"]
_LEVEL_NOUNS = "|".join(_TARGET.get("level_nouns", ["engineer", "developer"]))
MID_LEVEL_BLOCKER = re.compile(
    rf"\b({_LEVEL_NOUNS})\s+(?:{'|'.join(_ROMANS[MAX_LEVEL:] + [str(n) for n in range(MAX_LEVEL + 1, 6)])})\b"
    + (rf"|\bic[{MAX_LEVEL + 1}-9]\b" if MAX_LEVEL < 9 else ""),
    re.I,
)
MAX_YEARS_REQUIRED = int(_TARGET.get("max_years_required", 3))
DROP_MANAGER = bool(_TARGET.get("drop_manager_titles", True))
DROP_LEAD = bool(_TARGET.get("drop_lead_titles", True))
DROP_PHD = bool(_TARGET.get("drop_phd_roles", True))
DROP_CONTRACT = bool(_TARGET.get("drop_contract_roles", True))
DROP_APPRENTICE = bool(_TARGET.get("drop_apprenticeships", True))

INTERN_BLOCKER = re.compile(r"\b(intern|interns|internships?|co-?op)\b", re.I)

# Student titles are always dropped (people using this have graduated). Contract roles and
# apprenticeships depend on the field: [target] drop_contract_roles / drop_apprenticeships.
STUDENT_BLOCKER = re.compile(r"\bstudent\b", re.I)
CONTRACT_BLOCKER = re.compile(r"\bcontractor\b|\(contract\)|\bcontract\s+(?:student|role|position)\b", re.I)
APPRENTICE_BLOCKER = re.compile(r"\bapprentice\w*\b", re.I)
DEGREE_BLOCKER = re.compile(r"\b(ph\.?\s?d|postdoc\w*|post-?doctoral|postbaccalaureate)\b", re.I)
# A country or region outside the US named in the title ("…, Korea", "- India").
NON_US_TITLE_RE = re.compile(
    r"\b(india|korea|japan|china|taiwan|singapore|uk|united\s+kingdom|london|germany|"
    r"france|israel|canada|toronto|mexico|brazil|poland|ireland|netherlands|australia|"
    r"europe|emea|apac|latam)\b",
    re.I,
)

SENIOR_BLOCKERS = re.compile(
    r"\b(senior|sr\.?|staff|principal|distinguished|director|"
    r"head\s+of|vp|vice\s*president|chief)\b",
    re.I,
)

# Manager-titled roles (drop_manager_titles in search.toml).
MANAGER_BLOCKER = re.compile(r"\bmanager\b", re.I)
LEAD_BLOCKER = re.compile(r"\blead\b", re.I)

NEW_GRAD_HINTS = re.compile(
    r"\b(new\s*grad|university|early\s*career|entry[\s-]*level|"
    r"recent\s*(?:graduate|grad)|graduate\s*program|early\s*in\s*career|"
    r"2026\s*grad|class\s*of\s*20\d\d)\b",
    re.I,
)

# Stronger new-grad signal — used as a positive tag in tracker notes and as a
# requirement for surviving the Amazon strict whitelist.
NEW_GRAD_TITLE_RE = re.compile(
    r"\b(new\s*grad|university\s*(?:hire|graduate|recruit|recruiting)?|"
    r"early\s*career|entry[\s-]*level|recent\s*(?:graduate|grad)|"
    r"class\s+of\s+20\d\d|graduate\s+program|"
    r"2026(?:\s*\(us\))?|fall\s*20(?:25|26)\s*start|"
    r"associate\s+(?:software|engineer|developer|ml|machine\s*learning|"
    r"data|research|applied)|"
    r"(?:engineer|sde|developer)\s+i\b)",
    re.I,
)
NEW_GRAD_DESC_RE = re.compile(
    r"\b(new\s*grad|entry[\s-]*level|recent\s*(?:graduate|grad)|"
    r"early\s*in\s+(?:your\s+)?career|"
    r"university\s+(?:hire|hires|graduate|new\s*grad)|"
    r"0\s*[-–to]+\s*[12]\s*years?|"
    r"graduating\s+in\s+20(?:25|26)|"
    r"completed?\s+(?:your\s+)?(?:bachelor|master|degree)\s+(?:in|by)\s+20(?:25|26))",
    re.I,
)

YEARS_REQ_RE = re.compile(
    r"(?:minimum\s+of\s+|at\s+least\s+)?"
    r"(\d{1,2})\s*(?:\+|to|–|\-)\s*(?:\d{1,2}\s*)?years?",
    re.I,
)

SALARY_RE = re.compile(
    r"\$\s*(\d{2,3}(?:,\d{3})?(?:\.\d+)?\s*[kK]?)"
    r"\s*[-–to]+\s*"
    r"\$?\s*(\d{2,3}(?:,\d{3})?(?:\.\d+)?\s*[kK]?)"
)

NO_SPONSOR_RE = re.compile(
    r"(no\s+visa\s+sponsor|not?\s+(?:provide|offer)\s+(?:visa\s+)?sponsor|"
    r"unable\s+to\s+sponsor|will\s+not\s+sponsor|do(?:es)?\s+not\s+sponsor|"
    r"sponsorship\s+is\s+not\s+(?:available|offered)|"
    r"must\s+be\s+(?:a\s+)?u\.?s\.?\s+citizen|"
    r"u\.?s\.?\s+citizenship\s+(?:is\s+)?required|"
    r"requires?\s+(?:active\s+)?(?:us\s+|u\.s\.\s+)?security\s+clearance|"
    r"top\s*secret\s+clearance)",
    re.I,
)

NON_US_HINTS = [
    "london", "berlin", "munich", "paris", "lyon", "tokyo", "kyoto",
    "tel aviv", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai",
    "toronto", "vancouver", "montreal", "ottawa", "amsterdam", "rotterdam",
    "dublin", "warsaw", "krakow", "singapore", "shanghai", "beijing",
    "shenzhen", "seoul", "sydney", "melbourne", "auckland", "mexico city",
    "sao paulo", "são paulo", "barcelona", "madrid", "lisbon", "milan",
    "zurich", "geneva", "stockholm", "copenhagen", "oslo", "helsinki",
    "united kingdom", "germany", "france", "japan", "india", "canada",
    "ireland", "poland", "netherlands", "spain", "italy", "brazil", "mexico",
    "israel", "australia", "new zealand", "south korea", "china",
    "switzerland", "sweden", "denmark", "norway", "finland",
    " uk", "(uk)", ", uk", " emea", " apac", " latam",
    "ontario", "quebec", "british columbia",
    # Added 2026-11-08 after the fit check found these in the tracker.
    "bucharest", "romania", "costa rica", "armenia", "yerevan", "taiwan", "taipei", "kuwait",
    "portugal", "hong kong", "vietnam", "hanoi", "ho chi minh", "philippines", "manila",
    "indonesia", "jakarta", "malaysia", "kuala lumpur", "thailand", "bangkok", "cairo", "egypt",
    "nigeria", "nairobi", "kenya", "south africa", "cape town", "johannesburg", " uae",
    "dubai", "abu dhabi", "saudi", "riyadh", "qatar", "doha", "istanbul", "prague", "budapest",
    "brussels", "belgium", "austria", "kyiv", "ukraine", "belgrade", "serbia", "bulgaria",
    "zagreb", "croatia", "vilnius", "lithuania", "tallinn", "estonia", "latvia", "argentina",
    "buenos aires", "colombia", "bogota", "bogotá", "medellin", "medellín", "uruguay",
    "montevideo", "edinburgh", "glasgow", "belfast", "luxembourg", "chennai", "delhi",
    "gurgaon", "gurugram", "noida", "kolkata", "ahmedabad", "osaka", "wroclaw", "haifa",
    "herzliya", "calgary", "edmonton", "waterloo, on", "pakistan", "bangladesh", "sri lanka",
]

US_HINTS = [
    "united states", " usa", "u.s.a", "u.s.", " us ",
    "remote, us", "remote - us", "remote (us", "remote us",
    "us-remote", "u.s. remote", "us only", "u.s. only",
    "san francisco", "bay area", "new york", "nyc", "manhattan", "brooklyn",
    "seattle", "boston", "cambridge, ma", "austin", "los angeles",
    "chicago", "mountain view", "menlo park", "redmond", "cupertino",
    "san jose", "palo alto", "bellevue", "sunnyvale", "santa clara",
    "denver", "boulder", "atlanta", "san diego", "irvine", "burbank",
    "raleigh", "durham", "pittsburgh", "philadelphia", "minneapolis",
    "salt lake city", "portland", "phoenix", "dallas", "houston",
    "miami", "tampa", "orlando", "washington, dc", "washington d.c.",
    ", ca ", ", wa ", ", ny ", ", tx ", ", ma ", ", il ", ", co ", ", ga ",
    ", nc ", ", or ", ", fl ", ", pa ", ", va ", ", md ", ", az ", ", mn ",
    ", ut ", ", nv ", ", oh ", ", mi ", ", wi ", ", nj ", ", dc ",
]
# Hints that can't stand alone against a named foreign place: "Toronto, CA" is Canada and
# "San Jose, Costa Rica" isn't California. They only count with another US hint.
WEAK_US_HINTS = {h for h in US_HINTS if h.startswith(", ")} | {"san jose", "durham"}


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class HttpClient:
    """Minimal HTTP wrapper with per-host rate limiting."""

    def __init__(self, sleep_range=RATE_LIMIT_RANGE):
        self._last_hit: dict[str, float] = {}
        self._sleep_range = sleep_range

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        gap_needed = random.uniform(*self._sleep_range)
        elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
        if elapsed < gap_needed:
            time.sleep(gap_needed - elapsed)
        self._last_hit[host] = time.monotonic()

    def get_json(self, url: str) -> dict | list:
        self._wait(url)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post_json(self, url: str, body: dict) -> dict | list:
        self._wait(url)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Posting model
# ---------------------------------------------------------------------------

@dataclass
class Posting:
    company: str
    role_title: str
    jd_url: str
    location: str = ""
    salary_range: str = ""
    description: str = ""
    posted: str = ""          # YYYY-MM-DD when the board reports it


class PostingList(list):
    """A fetcher's results. complete=False when pagination hit our safety cap,
    so the list can't be used to decide which jobs have closed."""

    complete = True


# ---------------------------------------------------------------------------
# ATS fetchers
# ---------------------------------------------------------------------------

def fetch_greenhouse(http: HttpClient, company: str, slug: str) -> list[Posting]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = http.get_json(url)
    out: list[Posting] = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "") or ""
        out.append(Posting(
            company=company,
            role_title=(j.get("title") or "").strip(),
            jd_url=j.get("absolute_url", "") or "",
            location=loc,
            description=strip_html(j.get("content", "") or ""),
            posted=day_from_iso(j.get("first_published") or j.get("updated_at") or ""),
        ))
    return out


def fetch_lever(http: HttpClient, company: str, slug: str) -> list[Posting]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = http.get_json(url)
    out: list[Posting] = []
    for j in data:
        cats = j.get("categories") or {}
        desc = j.get("descriptionPlain") or strip_html(j.get("description") or "")
        out.append(Posting(
            company=company,
            role_title=(j.get("text") or "").strip(),
            jd_url=j.get("hostedUrl", "") or "",
            location=cats.get("location", "") or "",
            description=desc,
            posted=day_from_epoch(j.get("createdAt")),
        ))
    return out


def fetch_ashby(http: HttpClient, company: str, slug: str) -> list[Posting]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    data = http.get_json(url)
    out: list[Posting] = []
    for j in data.get("jobs", []):
        comp = ""
        ct = j.get("compensationTier") or j.get("compensation") or {}
        if isinstance(ct, dict):
            comp = ct.get("compensationTierSummary") or ct.get("summary") or ""
        loc = j.get("location") or j.get("locationName") or ""
        desc = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml") or "")
        out.append(Posting(
            company=company,
            role_title=(j.get("title") or "").strip(),
            jd_url=j.get("jobUrl") or j.get("applyUrl") or "",
            location=loc,
            salary_range=comp,
            description=desc,
            posted=day_from_iso(j.get("publishedAt") or ""),
        ))
    return out


def fetch_workable(http: HttpClient, company: str, slug: str) -> list[Posting]:
    url = f"https://www.workable.com/api/accounts/{slug}"
    data = http.get_json(url)
    out: list[Posting] = []
    for j in data.get("jobs", []) or []:
        loc_parts = [j.get("city"), j.get("state"), j.get("country")]
        loc = ", ".join(p for p in loc_parts if p)
        if j.get("telecommuting"):
            loc = (loc + " (Remote)").strip()
        out.append(Posting(
            company=company,
            role_title=(j.get("title") or "").strip(),
            jd_url=j.get("url") or j.get("shortlink") or "",
            location=loc,
            description="",  # would require per-job call
            posted=day_from_iso(j.get("published_on") or j.get("created_at") or ""),
        ))
    return out


def fetch_smartrecruiters(http: HttpClient, company: str, slug: str) -> list[Posting]:
    out = PostingList()
    offset = 0
    page_size = 100
    while True:
        url = (
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
            f"?limit={page_size}&offset={offset}"
        )
        data = http.get_json(url)
        items = data.get("content", []) or []
        for j in items:
            loc = j.get("location") or {}
            loc_str = ", ".join(
                filter(None, [loc.get("city"), loc.get("region"), loc.get("country")])
            )
            jd_url = (
                j.get("applyUrl")
                or f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}"
            )
            out.append(Posting(
                company=company,
                role_title=(j.get("name") or "").strip(),
                jd_url=jd_url,
                location=loc_str,
                description="",  # SR requires a per-posting call; skip to stay light
                posted=day_from_iso(j.get("releasedDate") or ""),
            ))
        if len(items) < page_size:
            break
        offset += page_size
        if offset > 2000:
            out.complete = False
            break
    return out


def fetch_workday(http: HttpClient, company: str, career_url: str, search: str = "") -> list[Posting]:
    """Use the public wday/cxs JSON endpoint on *.myworkdayjobs.com sites.

    Example:
      https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
      -> POST https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs
    """
    parsed = urlparse(career_url)
    host = parsed.netloc
    if "myworkdayjobs.com" not in host:
        raise ValueError(f"workday career_url not on myworkdayjobs.com: {career_url}")
    site = parsed.path.strip("/").split("/")[0]
    if not site:
        raise ValueError(f"workday career_url missing site segment: {career_url}")
    tenant = host.split(".")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out = PostingList()
    offset = 0
    limit = 20
    today = date.today()
    while True:
        body = {"limit": limit, "offset": offset, "searchText": search, "appliedFacets": {}}
        data = http.post_json(api, body)
        postings = data.get("jobPostings") or []
        for j in postings:
            ext_path = j.get("externalPath") or ""
            jd_url = f"https://{host}{ext_path}" if ext_path else career_url
            bullets = j.get("bulletFields") or []
            location = j.get("locationsText") or (bullets[0] if bullets else "")
            out.append(Posting(
                company=company,
                role_title=(j.get("title") or "").strip(),
                jd_url=jd_url,
                location=location,
                description="",  # per-job description would need another N requests
                posted=day_from_workday(j.get("postedOn") or "", today),
            ))
        if len(postings) < limit:
            break
        offset += limit
        if offset > 1000:
            out.complete = False
            break
    return out


def backfill_workday_description(http: HttpClient, career_url: str, posting: Posting) -> None:
    """Fetch the full description for one Workday posting and update it in place.

    Workday's list endpoint returns titles + locations but no description. To run
    the years/sponsorship filters reliably we need the detail call. We do this
    only after the title+location filter has already passed, to avoid spending
    a request per junk posting.
    """
    parsed = urlparse(career_url)
    host = parsed.netloc
    site = parsed.path.strip("/").split("/")[0]
    tenant = host.split(".")[0]
    # The jd_url was built as f"https://{host}{externalPath}", so the path is
    # everything after the host. externalPath itself starts with "/job/...".
    if not posting.jd_url.startswith(f"https://{host}"):
        return
    ext_path = posting.jd_url[len(f"https://{host}"):]
    if not ext_path.startswith("/job/"):
        return
    detail_url = f"https://{host}/wday/cxs/{tenant}/{site}{ext_path}"
    data = http.get_json(detail_url)
    info = data.get("jobPostingInfo") or {}
    desc_html = info.get("jobDescription") or ""
    posting.description = strip_html(desc_html)
    posting.posted = day_from_iso(info.get("startDate") or "") or posting.posted


def fetch_amazon(http: HttpClient, company: str, career_url: str) -> list[Posting]:
    """Amazon Jobs public search API. US software-development category only."""
    out = PostingList()
    offset = 0
    page_size = 100
    while True:
        url = (
            "https://www.amazon.jobs/en/search.json"
            f"?category%5B%5D=software-development"
            f"&result_limit={page_size}&offset={offset}"
            f"&country%5B%5D=US"
        )
        data = http.get_json(url)
        items = data.get("jobs", []) or []
        for j in items:
            jd_url = (
                ("https://www.amazon.jobs" + j["job_path"])
                if j.get("job_path") else j.get("url_next_step", "")
            )
            desc = " ".join(filter(None, [
                j.get("description"),
                j.get("basic_qualifications"),
                j.get("preferred_qualifications"),
            ]))
            out.append(Posting(
                company=company,
                role_title=(j.get("title") or "").strip(),
                jd_url=jd_url,
                location=j.get("normalized_location") or j.get("location") or "",
                description=desc,
                posted=day_from_text(j.get("posted_date") or ""),
            ))
        total = data.get("hits", 0)
        offset += page_size
        if not items or offset >= total:
            break
        if offset > 2000:
            out.complete = False
            break
    return out


def fetch_simplify(http: HttpClient) -> list[dict]:
    """SimplifyJobs/New-Grad-Positions listings.json (about 13 MB, one request)."""
    data = http.get_json(SIMPLIFY_LISTINGS_URL)
    if not isinstance(data, list):
        raise ValueError("unexpected listings.json shape")
    return data


def simplify_postings(listings: list[dict], today: date) -> tuple[list[Posting], set[str]]:
    """Recent active software/AI listings as Postings, plus hashes of every active listing.

    Listings marked as not sponsoring or citizenship-only are dropped here, since
    filter_posting has no description to read for them.
    """
    postings: list[Posting] = []
    active: set[str] = set()
    for item in listings:
        if not item.get("active") or not item.get("is_visible", True):
            continue
        url = item.get("url") or ""
        h = url_hash(url)
        if h:
            active.add(h)
        if item.get("category") not in SIMPLIFY_CATEGORIES:
            continue
        if item.get("sponsorship") in SIMPLIFY_BLOCKED_SPONSORSHIP:
            continue
        posted = item.get("date_posted") or 0
        if not posted or (today - date.fromtimestamp(posted)).days > SIMPLIFY_MAX_AGE_DAYS:
            continue
        postings.append(Posting(
            company=(item.get("company_name") or "").strip(),
            role_title=(item.get("title") or "").strip(),
            jd_url=url,
            location="; ".join(item.get("locations") or []),
            posted=day_from_epoch(posted),
        ))
    return postings, active


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ENTITY_MAP = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&#39;": "'", "&quot;": '"', "&rsquo;": "'", "&lsquo;": "'",
}


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = _TAG_RE.sub(" ", s)
    for k, v in _ENTITY_MAP.items():
        s = s.replace(k, v)
    return _WS_RE.sub(" ", s).strip()


def has_new_grad_signal(p: "Posting") -> bool:
    if NEW_GRAD_TITLE_RE.search(p.role_title):
        return True
    if p.description and NEW_GRAD_DESC_RE.search(p.description[:3000]):
        return True
    return False


def parse_min_years(text: str) -> int | None:
    if not text:
        return None
    # Look only at the first ~5k chars to avoid pulling numbers from boilerplate.
    candidates: list[int] = []
    for m in YEARS_REQ_RE.finditer(text[:5000]):
        try:
            candidates.append(int(m.group(1)))
        except (ValueError, TypeError):
            continue
    if not candidates:
        return None
    return min(candidates)


def detect_salary(text: str) -> str:
    if not text:
        return ""
    m = SALARY_RE.search(text)
    if not m:
        return ""
    lo = m.group(1).strip().rstrip(",")
    hi = m.group(2).strip().rstrip(",")
    return f"${lo} - ${hi}"


def location_verdict(loc: str) -> tuple[str, str]:
    """Return (verdict, reason). verdict in {us, nonus, unknown}."""
    if not loc:
        return "unknown", "no location"
    lo = " " + loc.lower() + " "
    nonus_hit = any(h in lo for h in NON_US_HINTS)
    us_hit = any(h in lo for h in US_HINTS)
    strong_us = any(h in lo for h in US_HINTS if h not in WEAK_US_HINTS)
    weak_us = sum(h in lo for h in WEAK_US_HINTS)
    if nonus_hit and not strong_us and weak_us < 2:
        return "nonus", f"non-US location: {loc.strip()}"
    if us_hit:
        return "us", ""
    return "unknown", f"location unclear: {loc.strip()}"


def title_verdict(title: str, company: str = "") -> tuple[bool, str]:
    rule = COMPANY_RULES.get(company)
    if rule and rule["title_re"] and not rule["title_re"].match(title):
        return False, f"{company}: title outside the roles you take from {company}"
    if INTERN_BLOCKER.search(title):
        return False, "intern/co-op"
    if NON_ENG_BLOCKERS and NON_ENG_BLOCKERS.search(title):
        return False, "not the kind of role you're after"
    if DROP_MANAGER and MANAGER_BLOCKER.search(title):
        return False, "manager role"
    if ENGINEERING_INDICATORS and not ENGINEERING_INDICATORS.search(title):
        return False, "title doesn't match your target roles"
    if SENIOR_BLOCKERS.search(title):
        return False, "senior/staff/principal level"
    if DROP_LEAD and LEAD_BLOCKER.search(title):
        return False, "lead-level role"
    if MID_LEVEL_BLOCKER.search(title):
        return False, f"level above {_ROMANS[MAX_LEVEL - 1].upper()} — typically more years than you have"
    if STUDENT_BLOCKER.search(title) or (DROP_CONTRACT and CONTRACT_BLOCKER.search(title)):
        return False, "student / contract role"
    if DROP_APPRENTICE and APPRENTICE_BLOCKER.search(title):
        return False, "apprenticeship"
    if DROP_PHD and DEGREE_BLOCKER.search(title):
        return False, "PhD / postdoc role"
    m = OFF_PROFILE_BLOCKERS.search(title) if OFF_PROFILE_BLOCKERS else None
    if m:
        return False, f"specialist area outside your background ({m.group(0).strip()})"
    m = NOT_TARGET_TRACK.search(title) if NOT_TARGET_TRACK else None
    if m:
        return False, f"a track you don't want ({m.group(0).strip()})"
    m = NON_US_TITLE_RE.search(title)
    if m:
        return False, f"non-US role ({m.group(0)})"
    return True, ""


def blocks_you(text: str) -> str:
    """Why a posting's sponsorship/citizenship wording rules this person out ("" if it doesn't)."""
    m = NO_SPONSOR_RE.search(text or "")
    if not m:
        return ""
    if re.search(r"citizen|clearance", m.group(0), re.I):
        return "" if US_CITIZEN else "requires US citizenship or a clearance"
    return "JD rules out visa sponsorship" if NEEDS_SPONSORSHIP else ""


def filter_posting(p: Posting) -> tuple[str, list[str]]:
    """Return (action, reasons). action in {keep, drop, flag}."""
    flags: list[str] = []

    ok, why = title_verdict(p.role_title, p.company)
    if not ok:
        return "drop", [why]

    # Amazon-strict: their unleveled SDE titles often hide mid-level expectations.
    # Require an explicit new-grad signal (title tag like "2026 (US)" / "University"
    # or a description that calls out entry-level) to survive.
    rule = COMPANY_RULES.get(p.company)
    if rule and rule["new_grad"] and not has_new_grad_signal(p):
        return "drop", [f"{p.company}: no explicit new-grad/entry-level signal"]

    verdict, reason = location_verdict(p.location)
    if verdict == "nonus":
        return "drop", [reason]
    if verdict == "unknown":
        flags.append(reason)

    why = blocks_you(f"{p.role_title} {p.description}")
    if why:
        return "drop", [why]

    yrs = parse_min_years(p.description)
    if yrs is not None and yrs > MAX_YEARS_REQUIRED:
        # If the JD also explicitly invites new grads, surface it as a flag
        # rather than dropping it outright.
        head = (p.role_title + " " + p.description[:2000])
        if NEW_GRAD_HINTS.search(head):
            flags.append(f"says {yrs}+ years but also flags new-grad eligibility")
        else:
            return "drop", [f"requires {yrs}+ years experience"]

    return ("flag" if flags else "keep"), flags


# ---------------------------------------------------------------------------
# Tracker IO
# ---------------------------------------------------------------------------

def iter_known_rows():
    """Rows already tracked: tracker.csv plus every postings file (imported or not)."""
    paths = [TRACKER_PATH] if TRACKER_PATH.exists() else []
    if POSTINGS_DIR.exists():
        paths += sorted(POSTINGS_DIR.glob("*.csv"))
    for path in paths:
        with path.open("r", newline="", encoding="utf-8") as f:
            yield from csv.DictReader(f)


class KnownJobs:
    """Dedup index: job identity for everything, plus company + title for recent rows.

    The company + title check only applies to the Simplify list, where the same role
    arrives with a different URL than the company's own board. Company boards often
    post one title in several locations, and those are separate openings.
    """

    def __init__(self, today: date):
        self.ids: set[str] = set()
        self.recent_titles: set[tuple[str, str]] = set()
        self._cutoff = today.toordinal() - TITLE_DUP_WINDOW_DAYS
        for row in iter_known_rows():
            self.add(row.get("company", ""), row.get("role_title", ""),
                     row.get("jd_url", ""), parse_date(row.get("date_added", "")))

    def add(self, company: str, title: str, url: str, added: date | None = None) -> None:
        ident = job_identity(url)
        if ident:
            self.ids.add(ident)
        if added is None or added.toordinal() >= self._cutoff:
            self.recent_titles.add((company_key(company), title_key(title)))

    def seen(self, p: Posting, check_titles: bool = False) -> bool:
        if job_identity(p.jd_url) in self.ids:
            return True
        return check_titles and (company_key(p.company), title_key(p.role_title)) in self.recent_titles


def write_postings(rows: list[dict], today: date) -> Path | None:
    if not rows:
        return None
    POSTINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = POSTINGS_DIR / f"{today.isoformat()}.csv"
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKER_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)
    return path


def write_open_jobs(companies: dict[str, set[str]], simplify_active: set[str] | None,
                    today: date) -> None:
    """One hash per line so the nightly diff stays small."""
    OPEN_JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": today.isoformat(),
        "note": "url_hash() of every posting still listed. Only companies whose "
                "full list was fetched tonight are included.",
        "companies": {name: sorted(h) for name, h in sorted(companies.items())},
        "simplify_active": sorted(simplify_active) if simplify_active is not None else None,
    }
    OPEN_JOBS_PATH.write_text(json.dumps(data, indent=0) + "\n", encoding="utf-8")


def posting_to_row(p: Posting, sponsors_h1b: str, flags: list[str], source: str = "") -> dict:
    notes_parts = [f"src:{source}"] if source else []
    if has_new_grad_signal(p):
        notes_parts.append("new-grad")
    if flags:
        notes_parts.append("flagged: " + "; ".join(flags))
    return {
        "date_added": date.today().isoformat(),
        "date_posted": p.posted,
        "company": p.company,
        "role_title": p.role_title,
        "jd_url": p.jd_url,
        "location": p.location,
        "salary_range": p.salary_range or detect_salary(p.description),
        "sponsors_h1b": sponsors_h1b,
        "resume_variant": "",
        "status": "to_review",
        "referral_contact": "",
        "date_applied": "",
        "response_received": "",
        "response_date": "",
        "next_action": "",
        "next_action_date": "",
        "notes": "; ".join(notes_parts),
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

SLUG_FETCHERS: dict[str, Callable[[HttpClient, str, str], list[Posting]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
}

CUSTOM_FETCHERS: dict[str, Callable[[HttpClient, str, str], list[Posting]]] = {
    # Map company name (lower) -> fetcher. Add more as you implement them.
    "amazon": fetch_amazon,
}


def scrape_entry(http: HttpClient, entry: dict) -> list[Posting]:
    company = entry["company"]
    provider = (entry.get("ats_provider") or "").lower()

    if provider in SLUG_FETCHERS:
        slug = entry.get("ats_slug")
        if not slug:
            raise ValueError(f"missing ats_slug for {provider} provider")
        return SLUG_FETCHERS[provider](http, company, slug)

    if provider == "workday":
        career_url = entry.get("career_url") or ""
        # "search": a Workday search term, for boards bigger than the fetch cap (NVIDIA has 2,000+).
        return fetch_workday(http, company, career_url, entry.get("search", ""))

    if provider == "custom":
        key = company.lower().split()[0]
        if key in CUSTOM_FETCHERS:
            return CUSTOM_FETCHERS[key](http, company, entry.get("career_url", ""))
        raise NotImplementedError(
            f"no custom parser yet for {company} ({entry.get('career_url', '')})"
        )

    raise ValueError(f"unknown ats_provider: {provider!r}")


# ---------------------------------------------------------------------------
# Logging + main
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("scraper")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def iter_company_entries(cfg: dict):
    for tier, val in cfg.items():
        if tier.startswith("_"):
            continue
        if isinstance(val, list):
            for entry in val:
                yield tier, entry


def screen_postings(http: HttpClient, postings: list[Posting], known: KnownJobs, *,
                    sponsors: str, provider: str = "", career_url: str = "",
                    source: str = "", check_titles: bool = False
                    ) -> tuple[list[dict], dict[str, int]]:
    """Dedupe and filter one source's postings. Returns new rows and counts."""
    rows: list[dict] = []
    c: dict[str, int] = defaultdict(int)
    for p in postings:
        if not p.role_title or not p.jd_url:
            continue
        if known.seen(p, check_titles=check_titles):
            c["dup"] += 1
            continue
        action, reasons = filter_posting(p)
        if action == "drop":
            c["dropped"] += 1
            continue
        # If the posting survived stage-1 but came from a provider whose
        # list endpoint returns no description (workday today), fetch the
        # detail page and re-filter. This catches "requires 5+ years" /
        # "no visa sponsorship" cases that the title-only pass misses.
        if not p.description and provider == "workday" and career_url:
            try:
                backfill_workday_description(http, career_url, p)
                c["backfilled"] += 1
                action, reasons = filter_posting(p)
                if action == "drop":
                    c["dropped"] += 1
                    continue
            except (urllib.error.HTTPError, urllib.error.URLError,
                    json.JSONDecodeError, ValueError):
                pass  # leave description empty; keep the row
        rows.append(posting_to_row(p, sponsors, reasons, source))
        known.add(p.company, p.role_title, p.jd_url)
        c["flagged" if action == "flag" else "kept"] += 1
    return rows, c


def main() -> int:
    log = setup_logging()
    log.info("=" * 70)
    log.info("Scraper run start")

    try:
        cfg = json.loads(COMPANIES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.error("companies.json not found at %s", COMPANIES_PATH)
        return 2

    today = date.today()
    known = KnownJobs(today)
    log.info("Loaded %d known jobs for dedup", len(known.ids))

    http = HttpClient()
    counts: dict[str, int] = defaultdict(int)
    new_rows: list[dict] = []
    open_by_company: dict[str, set[str]] = {}

    def tally(c: dict[str, int], fetched: int) -> None:
        counts["fetched"] += fetched
        for k in ("kept", "flagged", "dropped"):
            counts[k] += c[k]
        counts["duplicates"] += c["dup"]

    for tier, entry in iter_company_entries(cfg):
        company = entry.get("company", "?")
        provider = entry.get("ats_provider", "")

        try:
            postings = scrape_entry(http, entry)
        except NotImplementedError as e:
            log.warning("SKIP %s [%s/%s]: %s", company, tier, provider, e)
            counts["unsupported"] += 1
            continue
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            log.error("HTTP fail %s [%s/%s]: %s", company, tier, provider, e)
            counts["errors"] += 1
            continue
        except (ValueError, json.JSONDecodeError, KeyError) as e:
            log.error("Parse fail %s [%s/%s]: %s", company, tier, provider, e)
            counts["errors"] += 1
            continue

        # Only a company's full list says which of its postings closed. An entry with a search
        # term sees just part of the board, so it adds postings but never marks any closed.
        complete = getattr(postings, "complete", True) and not entry.get("search")
        if complete:
            open_by_company.setdefault(company, set()).update(
                h for h in map(url_hash, (p.jd_url for p in postings)) if h)

        # "sponsors": whether this employer is known to sponsor H-1Bs ("yes", the default, or
        # "unclear" for companies added without a record checked).
        rows, c = screen_postings(
            http, postings, known, sponsors=entry.get("sponsors", "yes"), provider=provider,
            career_url=entry.get("career_url", "") if provider == "workday" else "",
        )
        new_rows += rows
        log.info(
            "%s [%s/%s]: fetched=%d kept=%d flagged=%d dropped=%d dup=%d%s%s",
            company, tier, provider, len(postings), c["kept"], c["flagged"],
            c["dropped"], c["dup"],
            f" desc-backfilled={c['backfilled']}" if c["backfilled"] else "",
            "" if complete else " (list truncated; closed check skipped)",
        )
        tally(c, len(postings))

    simplify_active: set[str] | None = None
    listings = None
    try:
        listings = fetch_simplify(http) if SIMPLIFY_ENABLED else None
    except (urllib.error.URLError, urllib.error.HTTPError,
            ValueError, json.JSONDecodeError) as e:
        log.error("HTTP/parse fail Simplify new-grad list: %s", e)
        counts["errors"] += 1
    if listings is not None:
        postings, simplify_active = simplify_postings(listings, today)
        rows, c = screen_postings(http, postings, known, sponsors="unclear",
                                  source="simplify", check_titles=True)
        new_rows += rows
        log.info(
            "Simplify new-grad list: active=%d recent=%d kept=%d flagged=%d dropped=%d dup=%d",
            len(simplify_active), len(postings), c["kept"], c["flagged"],
            c["dropped"], c["dup"],
        )
        tally(c, len(postings))

    path = write_postings(new_rows, today)
    write_open_jobs(open_by_company, simplify_active, today)
    log.info(
        "Run done. fetched=%d new=%d (kept=%d flagged=%d) dropped=%d dup=%d "
        "errors=%d unsupported=%d -> %s",
        counts["fetched"], len(new_rows), counts["kept"], counts["flagged"],
        counts["dropped"], counts["duplicates"], counts["errors"],
        counts["unsupported"], path.relative_to(PROJECT_DIR) if path else "no new rows",
    )
    if len(new_rows) > NEW_ROWS_WARN_THRESHOLD:
        log.warning(
            "New rows = %d (>%d). Filters may be too loose — review companies.json "
            "or tighten the patterns in scraper.py.",
            len(new_rows), NEW_ROWS_WARN_THRESHOLD,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
