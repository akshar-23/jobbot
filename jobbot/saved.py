"""Saved answers: your own answers to questions the rules and facts.md don't cover.

When jobbot can't answer a question (a non-compete, "have you been terminated", a company's
own wording of a standard question), you answer it once: in the browser extension ("Save my
answers") or on the dashboard's Answers tab. It's stored in profile/saved_answers.json and
reused by the bot and the extension whenever the same question comes up again.

These are your answers, not Claude's, so they may answer legal and background questions.
They never answer: questions about using AI, "have you applied here before", or required
agreements (those follow profile/bot.toml).

Matching is by the question's wording, normalized: case, punctuation and "required" markers
don't matter, and the company's name becomes "{company}" so "Have you worked for Unity?"
also matches "Have you worked for Adobe?". An answer that mentions the company it was given
to is kept for that company only.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date
from pathlib import Path

from jobbot import profile as person
from jobbot.apply.model import Answer, Question
from jobbot.paths import PROJECT_DIR

PATH = person.PROFILE_DIR / "saved_answers.json"
APPS_DIR = PROJECT_DIR / "applications"
LOCK = threading.Lock()

COMPANY_SUFFIX_RE = re.compile(
    r"[,.]?\s+(inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs|systems|group|holdings|"
    r"software|games|studios|international|incorporated)\.?$", re.I)
# Questions saved answers must never answer (see the module docstring).
NEVER_RE = re.compile(
    r"ai policy|use of ai|using ai|\bai[- ]generated|chatgpt|artificial intelligence"
    r"|applied (to|at|with|for) .* (before|previously)|previously applied"
    r"|arbitrat|i (agree|acknowledge|consent|certify|accept)|terms (and|&) conditions", re.I)


def never(label: str) -> bool:
    """AI-policy questions, "applied here before" and any agreement/consent: never saved or reused."""
    from jobbot.apply import fixed  # fixed imports this module
    return bool(NEVER_RE.search(label) or fixed.AI_POLICY_RE.search(label) or fixed.CONSENT_RE.search(label)
                or fixed.APPLIED_BEFORE_RE.search(label))


def company_names(company: str) -> list[str]:
    """'Unity Technologies Inc.' -> ['unity technologies inc.', 'unity technologies', 'unity']."""
    names, name = [], (company or "").strip().lower()
    while name and name not in names:
        names.append(name)
        name = COMPANY_SUFFIX_RE.sub("", name).strip()
    return [n for n in names if len(n) >= 2]


def question_key(label: str, company: str = "") -> str:
    text = (label or "").lower().replace("*", " ")
    text = re.sub(r"\((required|optional)\)", " ", text)
    for name in company_names(company):
        text = re.sub(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", " {company} ", text)
    text = re.sub(r"[^a-z0-9{}]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load() -> list[dict]:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("answers", []) if isinstance(data, dict) else []


def _write(items: list[dict]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "answers": items}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(PATH)


def _as_text(value) -> str:
    return " | ".join(value) if isinstance(value, list) else str(value or "").strip()


def lookup(q: Question, company: str) -> Answer | None:
    """Your saved answer for this question, fitted to its options; None if there isn't one."""
    if never(q.label or "") or q.kind in ("file", "hidden"):
        return None
    key = question_key(q.label, company)
    hits = [e for e in load() if e.get("key") == key
            and (not e.get("company") or e["company"].lower() == (company or "").lower())]
    if not hits:
        return None
    hits.sort(key=lambda e: e.get("saved", ""), reverse=True)   # newest first…
    hits.sort(key=lambda e: not e.get("company"))               # …and this company's own answer first
    entry = hits[0]
    value = _as_text(entry.get("answer"))
    if not value:
        return None
    source = "saved answer" + (f" (for {entry['company']})" if entry.get("company") else "")
    if not q.options or q.kind in ("text", "textarea", "date", "combobox", "location", "search"):
        return Answer(q.key, value, source)
    from jobbot.apply.fixed import match_option  # fixed imports this module
    parts = [match_option(q.options, p) for p in value.split(" | ")]
    if None in parts:
        return None  # the options changed; let the rules or you answer instead
    return Answer(q.key, " | ".join(parts), source)


def remember(label: str, value, *, kind: str = "text", options: list[str] | None = None,
             company: str = "", job: str = "", only_this_company: bool | None = None) -> tuple[bool, str]:
    """Save (or replace) your answer to a question. Returns (saved, why not)."""
    text = _as_text(value)
    if not label or not text:
        return False, "no answer given"
    if never(label):
        return False, "jobbot never reuses answers to AI-policy, applied-before or agreement questions"
    key = question_key(label, company)
    if not key:
        return False, "question has no words"
    if only_this_company is None:  # an answer naming the company only makes sense there
        only_this_company = any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", text.lower())
                                for n in company_names(company))
    scope = company if only_this_company else ""
    entry = {"question": label.strip(), "key": key, "answer": value if isinstance(value, list) else text,
             "kind": kind, "options": (options or [])[:40], "company": scope,
             "saved": date.today().isoformat(), "from": job[:120]}
    with LOCK:
        items = [e for e in load() if not (e.get("key") == key and e.get("company", "") == scope)]
        items.append(entry)
        items.sort(key=lambda e: e["key"])
        _write(items)
    return True, ""


def forget(key: str, company: str = "") -> bool:
    with LOCK:
        items = load()
        kept = [e for e in items if not (e.get("key") == key and e.get("company", "") == company)]
        if len(kept) == len(items):
            return False
        _write(kept)
    return True


def pending(limit: int = 60) -> list[dict]:
    """Questions jobbot recently left for you, most frequent first, minus ones you've answered.

    Read from the bot's plan.json blockers and the extension's assist.json "left" lists.
    """
    seen: dict[str, dict] = {}
    saved_keys = {(e.get("key"), e.get("company", "")) for e in load()}
    for folder in sorted(APPS_DIR.glob("*/"), reverse=True)[:300]:
        company, items = "", []
        try:
            if (folder / "plan.json").exists():
                plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
                company = plan.get("company", "")
                kinds = {q["key"]: (q.get("type", "text"), q.get("options", [])) for q in plan.get("questions", [])}
                items += [{"label": b["label"], "reason": b["reason"], "kind": kinds.get(b["key"], ("text", []))[0],
                           "options": kinds.get(b["key"], ("text", []))[1]}
                          for b in plan.get("blockers", []) if b.get("key")]
            if (folder / "assist.json").exists():
                cache = json.loads((folder / "assist.json").read_text(encoding="utf-8"))
                company = company or cache.get("company", "")
                items += list(cache.get("left", {}).values())
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for it in items:
            label = it.get("label", "")
            if not label or never(label):
                continue
            key = question_key(label, company)
            if (key, "") in saved_keys or (key, company) in saved_keys:
                continue
            row = seen.setdefault(key, {"key": key, "question": label, "kind": it.get("kind", "text"),
                                        "options": it.get("options", [])[:40], "reason": it.get("reason", ""),
                                        "companies": [], "count": 0})
            row["count"] += 1
            if company and company not in row["companies"]:
                row["companies"].append(company)
    out = sorted(seen.values(), key=lambda r: (-r["count"], r["question"]))
    return out[:limit]
