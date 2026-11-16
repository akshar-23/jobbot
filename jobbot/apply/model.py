"""Data passed between the planner, the drafter and the form handlers.

Every handler module (greenhouse, ashby, lever) provides:
    load(row) -> Plan                         read the job + questions, no browser
    fill(page, plan, state_name) -> [problems] fill the form, read required fields back
    submit(page) -> (ok, message)             click Submit, wait for a confirmation
"""

from __future__ import annotations

from dataclasses import dataclass, field


class PlanError(RuntimeError):
    """The job or its form couldn't be read."""


@dataclass
class Question:
    key: str                     # form field id, e.g. "first_name", "question_70265299"
    label: str
    kind: str                    # text | textarea | select | multiselect | boolean | date | file | location | search | hidden
    required: bool = False
    options: list[str] = field(default_factory=list)
    # "", "education", "eeo", "location", "resume" / "cover_letter" (file or text box that
    # takes the generated letter; never sent to the drafter as a question)
    section: str = ""


@dataclass
class Answer:
    key: str
    value: str | list[str]
    source: str                  # where it came from: "answers.toml: identity.email", "claude", ...


@dataclass
class Blocker:
    key: str
    label: str
    reason: str


@dataclass
class Plan:
    """Everything decided before a browser opens."""
    handler: str
    form_url: str
    company: str
    title: str
    job_text: str
    questions: list[Question]
    answers: dict[str, Answer] = field(default_factory=dict)
    blockers: list[Blocker] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cover_letter: str = ""
    cover_letter_pdf: str = ""
    resume_pdf: str = ""


# submit() returns (False, NEEDS_CODE) when the site emailed the applicant a code to confirm
# they're a person: the bot keeps the page open and waits for the code typed into the dashboard.
NEEDS_CODE = "needs_code"
