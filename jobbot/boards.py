"""Find a company's public job board from its name, for the setup wizard's company list.

Tries likely board names ("Epic Games" -> epicgames, epic-games, epic) on the job-board
systems with public APIs: Greenhouse, Lever, Ashby, Workable and SmartRecruiters. A board
counts as found when the API answers with a job list. Companies on Workday or their own
career site aren't found this way; the wizard asks for their careers link instead.

One request per system at a time with a short pause; different systems run in parallel.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = "jobbot/1.0 (personal job search)"
SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|the)\b\.?", re.I)
DROPPABLE = re.compile(r"\b(studios?|games|entertainment|animation|technologies|technology|labs|interactive|media|digital|pictures)\b", re.I)

PROVIDERS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                   lambda d: len(d.get("jobs", [])) if isinstance(d, dict) and "jobs" in d else None),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json",
              lambda d: len(d) if isinstance(d, list) else None),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}",
              lambda d: len(d.get("jobs", [])) if isinstance(d, dict) and "jobs" in d else None),
    "workable": ("https://www.workable.com/api/accounts/{slug}",
                 lambda d: len(d.get("jobs", [])) if isinstance(d, dict) and "jobs" in d else None),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
                        lambda d: d.get("totalFound") if isinstance(d, dict) and d.get("totalFound") else None),
}


def slugs(name: str) -> list[str]:
    """Board names to try, most likely first."""
    base = SUFFIXES.sub(" ", name or "").strip()
    short = DROPPABLE.sub(" ", base).strip() or base
    out = []
    for text in (base, short):
        words = re.findall(r"[a-z0-9]+", text.lower())
        for slug in ("".join(words), "-".join(words), words[0] if words else ""):
            if slug and slug not in out:
                out.append(slug)
    return out[:5]


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


BOARD_URLS = {"greenhouse": "https://job-boards.greenhouse.io/{slug}", "lever": "https://jobs.lever.co/{slug}",
              "ashby": "https://jobs.ashbyhq.com/{slug}", "workable": "https://apply.workable.com/{slug}",
              "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}"}


def probe(name: str) -> list[dict]:
    """Boards found for one company: [{company, ats_provider, ats_slug, open_jobs, sure, board_url}].

    sure is False when only a one-word guess ("riot" for Riot Games) matched: that board may
    belong to another company with the same word, so the wizard asks you to check the link.
    """
    tries = slugs(name)
    multi_word = len(re.findall(r"[a-z0-9]+", SUFFIXES.sub(" ", name or "").lower())) > 1

    def check(provider: str) -> dict | None:
        url, count = PROVIDERS[provider]
        for i, slug in enumerate(tries):
            if i:
                time.sleep(0.4)
            n = count(_get_json(url.format(slug=slug)))
            if n:  # an empty board is as good as no board for a job search
                sure = not (multi_word and slug == re.findall(r"[a-z0-9]+", name.lower())[0])
                return {"company": name, "ats_provider": provider, "ats_slug": slug, "open_jobs": int(n),
                        "sure": sure, "board_url": BOARD_URLS[provider].format(slug=slug)}
        return None

    with ThreadPoolExecutor(max_workers=len(PROVIDERS)) as pool:
        found = [r for r in pool.map(check, PROVIDERS) if r]
    return sorted(found, key=lambda r: -r["open_jobs"])


def probe_many(names: list[str], workers: int = 4) -> dict[str, list[dict]]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(names, pool.map(probe, names)))
