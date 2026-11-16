"""Claude helpers for the setup wizard, run on the user's own Claude login (`claude -p`).

- read_resume: the resume PDF -> a draft facts.md plus contact / education / job fields
- suggest_search: facts + what they want -> title rules, levels, locations, fit-check guide
- suggest_companies: facts + what they want -> employers, then their public job boards
  (jobbot/boards.py)

Everything comes back as a draft in the wizard; nothing is saved until the user clicks Save.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from jobbot import boards
from jobbot import profile as person
from jobbot.apply.draft import find_claude
from jobbot.paths import PROJECT_DIR

TIMEOUT = 600
EXAMPLE_FACTS = PROJECT_DIR / "profile.example" / "facts.md"
EXAMPLE_SEARCH = PROJECT_DIR / "profile.example" / "search.toml"


class SetupAIError(RuntimeError):
    pass


def _claude(prompt: str, schema: dict, *, files: dict[str, Path] | None = None, system: str = "") -> dict:
    claude = find_claude()
    if not claude:
        raise SetupAIError("Claude Code isn't installed or wasn't found. Install it and sign in, then try again.")
    cmd = [claude, "-p", "--output-format", "json", "--no-session-persistence", "--model", "sonnet",
           "--effort", "medium", "--json-schema", json.dumps(schema)]
    cmd += ["--tools", "Read", "--allowedTools", "Read"] if files else ["--tools", ""]
    if system:
        cmd += ["--system-prompt", system]
    with tempfile.TemporaryDirectory(prefix="jobbot-setup-") as cwd:
        for name, src in (files or {}).items():
            shutil.copyfile(src, Path(cwd) / name)
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                  timeout=TIMEOUT, cwd=cwd)
        except subprocess.TimeoutExpired as e:
            raise SetupAIError("Claude took longer than 10 minutes") from e
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise SetupAIError(f"unexpected output from Claude Code: {(proc.stderr or proc.stdout)[:200]}") from e
    out = data.get("structured_output")
    if data.get("is_error") or not isinstance(out, dict):
        raise SetupAIError(f"Claude didn't return an answer ({data.get('subtype')}: {str(data.get('result'))[:200]})")
    return out


_STR = {"type": "string"}


def _obj(*keys: str) -> dict:
    return {"type": "object", "properties": {k: _STR for k in keys}}


RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        "facts_md": _STR,
        "identity": _obj("first_name", "last_name", "email", "phone", "city", "state", "postal_code"),
        "links": _obj("linkedin", "github", "portfolio", "reel", "artstation"),
        "education": _obj("school", "degree", "major", "graduation", "gpa",
                          "undergrad_school", "undergrad_degree", "undergrad_major", "undergrad_graduation"),
        "current_job": _obj("company", "title", "start_date"),
    },
    "required": ["facts_md", "identity", "links", "education", "current_job"],
}


def read_resume(pdf: Path) -> dict:
    """A draft facts.md and form fields from the resume PDF."""
    example = EXAMPLE_FACTS.read_text(encoding="utf-8") if EXAMPLE_FACTS.exists() else ""
    prompt = f"""Read resume.pdf (use the Read tool) and draft two things for a job-application assistant.

1. facts_md: the person's facts file, in the same format and sections as the example below
   (rules, cover letters, short answers, summary, education, experience, "Experience in
   numbers", projects, skills, links, work authorization). Rules:
   - Use only what the resume says. Never add a skill, number, title or date it doesn't show.
   - Keep the example's generic rules and the cover-letter / short-answer format; replace
     everything about the example person (name, field, wording about their work).
   - "Experience in numbers": compute years from the resume's dates as of {date.today():%B %Y},
     rounded down, for general / industry / full-time and the main tools or skills.
   - Work authorization section: write "TODO: fill in" (the resume rarely says).
2. The form fields: contact details, links, education (dates as YYYY-MM, degree written out
   like "Bachelor of Fine Arts"), current job. Empty string for anything the resume doesn't show.
   state = the two-letter US state code.

<example_facts_md>
{example}
</example_facts_md>"""
    return _claude(prompt, RESUME_SCHEMA, files={"resume.pdf": pdf})


SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "include": {"type": "array", "items": _STR},
        "not_target": {"type": "array", "items": _STR},
        "outside_background": {"type": "array", "items": _STR},
        "other_tracks": {"type": "array", "items": _STR},
        "level_nouns": {"type": "array", "items": _STR},
        "max_level": {"type": "integer"},
        "max_years_required": {"type": "integer"},
        "drop_contract_roles": {"type": "boolean"},
        "drop_apprenticeships": {"type": "boolean"},
        "simplify_new_grad": {"type": "boolean"},
        "simplify_categories": {"type": "array", "items": _STR},
        "locations": {"type": "array", "items": {"type": "object", "properties": {"name": _STR, "match": _STR},
                                                  "required": ["name", "match"]}},
        "fit_guide": _STR,
    },
    "required": ["include", "not_target", "outside_background", "other_tracks", "level_nouns", "max_level",
                 "max_years_required", "drop_contract_roles", "drop_apprenticeships", "simplify_new_grad",
                 "simplify_categories", "locations", "fit_guide"],
}


def suggest_search(facts: str, goal: str, cities: list[str], needs_sponsorship: bool) -> dict:
    """Title rules, level settings, location patterns and the fit-check guide."""
    example = EXAMPLE_SEARCH.read_text(encoding="utf-8") if EXAMPLE_SEARCH.exists() else ""
    prompt = f"""Set up job-search filters for this person. They filter job TITLES from company job
boards (US jobs only), then Claude judges each remaining posting with the fit guide.

What they want, in their words: {goal or "(not given: infer from the facts)"}
Cities, best first: {", ".join(cities) or "(none given)"}
Needs visa sponsorship: {"yes" if needs_sponsorship else "no"}

Return:
- include: title words/phrases that mark a job they'd want (a title must contain one). Python
  regular expressions, matched as whole words ignoring case: lowercase, use \\s+ between words,
  \\w* for word endings. 8-20 entries, broad enough to catch real titles at studios/companies.
- not_target: titles of other professions that would otherwise slip through include.
- outside_background: specialist areas of the same field they lack the background for.
- other_tracks: anything else to drop (may be empty).
- level_nouns: role nouns a level number follows ("animator" in "Animator II").
- max_level: 1 for new grads unless their facts justify 2. max_years_required: years they can
  credibly claim (from "Experience in numbers"), usually 1-3.
- drop_contract_roles / drop_apprenticeships: false where those are normal first jobs in the field.
- simplify_new_grad: true only for software / data / ML roles (the list only has tech jobs);
  simplify_categories: from "Software", "Software Engineering", "AI/ML/Data", "Data Science", "Hardware".
- locations: one per city, in their order: name, and match = a lowercase regex of the city and
  its nearby cities/neighborhoods, | separated (as in the example).
- fit_guide: plain-language guidance in the example's format (what they're looking for; good /
  stretch / poor verdict rules using {{years}} and {{years_plus}}; reason examples), third person,
  specific to their field and facts. Mention sponsorship only if they need it.

<their_facts>
{facts}
</their_facts>

<example_search_toml (a new-grad animator)>
{example}
</example_search_toml>"""
    return _claude(prompt, SEARCH_SCHEMA)


COMPANIES_SCHEMA = {
    "type": "object",
    "properties": {"companies": {"type": "array", "items": {
        "type": "object", "properties": {"name": _STR, "why": _STR}, "required": ["name", "why"]}}},
    "required": ["companies"],
}


def suggest_companies(facts: str, goal: str, cities: list[str], needs_sponsorship: bool, count: int = 30) -> list[dict]:
    """Employers worth watching, each with the public job board found for it (if any)."""
    prompt = f"""List {count} US employers this person should watch for openings, most promising first.
What they want: {goal or "(infer from the facts)"}
Preferred cities: {", ".join(cities) or "anywhere in the US"}
{"They need H-1B sponsorship: favor employers known to sponsor." if needs_sponsorship else ""}
Mix large employers with mid-size and smaller studios/companies that hire juniors. Use each
company's usual name (e.g. "Epic Games", not "Epic Games, Inc."). why: under 12 words.

<their_facts>
{facts}
</their_facts>"""
    names = _claude(prompt, COMPANIES_SCHEMA).get("companies", [])
    found = boards.probe_many([c["name"] for c in names if c.get("name")])
    out = []
    for c in names:
        hits = found.get(c.get("name", ""), [])
        out.append({"company": c.get("name", ""), "why": c.get("why", ""), "boards": hits})
    return out
