"""Rule-based ranking. No AI here: it runs on every dashboard load and must stay free.

Score = location + freshness + salary + new-grad signal + fit check verdict.
- Location: your places in order (profile/search.toml [[locations]]), then other US and
  remote, then unclear.
- Freshness: days since the job was posted (date_posted), or since we first saw
  it when the board doesn't say. Fresh postings get far more responses, so this
  weighs about as much as the location order.
- Salary at or above the target ranks up; a listed salary below the floor is
  flagged and hidden by default.
"""

from __future__ import annotations

import re
from datetime import date

from jobbot import eligibility
from jobbot import profile as person
from jobbot.ats import AUTO_PROVIDERS, provider
from jobbot.common import fit_tag, parse_date
from jobbot.settings import salary_bounds
from jobbot.tracker import job_id

# (max age in days, score bonus). Older than the last bucket: FRESHNESS_STALE.
FRESHNESS = [(3, 25), (7, 20), (14, 12), (30, 5), (60, 0)]
FRESHNESS_STALE = -15
# Fit check verdict (jobbot/fit.py). Unscreened rows get 0.
FIT_BONUS = {"good": 10, "stretch": 0, "poor": -40}

def location_tiers() -> list[tuple[int, str, re.Pattern]]:
    """Your location order from profile/search.toml [[locations]]: (tier, name, pattern), best first."""
    locs = person.search().get("locations", [])
    return [(n + 1, loc["name"], re.compile(loc["match"], re.I))
            for n, loc in enumerate(l for l in locs if l.get("name") and l.get("match"))]


OTHER_US_RE = re.compile(
    r"\bremote\b|united states|\busa\b|\bu\.s\.|,\s*(a[klrz]|c[aot]|d[ce]|fl|ga|hi|i[adln]|k[sy]|la"
    r"|m[deinost]|n[cdehjmvy]|o[hkr]|pa|ri|s[cd]|t[nx]|ut|v[at]|w[aivy])\b")

_RANGE_RE = re.compile(
    r"\$\s*([\d,.]+)\s*([kK])?\s*(?:-|–|—|to)\s*\$?\s*([\d,.]+)\s*([kK])?")
_SINGLE_RE = re.compile(r"\$\s*([\d,.]+)\s*([kK])?")


def location_tier(location: str) -> tuple[int, str]:
    """(tier, label): your listed places in order, then other US / remote, then unclear."""
    loc = (location or "").lower()
    named = location_tiers()
    hits = [(t, name) for t, name, rx in named if rx.search(loc)]
    if hits:
        return min(hits)
    if OTHER_US_RE.search(loc):
        return len(named) + 1, "Other US / remote"
    return len(named) + 2, "Unclear"


def _dollars(number: str, k_flag: str | None) -> float | None:
    try:
        value = float(number.replace(",", ""))
    except ValueError:
        return None
    if k_flag:
        return value * 1000
    if value < 300:        # hourly rate
        return value * 2080
    if value < 1000:       # "$150 - $200" with the K left off
        return value * 1000
    return value


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """Annual (low, high) in dollars from strings like '$120K – $180K' or '$58 - $72/hr'."""
    text = text or ""
    m = _RANGE_RE.search(text)
    if m:
        lo, hi = _dollars(m.group(1), m.group(2)), _dollars(m.group(3), m.group(4) or m.group(2))
    else:
        m = _SINGLE_RE.search(text)
        if not m:
            return None, None
        lo = hi = _dollars(m.group(1), m.group(2))
    if lo is None or hi is None:
        return None, None
    return int(min(lo, hi)), int(max(lo, hi))


def view(row: dict, today: date, floor: int, target: int) -> dict:
    """One tracker row as the dashboard shows it, with its score."""
    tier, tier_label = location_tier(row["location"])
    lo, hi = parse_salary(row["salary_range"])
    fit, fit_reason = fit_tag(row["notes"])
    # The fit reason is Claude's wording: keep it out of the keyword checks below.
    notes = ";".join(p for p in row["notes"].split(";") if not p.strip().startswith("fit:")).lower()
    posted = parse_date(row["date_posted"])
    seen = posted or parse_date(row["date_added"])
    age = (today - seen).days if seen else 365
    ats = provider(row["jd_url"])
    eligible, ineligible_reason = eligibility.check(row)

    score = 100 - 10 * tier
    score += next((bonus for max_age, bonus in FRESHNESS if age <= max_age), FRESHNESS_STALE)
    below_floor = hi is not None and hi < floor
    if below_floor:
        score -= 50
    elif hi is not None:
        score += 15 if hi >= target else 5
    if "new-grad" in notes:
        score += 10
    if "years but also flags new-grad" in notes:
        score -= 10
    if "location unclear" in notes:
        score -= 3
    score += FIT_BONUS.get(fit, 0)

    return {
        "id": job_id(row),
        "company": row["company"],
        "title": row["role_title"],
        "url": row["jd_url"],
        "location": row["location"],
        "tier": tier,
        "tier_label": tier_label,
        "salary": row["salary_range"],
        "salary_high": hi,
        "below_floor": below_floor,
        "ats": ats,
        "auto": ats in AUTO_PROVIDERS,
        "status": row["status"],
        "date_added": row["date_added"],
        "date_posted": row["date_posted"],
        "age_days": age,
        "age_is_posted": posted is not None,
        "eligible": eligible,
        "ineligible_reason": ineligible_reason,
        "notes": row["notes"],
        "fit": fit,
        "fit_reason": fit_reason,
        "score": score,
    }


def ranked(rows: list[dict], statuses: set[str] | None = None) -> list[dict]:
    floor, target = salary_bounds()
    today = date.today()
    views = [view(r, today, floor, target) for r in rows if statuses is None or r["status"] in statuses]
    views.sort(key=lambda v: (-v["score"], v["company"].lower(), v["title"].lower()))
    return views
