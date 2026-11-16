"""Answers for the browser extension (extension/): the bot's rules and drafter, applied to
whatever application form is open in your own browser.

This is how Workday, custom portals and anything in Needs you get filled: you log in
and clicks through himself; the extension sends each page's fields here and fills in what
comes back. Same guardrails as the bot (jobbot/apply): fixed answers from answers.toml,
drafts from facts.md only, blockers for anything uncertain. It never clicks Submit.

Drafts are cached per job in applications/<date>_<Company>_<id>/assist.json, so moving
to the next page of a multi-page form doesn't wait on Claude for questions already answered.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from jobbot import ats, saved, tracker
from jobbot import profile as person
from jobbot.apply import draft as drafter
from jobbot.apply import fixed
from jobbot.apply.engine import AI_BAN_RE, APPS_DIR, HANDLERS, NO_SPONSOR_JD_RE, load_bot_config, one_word
from jobbot.apply.model import Answer, Blocker, Question
from jobbot.common import TRACKER_COLUMNS, job_identity
from jobbot.linkcheck import _company_config
from jobbot.paths import PROJECT_DIR
from jobbot.settings import load_answers

USER_AGENT = "jobbot/1.0 (personal job search)"
KINDS = {"text": "text", "email": "text", "tel": "text", "url": "text", "number": "text", "textarea": "textarea",
         "select": "select", "radio": "select", "listbox": "select", "checkbox": "multiselect", "date": "date",
         "file": "file", "combobox": "text", "buttons": "select"}
_TAG_RE = re.compile(r"<[^>]+>")


def _norm(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (label or "").lower()).strip()[:160]


def find_row(url: str) -> dict | None:
    """The tracker row for the job this page belongs to (Workday apply pages included)."""
    ident = job_identity(url)
    if not ident:
        return None
    for row in tracker.load_rows():
        if job_identity(row["jd_url"]) == ident:
            return row
    return None


def _workday_text(url: str) -> str:
    """Job description from Workday's public job-detail endpoint."""
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    if "job" not in segs:
        return ""
    i = segs.index("job")
    site = next((s for s in reversed(segs[:i]) if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)), "")
    if not site:
        _, wd_sites = _company_config()
        site = next((v for k, v in wd_sites.items() if k.lower().split()[0] in parts.netloc), "")
    if not site:
        return ""
    tenant = parts.netloc.split(".")[0]
    api = f"https://{parts.netloc}/wday/cxs/{tenant}/{site}/" + "/".join(segs[i:i + 3])
    req = urllib.request.Request(api, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        info = json.loads(resp.read()).get("jobPostingInfo") or {}
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", info.get("jobDescription", "")))).strip()


def _job_text(row: dict | None, url: str, page_text: str) -> str:
    try:
        source = row or {"jd_url": url, "company": "", "role_title": ""}
        kind = ats.provider(source["jd_url"])
        if kind in HANDLERS:
            return HANDLERS[kind].load(source).job_text or page_text
        if kind == "workday":
            return _workday_text(source["jd_url"]) or page_text
    except Exception:  # best effort: the page's own text is a fine fallback
        pass
    return page_text


def _folder(row: dict | None, url: str, company: str) -> Path:
    """Reuse the bot's folder for this job if it has one (its drafts and cover letter)."""
    jid = tracker.job_id(row) if row else hashlib.sha1((job_identity(url) or url).encode()).hexdigest()[:10]
    existing = sorted(APPS_DIR.glob(f"*_{jid}")) if APPS_DIR.exists() else []
    return existing[-1] if existing else APPS_DIR / f"{date.today()}_{one_word(company)}_{jid}"


def _load_cache(folder: Path) -> dict:
    cache = {"answers": {}, "cover_letter": "", "job_text": "", "company": "", "left": {}}
    try:
        cache.update(json.loads((folder / "assist.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if not cache["cover_letter"] and (folder / "cover_letter.txt").exists():
        cache["cover_letter"] = (folder / "cover_letter.txt").read_text(encoding="utf-8").strip()
    # The bot's dry run already drafted (and you reviewed) answers for this job: reuse them.
    try:
        report = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
        for q in report.get("questions", []):
            if q.get("answer") and str(q.get("source") or "").startswith("claude"):
                cache["answers"][_norm(q["label"])] = q["answer"]  # the reviewed one wins
    except (OSError, ValueError):
        pass
    return cache


def _save_cache(folder: Path, cache: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "assist.json").write_text(json.dumps(cache, indent=1), encoding="utf-8")
    if cache.get("cover_letter"):
        (folder / "cover_letter.txt").write_text(cache["cover_letter"] + "\n", encoding="utf-8")


def guess_company(url: str) -> str:
    """Company for a page that isn't in the tracker, from its link."""
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    kind = ats.provider(url)
    if kind in ("lever", "ashby") and segs:
        name = segs[0]
    elif kind == "greenhouse":
        q = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p)
        name = q.get("for") or (segs[0] if segs else "")
    elif kind == "workday":
        name = parts.netloc.split(".")[0]
    else:
        labels = [l for l in parts.netloc.split(".") if l not in ("www", "jobs", "careers", "apply", "boards")]
        name = labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")
    return re.sub(r"[-_]+", " ", name).strip().title()


def _context(payload: dict) -> tuple[dict | None, str, str, Path, dict]:
    url = payload.get("url", "")
    row = find_row(url)
    company = row["company"] if row else (payload.get("company") or "").strip() or guess_company(url)
    title = row["role_title"] if row else (payload.get("title") or "").strip()
    folder = _folder(row, url, company)
    cache = _load_cache(folder)
    if not cache["job_text"]:
        cache["job_text"] = _job_text(row, url, payload.get("page_text", ""))[:20000]
    return row, company, title, folder, cache


def plan(payload: dict) -> dict:
    """Answers for one page of fields. payload: {url, title, company, page_text, fields:[{key,label,kind,options,required}]}."""
    answers, cfg = load_answers(), load_bot_config()
    row, company, title, folder, cache = _context(payload)
    job_text = cache["job_text"]

    out: dict = {"job": {"company": company, "title": title, "tracked": bool(row),
                         "status": row["status"] if row else ""},
                 "answers": {}, "blockers": [], "warnings": [], "files": {}}
    if AI_BAN_RE.search(job_text):
        out["warnings"].append("This posting asks applicants not to use AI. Write the answers yourself.")
    no_sponsor = NO_SPONSOR_JD_RE.search(job_text)
    if no_sponsor:
        out["warnings"].append(f"The posting suggests no visa sponsorship: \"{no_sponsor.group(0)[:100]}\"")

    to_draft: list[Question] = []
    for f in payload.get("fields", []):
        kind = KINDS.get(f.get("kind", ""), "text")
        if f.get("kind") == "combobox" and f.get("options"):
            kind = "select"  # a searchable dropdown whose options the extension could read
        elif f.get("kind") == "combobox" and re.search(r"location|\bcity\b", f.get("label", ""), re.I):
            kind = "location"  # a city search box: needs a real city ("San Francisco"), not "Bay Area"
        q = Question(str(f["key"]), (f.get("label") or "").strip(), kind, bool(f.get("required")),
                     [o for o in f.get("options") or [] if o])
        if not q.label:
            continue
        if kind == "file":
            if re.search(r"auto-?fill|parse|import", q.label, re.I):
                continue  # "Autofill from resume" would refill (and wipe) the form after it's typed
            if re.search(r"resume|\bcv\b|curriculum", q.label, re.I):
                out["files"][q.key] = "resume"
            elif re.search(r"cover letter", q.label, re.I):
                out["files"][q.key] = "cover_letter"
            elif q.required:
                out["blockers"].append({"key": q.key, "label": q.label, "reason": "asks for a file other than your resume or cover letter"})
            continue
        if re.search(r"cover letter", q.label, re.I) and kind in ("text", "textarea"):
            out["answers"][q.key] = {"value": "__cover_letter__", "source": "drafted cover letter"}
            continue
        result = fixed.resolve(q, answers, company, cfg)
        if isinstance(result, Answer):
            if result.value:
                out["answers"][q.key] = {"value": result.value, "source": result.source}
        elif isinstance(result, Blocker):
            if q.required or AI_BAN_RE.search(q.label):
                out["blockers"].append({"key": q.key, "label": q.label, "reason": result.reason})
        elif AI_BAN_RE.search(job_text):
            if q.required:
                out["blockers"].append({"key": q.key, "label": q.label, "reason": "write this yourself (the posting restricts AI)"})
        elif _norm(q.label) in cache["answers"]:
            out["answers"][q.key] = {"value": cache["answers"][_norm(q.label)], "source": "claude (from facts.md, cached)"}
        else:
            to_draft.append(q)

    needs_letter = "__cover_letter__" in [a["value"] for a in out["answers"].values()] or "cover_letter" in out["files"].values()
    if (to_draft or (needs_letter and not cache["cover_letter"])) and not AI_BAN_RE.search(job_text):
        drafted = drafter.draft(company, title, job_text, to_draft, answers, effort=str(cfg.get("draft_effort", "medium")))
        cache["cover_letter"] = cache["cover_letter"] or drafted["cover_letter"]
        out["warnings"] += drafted["fit_warnings"]
        out["warnings"] += [f"Dealbreaker: {d}" for d in drafted["dealbreakers"]]
        for q in to_draft:
            text, confident, note = drafted["answers"].get(q.key, ("", False, "not answered"))
            if text and q.options and q.kind != "text":
                parts = [fixed.match_option(q.options, p) for p in text.split(" | ")]
                text = " | ".join(parts) if None not in parts else ""
            if text and confident and not drafter.forbidden_terms(text):
                out["answers"][q.key] = {"value": text, "source": "claude (from facts.md)"}
                cache["answers"][_norm(q.label)] = text
            elif q.required:
                out["blockers"].append({"key": q.key, "label": q.label,
                                        "reason": f"facts.md doesn't cover it ({note or 'Claude unsure'})"})
    if cache["cover_letter"] and drafter.forbidden_terms(cache["cover_letter"]):
        out["warnings"].append("The drafted cover letter mentions an off-limits term; it was not used.")
        cache["cover_letter"] = ""
    # A separate country-code field (Workday's "Country Phone Code") means the number goes in alone.
    if any(fixed.PHONE_CODE_RE.search(f.get("label", "")) for f in payload.get("fields", [])):
        for key, ans in out["answers"].items():
            if str(ans.get("source", "")).endswith("identity.phone"):
                ans["value"] = person.national_phone(str(ans["value"]))

    # What's left for you, so the dashboard's Answers tab can ask for it once.
    by_key = {str(f["key"]): f for f in payload.get("fields", [])}
    for b in out["blockers"]:
        f = by_key.get(b["key"])
        if f and b["label"]:
            cache["left"][_norm(b["label"])] = {"label": b["label"], "kind": KINDS.get(f.get("kind", ""), "text"),
                                                "options": (f.get("options") or [])[:40], "reason": b["reason"]}
    for key in out["answers"]:
        f = by_key.get(key)
        if f:
            cache["left"].pop(_norm(f.get("label", "")), None)
    cache["company"] = company
    _save_cache(folder, cache)
    out["cover_letter"] = cache["cover_letter"]
    out["files_available"] = {"resume": True, "cover_letter": (folder / person.file_name("cover_letter")).exists()}
    out["file_names"] = {"resume": person.file_name("resume"), "cover_letter": person.file_name("cover_letter")}
    return out


def remember(payload: dict) -> dict:
    """Save the answers you typed for questions jobbot left: {url, title, items:[{label, kind, options, value}]}."""
    row, company, title, folder, cache = _context(payload)
    done, skipped = 0, []
    for item in payload.get("items", []):
        label, value = (item.get("label") or "").strip(), item.get("value")
        ok, why = saved.remember(label, value, kind=KINDS.get(item.get("kind", ""), "text"),
                                 options=item.get("options") or [], company=company, job=f"{company} — {title}")
        if ok:
            done += 1
            cache["left"].pop(_norm(label), None)
        else:
            skipped.append({"label": label[:90], "why": why})
    _save_cache(folder, cache)
    return {"saved": done, "skipped": skipped}


def cover_letter(payload: dict) -> str:
    """The cover letter for this page's job, drafting it if needed."""
    answers, cfg = load_answers(), load_bot_config()
    row, company, title, folder, cache = _context(payload)
    if not cache["cover_letter"] and not AI_BAN_RE.search(cache["job_text"]):
        drafted = drafter.draft(company, title, cache["job_text"], [], answers, effort=str(cfg.get("draft_effort", "medium")))
        if not drafter.forbidden_terms(drafted["cover_letter"]):
            cache["cover_letter"] = drafted["cover_letter"]
    _save_cache(folder, cache)
    return cache["cover_letter"]


def file_for(kind: str, url: str) -> Path | None:
    if kind == "resume":
        path = PROJECT_DIR / load_answers().get("documents", {}).get("resume_pdf", "")
        return path if path.is_file() else None
    if kind == "cover_letter":
        row = find_row(url)
        folder = _folder(row, url, row["company"] if row else "Job")
        pdf = folder / person.file_name("cover_letter")
        return pdf if pdf.is_file() else None
    return None


def mark_applied(payload: dict) -> str:
    """Record an application sent by hand from the extension."""
    url, today = payload.get("url", ""), date.today().isoformat()
    with tracker.LOCK:
        rows = tracker.load_rows()
        ident = job_identity(url)
        for row in rows:
            if ident and job_identity(row["jd_url"]) == ident:
                row["status"], row["date_applied"] = "applied", row["date_applied"] or today
                row["notes"] = tracker.join_notes(row["notes"], f"applied via extension {today}")
                tracker.save_rows(rows)
                return f"Marked applied: {row['company']} — {row['role_title']}"
        new = {c: "" for c in TRACKER_COLUMNS}
        new.update(date_added=today, company=(payload.get("company") or "").strip() or guess_company(url),
                   role_title=(payload.get("title") or "").strip(), jd_url=url, sponsors_h1b="unclear",
                   status="applied", date_applied=today, notes=f"src:manual; applied via extension {today}")
        rows.append(new)
        tracker.save_rows(rows)
        return f"Added and marked applied: {new['company']} — {new['role_title'] or url}"
