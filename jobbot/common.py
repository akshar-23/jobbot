"""Helpers shared by the nightly scraper and the local jobbot tool.

Standard library only: the scraper imports this module on GitHub Actions with no
install step beyond requirements.txt.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKER_COLUMNS = [
    "date_added", "date_posted", "company", "role_title", "jd_url", "location", "salary_range",
    "sponsors_h1b", "resume_variant", "status", "referral_contact", "date_applied",
    "response_received", "response_date", "next_action", "next_action_date", "notes",
]

DATE_COLUMNS = ("date_added", "date_posted", "date_applied", "response_date", "next_action_date")

# Tracking parameters that don't identify a job. Anything else in the query string is kept.
_DROP_PARAMS = {"ref", "source", "src", "gh_src", "lever-source", "lever-origin", "lever-via"}

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh)\b\.?", re.I
)


def normalize_url(url: str) -> str:
    """Lowercased URL with tracking params, fragments and trailing slashes removed.

    Used only for comparisons; tracker rows keep the original URL.
    """
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _DROP_PARAMS
    )
    path = parts.path.rstrip("/")
    scheme = (parts.scheme or "https").lower()
    return urlunsplit((scheme, parts.netloc.lower(), path, urlencode(query), "")).lower()


def job_identity(url: str) -> str:
    """Stable identity for a posting, so the same job matches across URL formats.

    Greenhouse, Lever, Ashby and Workday expose a job id in the URL; everything else
    falls back to the normalized URL.
    """
    norm = normalize_url(url)
    if not norm:
        return ""
    parts = urlsplit(norm)
    host, path = parts.netloc, parts.path
    query = dict(parse_qsl(parts.query))
    if "gh_jid" in query:
        return f"gh:{query['gh_jid']}"
    if "greenhouse.io" in host and "token" in query and "for" in query:  # embed/job_app?for=<board>&token=<id>
        return f"gh:{query['token']}"
    if "greenhouse.io" in host:
        m = re.search(r"/jobs/(\d+)", path)
        if m:
            return f"gh:{m.group(1)}"
    if host.endswith("lever.co") or "ashbyhq.com" in host:
        m = _UUID_RE.search(path)
        if m:
            return f"{'lever' if 'lever' in host else 'ashby'}:{m.group(0)}"
    if "myworkdayjobs.com" in host:
        # /…/job/<location>/<title>_<requisition>[/apply/applyManually…]: the requisition
        # segment comes right after the location, whatever follows it.
        segs = [s for s in path.split("/") if s]
        seg = segs[segs.index("job") + 2] if "job" in segs and len(segs) > segs.index("job") + 2 else segs[-1] if segs else ""
        if "_" in seg:
            return f"wd:{host.split('.')[0]}:{seg.rsplit('_', 1)[1]}"
    return norm


def url_hash(url: str) -> str:
    """Short hash of job_identity(), used in the open-jobs snapshot."""
    ident = job_identity(url)
    return hashlib.sha1(ident.encode("utf-8")).hexdigest()[:12] if ident else ""


def company_key(name: str) -> str:
    """Loose company match: 'Stripe, Inc.' and 'stripe' compare equal."""
    s = re.sub(r"\(.*?\)", " ", (name or "").lower())
    s = _COMPANY_SUFFIX_RE.sub(" ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def title_key(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def parse_date(value: str) -> date | None:
    """Parse ISO dates and the M/D/YYYY dates Excel leaves behind."""
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def iso_or_original(value: str) -> str:
    d = parse_date(value)
    return d.isoformat() if d else (value or "")


# --- Posted dates, as each job board reports them -> "YYYY-MM-DD" or "" ---

def day_from_iso(value: str) -> str:
    """'2026-10-19T11:17:24-04:00' -> '2026-10-19'."""
    value = (value or "").strip()
    return value[:10] if re.match(r"\d{4}-\d{2}-\d{2}", value) else ""


def day_from_epoch(value) -> str:
    """Unix time in seconds or milliseconds (Lever uses ms)."""
    try:
        t = float(value)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    if t > 1e11:
        t /= 1000
    return datetime.fromtimestamp(t).date().isoformat()


def day_from_workday(text: str, today: date) -> str:
    """Workday's 'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago'.

    '30+ Days Ago' is left blank: the exact date isn't known.
    """
    t = (text or "").lower()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return date.fromordinal(today.toordinal() - 1).isoformat()
    m = re.search(r"(\d+)\s*days?\s*ago", t)
    if m and "+" not in t:
        return date.fromordinal(today.toordinal() - int(m.group(1))).isoformat()
    return ""


def day_from_text(value: str) -> str:
    """'October 28, 2026' (Amazon) -> '2026-10-28'."""
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime((value or "").strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return day_from_iso(value)


# The fit check's verdict, kept in tracker notes as "fit: good — reason" (one per row).
FIT_VERDICTS = ("good", "stretch", "poor")
_FIT_TAG_RE = re.compile(r"^fit:\s*(good|stretch|poor)\b\s*(?:[—–-]\s*)?(.*)$", re.I)


def fit_tag(notes: str) -> tuple[str, str]:
    """(verdict, reason) from a row's notes, or ("", "") if it hasn't been screened."""
    for piece in (notes or "").split(";"):
        m = _FIT_TAG_RE.match(piece.strip())
        if m:
            return m.group(1).lower(), m.group(2).strip()
    return "", ""


def with_fit_tag(notes: str, verdict: str, reason: str) -> str:
    """Notes with the fit tag replaced. Reasons can't contain ';' (the notes separator)."""
    kept = [p.strip() for p in (notes or "").split(";") if p.strip() and not _FIT_TAG_RE.match(p.strip())]
    reason = re.sub(r"\s+", " ", reason.replace(";", ",")).strip()
    kept.append(f"fit: {verdict}" + (f" — {reason}" if reason else ""))
    return "; ".join(kept)
