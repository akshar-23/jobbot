"""Answer, on the dashboard, what stopped the bot, and let it run that application again.

Many applications land in Needs you over one decision: accept an agreement, say whether
you're bound by a non-compete, apply despite a fit warning. Instead of opening the site, you
answer on the dashboard; the answers go in that job's applications/ folder (overrides.json)
and the bot uses them on its next run of the job (engine.plan_job).

These are your decisions for one application. "Remember" also saves an answer for every
future form (saved_answers.json), except agreements, AI-use questions and "applied here
before", which are always decided per application.

Not answerable here: logins and portals (Workday), bot checks, uploads other than your
resume, and postings that ask applicants not to use AI.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from jobbot import saved, tracker
from jobbot.paths import PROJECT_DIR

APPS_DIR = PROJECT_DIR / "applications"


def _latest(jid: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", jid or ""):
        return None
    found = [f for f in sorted(APPS_DIR.glob(f"*_{jid}"), reverse=True) if (f / "plan.json").exists()]
    return found[0] if found else None


def _category(b: dict) -> str:
    reason, label = b.get("reason", "").lower(), b.get("label", "").lower()
    if not b.get("key"):
        if label == "fit":
            return "fit"
        if "sponsorship" in reason:
            return "sponsor_hint"
        return "fixed"          # AI ban in the posting, banned words in a draft: do it by hand
    if "agreement" in reason or "privacy notice" in reason:
        return "agreement"
    if "using ai" in reason or "about using ai" in reason:
        return "ai_policy"
    if "applied here before" in reason:
        return "applied_before"
    if "file other than" in reason:
        return "fixed"
    return "question"


def items(jid: str) -> dict | None:
    """What stopped the bot on this job's latest run, with what's needed to answer each."""
    folder = _latest(jid)
    if not folder:
        return None
    plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    questions = {q["key"]: q for q in plan.get("questions", [])}
    out = []
    for b in plan.get("blockers", []):
        q = questions.get(b.get("key"), {})
        cat = _category(b)
        out.append({"key": b.get("key", ""), "label": b.get("label", ""), "reason": b.get("reason", ""),
                    "kind": q.get("type", "text"), "options": q.get("options", []), "category": cat,
                    "answerable": cat != "fixed", "can_remember": cat == "question"})
    return {"folder": folder.name, "company": plan.get("company", ""), "title": plan.get("title", ""),
            "form": plan.get("form", ""), "items": out}


def answerable_count(jid: str) -> int:
    info = items(jid)
    if not info or not info["items"]:
        return 0
    return len(info["items"]) if all(i["answerable"] for i in info["items"]) else 0


def overrides(jid: str) -> dict:
    """Your dashboard answers for this job (newest overrides.json among its folders)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", jid or ""):
        return {}
    for folder in sorted(APPS_DIR.glob(f"*_{jid}"), reverse=True):
        p = folder / "overrides.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                return {}
    return {}


def apply(jid: str, payload: dict) -> str:
    """Store your answers, remember the ones you ticked, and put the job back in To apply."""
    info = items(jid)
    if not info:
        raise ValueError("No bot run found for this job.")
    by_key = {i["key"]: i for i in info["items"] if i["key"]}
    answers = {k: v for k, v in (payload.get("answers") or {}).items() if k in by_key and v not in ("", None, [])}
    data = overrides(jid)
    data.setdefault("answers", {}).update(answers)
    if payload.get("apply_anyway"):
        data["accept_dealbreakers"] = True
        data["accept_sponsor_hint"] = True
    data["date"] = date.today().isoformat()
    folder = APPS_DIR / info["folder"]
    (folder / "overrides.json").write_text(json.dumps(data, indent=1), encoding="utf-8")

    remembered = 0
    for key in payload.get("remember") or []:
        item = by_key.get(key)
        if item and item["can_remember"] and key in answers:
            ok, _ = saved.remember(item["label"], answers[key], kind=item["kind"], options=item["options"],
                                   company=info["company"])
            remembered += ok

    with tracker.LOCK:
        rows = tracker.load_rows()
        for row in rows:
            if tracker.job_id(row) == jid:
                row["status"] = "to_apply"
                kept = [p.strip() for p in row["notes"].split(";") if p.strip() and not p.strip().startswith("bot:")]
                note = f"bot: you answered {len(answers)} question(s) on the dashboard {date.today()}; running again"
                row["notes"] = tracker.join_notes("; ".join(kept), note)
        tracker.save_rows(rows)
    return f"Saved {len(answers)} answer(s)" + (f", {remembered} remembered for future forms" if remembered else "") + "."
