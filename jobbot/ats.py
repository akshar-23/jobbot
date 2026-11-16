"""Which application system a job link uses, and job details for links added by hand."""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

# The Day 2 bot fills these forms itself; everything else goes to "needs manual".
AUTO_PROVIDERS = {"greenhouse", "lever", "ashby"}

USER_AGENT = "jobbot/1.0 (personal job search)"
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def provider(url: str) -> str:
    parts = urlsplit((url or "").strip())
    host, query = parts.netloc.lower(), parts.query.lower()
    if "greenhouse.io" in host or "gh_jid=" in query:
        return "greenhouse"
    if host.endswith("lever.co"):
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "myworkdayjobs.com" in host:
        return "workday"
    for name in ("smartrecruiters", "workable", "icims", "linkedin", "handshake"):
        if name in host:
            return name
    return "custom" if host else ""


def _get(url: str, accept: str = "application/json") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _pretty_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def _greenhouse(url: str) -> dict:
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    q = parse_qs(parts.query)
    slug = job = ""
    if "jobs" in segs:
        i = segs.index("jobs")
        slug = segs[i - 1] if i > 0 else ""
        job = segs[i + 1] if i + 1 < len(segs) else ""
    if "for" in q and "token" in q:          # embed/job_app?for=slug&token=id
        slug, job = q["for"][0], q["token"][0]
    if not (slug and job.isdigit()):
        return {}
    data = json.loads(_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job}"))
    company = data.get("company_name") or ""
    if not company:
        try:
            company = json.loads(_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")).get("name", "")
        except (urllib.error.URLError, ValueError):
            company = ""
    return {
        "company": company or _pretty_slug(slug),
        "role_title": data.get("title", ""),
        "location": (data.get("location") or {}).get("name", ""),
    }


def _lever(url: str) -> dict:
    segs = [s for s in urlsplit(url).path.split("/") if s]
    m = _UUID_RE.search(url)
    if not segs or not m:
        return {}
    data = json.loads(_get(f"https://api.lever.co/v0/postings/{segs[0]}/{m.group(0)}"))
    return {
        "company": _pretty_slug(segs[0]),
        "role_title": data.get("text", ""),
        "location": (data.get("categories") or {}).get("location", ""),
    }


def _ashby(url: str) -> dict:
    segs = [s for s in urlsplit(url).path.split("/") if s]
    m = _UUID_RE.search(url)
    if not segs or not m:
        return {}
    data = json.loads(_get(
        f"https://api.ashbyhq.com/posting-api/job-board/{segs[0]}?includeCompensation=true"))
    for job in data.get("jobs", []):
        if m.group(0).lower() in (job.get("id", "") + job.get("jobUrl", "")).lower():
            comp = job.get("compensation") or {}
            return {
                "company": _pretty_slug(segs[0]),
                "role_title": job.get("title", ""),
                "location": job.get("location") or "",
                "salary_range": comp.get("compensationTierSummary") or "",
            }
    return {}


def _html_page(url: str) -> dict:
    page = _get(url, accept="text/html")
    def meta(prop: str) -> str:
        m = re.search(rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']*)', page, re.I)
        return html.unescape(m.group(1)).strip() if m else ""
    title = meta("og:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        title = html.unescape(m.group(1)).strip() if m else ""
    return {"company": meta("og:site_name"), "role_title": title}


def fetch_metadata(url: str) -> dict:
    """Best-effort company, title, location and salary for a job link.

    Public job APIs for Greenhouse, Lever and Ashby; the page's own title otherwise.
    Returns what it found plus an 'error' key if the lookup failed.
    """
    kind = provider(url)
    fetchers = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}
    try:
        found = fetchers[kind](url) if kind in fetchers else {}
        if not found.get("role_title"):
            found = {**_html_page(url), **{k: v for k, v in found.items() if v}}
        return found
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        return {"error": f"couldn't read the page ({e})"}
