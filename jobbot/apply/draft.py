"""Cover letters and answers to custom questions, drafted by Claude from facts.md only.

Runs the Claude Code CLI headless (`claude -p`) under your own Claude login: no API
key and no extra cost. All tools are disabled, so Claude can only return text.
It runs from a scratch folder so this project's CLAUDE.md isn't loaded, and its
output must match a JSON schema.

Work authorization, sponsorship, salary, demographic and contact questions are
never sent here; jobbot/apply/fixed.py answers those from answers.toml.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from jobbot import profile as person
from jobbot.apply.model import Question
MAX_JOB_CHARS = 14_000
TIMEOUT_SECONDS = 300

# Words that must never appear in anything sent (NDA project names and the like):
# profile/bot.toml forbidden_terms. Drafts containing one go to Needs you.
def forbidden_list() -> list[str]:
    return [str(t).lower() for t in person.bot().get("forbidden_terms", []) if str(t).strip()]


SCHEMA = {
    "type": "object",
    "properties": {
        "cover_letter": {"type": "string"},
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "answer": {"type": "string"},
                    "confident": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["key", "answer", "confident"],
                "additionalProperties": False,
            },
        },
        "fit_warnings": {"type": "array", "items": {"type": "string"}},
        "dealbreakers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["cover_letter", "answers", "fit_warnings", "dealbreakers"],
    "additionalProperties": False,
}

SYSTEM_TEMPLATE = """You write job-application text for {name}: a cover letter and answers to
an application form's custom questions. Use ONLY the facts below. {first_name} will not
review your text before it is sent, so accuracy matters more than polish.

<facts>
{facts}
</facts>

<preferences>
Lives in: {location}. Willing to relocate: {relocate}. Open to remote: {remote}.
Earliest start: {start}.
</preferences>

Rules:
- Never invent skills, numbers, titles, dates or results. If the facts don't support
  an answer, return confident=false and an empty answer. Don't guess.
- Years of experience ("How many years of experience…", "Years of Python experience"):
  answer from "Experience in numbers" in the facts, picking the line that matches what
  the question counts (general, industry, full-time, research, or a skill). Numbers:
  the bold whole number. Ranges: the option that holds his months. confident=true.
- "Do you have N years of experience in X?": Yes only if the matching number in
  "Experience in numbers" is at least N. If it's lower, or the facts don't show that
  experience, the answer is No (confident=true) and add a fit_warnings entry: he can't
  claim experience the facts don't contain.
- Questions with options (including type "boolean", whose options are Yes/No):
  "answer" must be copied exactly from the options given. Multi-select: join the
  chosen options with " | ". Type "date": answer as MM/DD/YYYY.
- Free-text answers: plain text, no markdown, no greeting. 2-5 sentences unless the
  question asks for more. Specific beats smooth.
- Cover letter: follow the cover-letter rules in the facts (4 short paragraphs,
  200 words max, no greeting line). Tie paragraph 1 to something concrete in this
  posting.
- Follow every confidentiality rule in the facts (work under NDA, names and projects that
  must not appear).
- fit_warnings: short notes when the role asks for something he lacks (preferred
  skills, borderline experience). Empty list if none.
- dealbreakers: only hard stops, each one short. Stated minimum requirements he
  clearly doesn't meet (e.g. "requires 5+ years", a required PhD, a clearance), or
  {sponsor_rule}. Empty list
  if none. A dealbreaker means the application won't be sent automatically.
"""


def find_claude() -> str | None:
    """The Claude Code CLI: $JOBBOT_CLAUDE, `claude` on PATH, or the newest VS Code extension build."""
    if os.environ.get("JOBBOT_CLAUDE"):
        return os.environ["JOBBOT_CLAUDE"]
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    home = os.path.expanduser("~")
    for fixed_path in (os.path.join(home, ".local", "bin", "claude.exe"), os.path.join(home, ".local", "bin", "claude"),
                       os.path.join(home, ".claude", "local", "claude")):
        if os.path.isfile(fixed_path):
            return fixed_path
    hits = []
    for editor in (".vscode", ".vscode-insiders", ".cursor", ".windsurf"):
        for name in ("claude", "claude.exe"):
            hits += glob.glob(os.path.join(home, editor, "extensions", "anthropic.claude-code-*", "resources",
                                           "native-binary", name))

    def version(path: str) -> tuple[int, ...]:
        m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", path)
        return tuple(int(x) for x in m.groups()) if m else (0,)

    return max(hits, key=version) if hits else None


def _system_prompt(answers: dict) -> str:
    ident, prefs = answers.get("identity", {}), answers.get("preferences", {})
    auth = answers.get("work_authorization", {})
    sponsor_rule = (f"any sign the company won't sponsor visas or employ people on {auth.get('current_status') or 'a visa'}"
                    if auth.get("requires_sponsorship") else
                    "any sign the role needs a citizenship or clearance you don't have")
    return SYSTEM_TEMPLATE.format(
        name=person.full_name(), first_name=ident.get("first_name", "The candidate"), sponsor_rule=sponsor_rule,
        facts=person.path("facts.md").read_text(encoding="utf-8"),
        location=ident.get("location", ""),
        relocate="yes" if prefs.get("willing_to_relocate", True) else "no",
        remote="yes" if prefs.get("open_to_remote", True) else "no",
        start=prefs.get("earliest_start", ""),
    )


def _user_prompt(company: str, title: str, job_text: str, questions: list[Question]) -> str:
    qs = [{"key": q.key, "question": q.label, "type": q.kind, "required": q.required,
           **({"options": q.options} if q.options else {})} for q in questions]
    return (f"Company: {company}\nRole: {title}\n\n<job_description>\n{job_text[:MAX_JOB_CHARS]}\n"
            f"</job_description>\n\nWrite the cover letter, then answer each question below "
            f"(one entry per key):\n{json.dumps(qs, indent=1)}")


def forbidden_terms(text: str) -> list[str]:
    low = (text or "").lower()
    return [t for t in forbidden_list() if re.search(rf"\b{re.escape(t)}", low)]


class DraftError(RuntimeError):
    pass


def draft(company: str, title: str, job_text: str, questions: list[Question], answers: dict,
          effort: str = "medium") -> dict:
    """Returns {"cover_letter", "answers": {key: (answer, confident, note)}, "fit_warnings", "dealbreakers"}."""
    claude = find_claude()
    if not claude:
        raise DraftError("Claude Code CLI not found; set JOBBOT_CLAUDE to its path")
    cmd = [claude, "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
           "--effort", effort, "--json-schema", json.dumps(SCHEMA), "--system-prompt", _system_prompt(answers)]
    with tempfile.TemporaryDirectory(prefix="jobbot-draft-") as cwd:
        try:
            proc = subprocess.run(cmd, input=_user_prompt(company, title, job_text, questions), encoding="utf-8", errors="replace",
                                  capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=cwd)
        except subprocess.TimeoutExpired as e:
            raise DraftError("Claude took longer than 5 minutes") from e
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise DraftError(f"unexpected CLI output: {(proc.stderr or proc.stdout)[:200]}") from e
    out = data.get("structured_output")
    if data.get("is_error") or not isinstance(out, dict):
        raise DraftError(f"Claude returned no structured answer ({data.get('subtype')}: {str(data.get('result'))[:150]})")
    return {
        "cover_letter": out.get("cover_letter", "").strip(),
        "answers": {a["key"]: (a.get("answer", "").strip(), bool(a.get("confident")), a.get("note", ""))
                    for a in out.get("answers", [])},
        "fit_warnings": out.get("fit_warnings", []),
        "dealbreakers": out.get("dealbreakers", []),
    }
