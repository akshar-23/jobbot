"""Reading and writing profile/ for the setup wizard (jobbot/web/setup.html).

The wizard sends back whole sections; they're laid over what's already on disk (or the
example profile), so settings the wizard doesn't show are kept. Files are rewritten with
short explanatory comments; comments you added by hand are not kept.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date

from jobbot import profile as person
from jobbot.paths import PROJECT_DIR

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

ANSWERS_ORDER = ["identity", "address", "links", "documents", "work_authorization", "preferences",
                 "education", "current_job", "voluntary_disclosures", "misc"]
SEARCH_ORDER = ["target", "titles", "company_rules", "sources", "locations", "fit"]

# One-line comments written above keys (section, key) or sections (section, "").
DOCS = {
    ("identity", ""): "Contact details, copied into forms exactly as written.",
    ("identity", "location"): 'Answer to "Where are you based?"',
    ("identity", "form_city"): "Typed into city-search boxes (must be a real city, not a region).",
    ("address", ""): "Mailing address for forms that ask (Workday). Blank line1 = those come to you.",
    ("links", ""): "Links for LinkedIn / portfolio / reel / GitHub questions. Blank = not asked for.",
    ("documents", "resume_pdf"): "The resume the bot and the browser helper upload.",
    ("work_authorization", ""): "Work authorization and visa answers: never written by Claude.",
    ("work_authorization", "requires_sponsorship"): "true = you'll need a visa sponsored (H-1B etc.) now or later.",
    ("work_authorization", "current_status"): 'e.g. "F-1 OPT", "Green card", "US citizen"',
    ("work_authorization", "opt_end_date"): "YYYY-MM-DD, only on OPT.",
    ("preferences", "salary_floor"): "Listings that show less are hidden by default.",
    ("preferences", "salary_target"): 'Ranked higher; also the answer to "desired salary".',
    ("preferences", "prefers_remote"): '"If remote is allowed, do you plan to work remotely?"',
    ("preferences", "open_to_onsite"): "In-office / hybrid questions (Yes/No only).",
    ("education", "graduation"): "YYYY-MM",
    ("voluntary_disclosures", ""): ("Optional on every form. Write the meaning; jobbot finds each form's wording. "
                                    '"Decline to self-identify" picks the decline option.'),
    ("voluntary_disclosures", "race_ethnicity"): "A list: the first one a form offers is picked.",
    ("misc", "relatives_at_companies"): '"" = ask me; a list of companies; [] = none anywhere.',
    ("misc", "government_employee"): '"" = ask me; true / false = jobbot answers it everywhere.',
    ("misc", "needs_accommodation"): '"" = ask me; true / false.',
    ("misc", "can_perform_essential_functions"): '"" = ask me; true / false.',
    ("target", ""): "Level and years. Titles levelled above max_level are dropped.",
    ("target", "level_nouns"): 'Words a level number follows ("Engineer III", "Animator 2").',
    ("target", "max_years_required"): "Postings asking more years are dropped unless they invite new grads.",
    ("titles", ""): "Title patterns: regular expressions matched as whole words, ignoring case.",
    ("titles", "include"): "A title must contain one of these to be considered.",
    ("titles", "not_target"): "Not the kind of job you're after.",
    ("titles", "outside_background"): "Specialist areas outside your background.",
    ("titles", "other_tracks"): "Other tracks you don't want.",
    ("sources", "simplify_new_grad"): "The SimplifyJobs New-Grad list (tech roles only).",
    ("locations", ""): "Location order for ranking, best first. Remote and other US places rank after these.",
    ("fit", "guide"): "What the fit check (Claude) weighs. {years} and {years_plus} come from max_years_required.",
}


# ---------------------------------------------------------------------------
# TOML writing
# ---------------------------------------------------------------------------

def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if "\n" in text and "'''" not in text:
        return "'''\n" + text.strip("\n") + "\n'''"
    if "\\" in text and "'" not in text and "\n" not in text:
        return "'" + text + "'"  # regex patterns read better unescaped
    return json.dumps(text, ensure_ascii=False)


def _array(values: list) -> str:
    items = [_scalar(v) for v in values]
    one_line = "[" + ", ".join(items) + "]"
    if len(one_line) <= 90 and "\n" not in one_line:
        return one_line
    return "[\n" + "".join(f"    {i},\n" for i in items) + "]"


def _value(value) -> str:
    return _array(value) if isinstance(value, list) else _scalar(value)


def _keys(section: str, table: dict) -> list[str]:
    out = []
    for key, value in table.items():
        if isinstance(value, dict) or (isinstance(value, list) and value and isinstance(value[0], dict)):
            continue
        doc = DOCS.get((section, key))
        if doc:
            out.append(f"# {doc}")
        out.append(f"{key} = {_value(value)}")
    return out


def dump(data: dict, order: list[str], header: str) -> str:
    lines = [f"# {line}" if line else "#" for line in header.splitlines()] + [""]
    names = [k for k in order if k in data] + [k for k in data if k not in order]
    top = {k: v for k, v in data.items() if not isinstance(v, (dict, list)) or
           (isinstance(v, list) and not (v and isinstance(v[0], dict)))}
    if top:
        lines += _keys("", top) + [""]
    for name in names:
        value = data[name]
        if isinstance(value, dict):
            if DOCS.get((name, "")):
                lines.append(f"# {DOCS[(name, '')]}")
            lines += [f"[{name}]"] + _keys(name, value) + [""]
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            if DOCS.get((name, "")):
                lines.append(f"# {DOCS[(name, '')]}")
            for item in value:
                lines += [f"[[{name}]]"] + _keys(name, item) + [""]
    text = "\n".join(lines).rstrip() + "\n"
    if tomllib:
        tomllib.loads(text)  # never write a file the reader can't load
    return text


# ---------------------------------------------------------------------------
# Snapshot and save
# ---------------------------------------------------------------------------

def _companies_flat() -> list[dict]:
    try:
        cfg = json.loads(person.path("companies.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for group, entries in cfg.items():
        if group.startswith("_") or not isinstance(entries, list):
            continue
        for e in entries:
            out.append({"group": group, **e})
    return out


# Before setup the example profile supplies defaults, but nothing about the example person.
PERSONAL_SECTIONS = ("identity", "address", "links", "documents", "education", "current_job")


def _blank(node):
    if isinstance(node, dict):
        return {k: _blank(v) for k, v in node.items()}
    if isinstance(node, list):
        return []
    return "" if isinstance(node, str) else node


def snapshot() -> dict:
    """Everything the wizard shows: your profile, or blank fields with defaults before setup."""
    set_up = person.is_set_up()
    answers = copy.deepcopy(person._toml("answers.toml"))   # raw: "" (ask me) stays distinct from false
    search = copy.deepcopy(person.search())
    facts = person.path("facts.md")
    facts_text = facts.read_text(encoding="utf-8") if facts.exists() else ""
    companies = _companies_flat()
    if not set_up:
        for name in PERSONAL_SECTIONS:
            answers[name] = _blank(answers.get(name, {}))
        answers.setdefault("address", {})["phone_type"] = "Mobile"
        answers.setdefault("identity", {})["country"] = "United States"
        search["titles"] = _blank(search.get("titles", {}))
        search["locations"], search["fit"] = [], {"guide": ""}
        search.setdefault("target", {})["goal"] = ""
        answers.setdefault("preferences", {})["preferred_locations"] = []
        facts_text, companies = "", []
    return {"set_up": set_up, "answers": answers, "search": search, "bot": person.bot(),
            "facts": facts_text, "companies": companies}


def _merge(base: dict, new: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (new or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def save(payload: dict) -> list[str]:
    """Write the sections the wizard sent. Returns the files written."""
    person.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    stamp = date.today().isoformat()
    for name, order, key in (("answers.toml", ANSWERS_ORDER, "answers"), ("search.toml", SEARCH_ORDER, "search"),
                             ("bot.toml", [], "bot")):
        if key not in payload:
            continue
        data = _merge(person._toml(name), payload[key])
        if key == "answers":
            for field, link in data.get("links", {}).items():
                if isinstance(link, str) and link and not re.match(r"https?://", link):
                    data["links"][field] = "https://" + link.lstrip("/")
        header = {"answers": "Fixed answers for application forms (setup wizard, {d}).\n"
                             "Never written by Claude: work authorization, sponsorship, demographics and salary\n"
                             "always come from here. Edit here or on the dashboard's Profile page.",
                  "search": "What to look for and how to judge it (setup wizard, {d}).\n"
                            "Read by the nightly scraper, the dashboard and the fit check.",
                  "bot": "Application bot settings (setup wizard, {d}). The dashboard's To apply tab\n"
                         "changes live_handlers and auto_run_on_pick."}[key].format(d=stamp)
        (person.PROFILE_DIR / name).write_text(dump(data, order, header), encoding="utf-8")
        written.append(name)
    if "facts" in payload:
        (person.PROFILE_DIR / "facts.md").write_text(payload["facts"].rstrip() + "\n", encoding="utf-8")
        written.append("facts.md")
    if "companies" in payload:
        groups: dict[str, list] = {}
        for c in payload["companies"]:
            entry = {k: v for k, v in c.items() if k in ("company", "ats_provider", "ats_slug", "career_url") and v}
            if entry.get("company") and entry.get("ats_provider"):
                groups.setdefault(c.get("group") or "companies", []).append(entry)
        cfg = {"_meta": {"description": f"Company job boards the nightly scraper checks (setup wizard, {stamp})."}, **groups}
        (person.PROFILE_DIR / "companies.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        written.append("companies.json")
    about = person.PROFILE_DIR / "about.md"
    if not about.exists():  # context for Claude Code sessions; theirs to extend
        about.write_text(_starter_about(), encoding="utf-8")
        written.append("about.md")
    saved = person.PROFILE_DIR / "saved_answers.json"
    if not saved.exists():
        saved.write_text('{"version": 1, "answers": []}\n', encoding="utf-8")
        written.append("saved_answers.json")
    return written


def _starter_about() -> str:
    a, srch = person.answers(), person.search()
    ident, auth, prefs = a.get("identity", {}), a.get("work_authorization", {}), a.get("preferences", {})
    edu = a.get("education", {})
    cities = ", ".join(prefs.get("preferred_locations", [])) or ", ".join(l["name"] for l in srch.get("locations", []))
    return f"""# About {person.full_name()}: context for Claude

Written by the setup wizard on {date.today().isoformat()}; add to it freely. CLAUDE.md imports this file.
Facts for letters live in facts.md, form answers in answers.toml, search rules in search.toml.

## Quick profile

- **Education:** {edu.get("degree", "")} in {edu.get("major", "")}, {edu.get("school", "")} ({person.month_year(str(edu.get("graduation", "")))})
- **Location:** {ident.get("location", "")}. Willing to relocate: {"yes" if prefs.get("willing_to_relocate") else "no"}.
- **Work authorization:** {auth.get("current_status", "")}; needs sponsorship: {"yes" if auth.get("requires_sponsorship") else "no"}.

## Target roles

{srch.get("target", {}).get("goal", "") or "(see search.toml)"}

Salary floor ${prefs.get("salary_floor", 0):,}, target ${prefs.get("salary_target", 0):,}. Locations in order: {cities or "(any)"}.

## Resume variants

- `{a.get("documents", {}).get("resume_pdf", "")}`: the one the bot uploads.

## Decisions (dated)

- {date.today().isoformat()}: set up jobbot.
"""


def save_resume(data: bytes, first_name: str = "", last_name: str = "") -> str:
    """Store an uploaded resume as resumes/<First_Last>_Resume.pdf; returns its path in the project."""
    if not data.startswith(b"%PDF"):
        raise ValueError("That isn't a PDF. Export your resume as PDF and upload that.")
    folder = PROJECT_DIR / "resumes"
    folder.mkdir(exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9]+", "_", f"{first_name} {last_name}").strip("_") or "My"
    name = stem + "_Resume.pdf"
    (folder / name).write_bytes(data)
    return f"resumes/{name}"
