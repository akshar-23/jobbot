"""Everything about the person using jobbot, read from profile/.

The code is the same for everyone; who you are and what you're looking for live in
profile/ (written by the setup wizard on the dashboard, editable by hand):

  facts.md            your background: the only facts Claude may use in letters and answers
  answers.toml        fixed form answers: contact, work authorization, salary, demographics
  search.toml         what to look for: title rules, level, years, locations, job sources
  companies.json      company job boards the nightly scraper checks
  bot.toml            application bot settings
  saved_answers.json  your own answers to questions nothing else covers

Before setup, profile/ doesn't exist and profile.example/ (shipped with the template) is
read instead, so nothing crashes on a fresh copy.

Standard library only: the nightly scraper imports this on GitHub Actions.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from jobbot.paths import PROJECT_DIR

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

# JOBBOT_PROFILE points at another profile folder (the tests use tests/fixtures/profile).
PROFILE_DIR = Path(os.environ["JOBBOT_PROFILE"]) if os.environ.get("JOBBOT_PROFILE") else PROJECT_DIR / "profile"
EXAMPLE_DIR = PROJECT_DIR / "profile.example"

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]

_cache: dict[str, tuple[float, dict]] = {}


def path(name: str) -> Path:
    """profile/<name>, or the example copy when there's no profile yet."""
    mine = PROFILE_DIR / name
    return mine if mine.exists() or not (EXAMPLE_DIR / name).exists() else EXAMPLE_DIR / name


def is_set_up() -> bool:
    return (PROFILE_DIR / "answers.toml").exists() and (PROFILE_DIR / "search.toml").exists()


def _toml(name: str) -> dict:
    p = path(name)
    if tomllib is None or not p.exists():
        return {}
    mtime = p.stat().st_mtime
    hit = _cache.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    with p.open("rb") as f:
        data = tomllib.load(f)
    _cache[str(p)] = (mtime, data)
    return data


_TRUE, _FALSE = {"true", "yes", "y"}, {"false", "no", "n"}


def _booleans(node, key: str = ""):
    """"true"/"false" typed as text become real booleans, so hand edits like
    government_employee = "false" don't read as true. Demographic answers stay text ("No")."""
    if isinstance(node, dict):
        return {k: (v if k in _TEXT_KEYS else _booleans(v, k)) for k, v in node.items()}
    if isinstance(node, str) and node.strip().lower() in _TRUE | _FALSE:
        return node.strip().lower() in _TRUE
    return node


# Keys whose "No" / "Yes" is an answer to match on a form, not a switch.
_TEXT_KEYS = {"gender", "race_ethnicity", "hispanic_latino", "veteran_status", "disability_status", "pronouns",
              "how_did_you_hear", "earliest_start"}


def answers() -> dict:
    """answers.toml, with true/false typed as text read as booleans (a fresh copy each call)."""
    return _booleans(_toml("answers.toml"))


def search() -> dict:
    return _toml("search.toml")


def bot() -> dict:
    return _toml("bot.toml")


def file_name(kind: str) -> str:
    """Name of an uploaded file: 'Jamie_Example_Resume.pdf' / 'Jamie_Example_Cover_Letter.pdf'."""
    return f"{file_stem()}_{'Resume' if kind == 'resume' else 'Cover_Letter'}.pdf"


def search_mtime() -> float:
    p = path("search.toml")
    return p.stat().st_mtime if p.exists() else 0.0


def full_name() -> str:
    ident = answers().get("identity", {})
    return f"{ident.get('first_name', '')} {ident.get('last_name', '')}".strip() or "the candidate"


def file_stem() -> str:
    """'Jamie Example' -> 'Jamie_Example', for the names of uploaded files."""
    return re.sub(r"[^A-Za-z0-9]+", "_", full_name()).strip("_") or "Applicant"


def state_name(code: str) -> str:
    return US_STATES.get((code or "").strip().upper(), code or "")


def month_year(value: str) -> str:
    """'2026-05' -> 'May 2026' (anything else is returned unchanged)."""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", (value or "").strip())
    return f"{MONTHS[int(m.group(2)) - 1]} {m.group(1)}" if m and 1 <= int(m.group(2)) <= 12 else (value or "")


def national_phone(phone: str) -> str:
    """'+1 555-010-0123' -> '555-010-0123': for forms with their own country-code picker."""
    return re.sub(r"^\s*\+\d{1,3}[\s.\-]*", "", phone or "").strip() or (phone or "")


def needs_sponsorship() -> bool:
    return bool(answers().get("work_authorization", {}).get("requires_sponsorship", False))


def words_regex(words: list[str]) -> re.Pattern | None:
    """A list of title words/patterns -> one case-insensitive whole-word regex (None if empty)."""
    words = [w for w in words or [] if w]
    return re.compile(r"\b(" + "|".join(words) + r")\b", re.I) if words else None
