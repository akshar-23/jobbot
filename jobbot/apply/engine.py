"""Run the application bot over the rows you moved to to_apply.

Per row:
  1. plan       read the job and its form questions (no browser yet)
  2. answer     fixed answers from answers.toml; Claude drafts the rest from facts.md
  3. blockers?  anything uncertain -> status needs_manual, with the reasons and drafts saved
  4. fill       open the form, fill it, read every required field back, screenshot
  5. submit     only if this handler is in bot.toml live_handlers, this isn't a dry
                run, and the daily / per-company caps allow it

Everything for a job is saved under applications/<date>_<Company>_<id>/
(gitignored): job.txt, plan.json, cover letter (.txt + .pdf), screenshots.
"""

from __future__ import annotations

import html
import json
import re
import time

import urllib.error
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jobbot import ats, rank, tracker
from jobbot import profile as person
from jobbot.apply import draft as drafter
from jobbot.apply import ashby, fixed, greenhouse, lever
from jobbot import codes, resolve
from jobbot.apply.model import NEEDS_CODE, Answer, Blocker, Plan, PlanError
from jobbot.paths import PROJECT_DIR
from jobbot.settings import load_answers

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

APPS_DIR = PROJECT_DIR / "applications"
COVER_DIR = PROJECT_DIR / "cover_letters"
HANDLERS = {"greenhouse": greenhouse, "ashby": ashby, "lever": lever}
BOT_DEFAULTS = {"live_handlers": [], "daily_limit": 50, "per_company_per_day": 2,
                "accept_privacy_notices": False, "accept_arbitration_agreements": False,
                "accept_other_agreements": False, "headless": True, "draft_effort": "medium",
                "fit_on_serve": True, "fit_model": "sonnet", "fit_effort": "medium", "auto_run_on_pick": False,
                "solve_checks_myself": False}

CODE_WAIT_MINUTES = 15  # how long an application waits for a security code or a solved CAPTCHA
CONFIRMED_RE = re.compile(r"thank you for (applying|your (application|interest))|thanks for applying"
                          r"|application (has been |was )?(successfully )?(submitted|received)", re.I)


def confirmed(page, before: str = "") -> bool:
    """Has the site's confirmation shown up since `before` (the page when it was handed to you)?"""
    now = page.inner_text("body")
    return any(s in page.url for s in ("/thanks", "confirmation")) or \
        len(CONFIRMED_RE.findall(now)) > len(CONFIRMED_RE.findall(before))

AI_BAN_RE = re.compile(
    r"(do not|don.t|please refrain from|refrain from|must not|not permitted to|please avoid)\s+(use|using)\s+"
    r"(of\s+)?(an?\s+)?(ai|artificial intelligence|chatgpt|generative ai|llms?|ai assistants?)\b"
    r"|without (the )?(use|help|assistance) of (ai|artificial intelligence|ai assistants?)", re.I)

# Postings that say (or hedge) they won't sponsor. Sponsorship is a hard requirement.
NO_SPONSOR_JD_RE = re.compile(
    r"(unable|not able|may not be able|cannot|can.t|will not|won.t|do not|does not|is not able)\s+(to\s+)?"
    r"(sponsor|support|provide|offer|employ|hire)[^.]{0,80}(visa|sponsorship|h-?1b|work authori)"
    r"|no (visa )?sponsorship|sponsorship (is )?not (available|offered|provided)"
    r"|must be (legally )?authori[sz]ed to work[^.]{0,40}without[^.]{0,30}sponsorship", re.I)


@dataclass
class Outcome:
    job_id: str
    company: str
    title: str
    result: str          # applied | dry_run_ok | needs_manual | error | capped | no_handler
    detail: str = ""
    folder: str = ""


def load_bot_config() -> dict:
    """profile/bot.toml over the defaults (read through jobbot/profile.py, like every profile file)."""
    return {**BOT_DEFAULTS, **person.bot()}


def todo_fields(node, prefix: str = "") -> list[str]:
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in todo_fields(v, f"{prefix}{k}.")]
    return [prefix.rstrip(".")] if node == "TODO" else []


def one_word(text: str, limit: int = 40) -> str:
    """'Software Engineer, Fraud Systems' -> 'SoftwareEngineer'; 'Scale AI' -> 'ScaleAI'."""
    head = re.split(r"[,(/|–—-]", text or "", maxsplit=1)[0]
    words = re.findall(r"[A-Za-z0-9]+", head)
    return "".join(w if w.isupper() else w[:1].upper() + w[1:] for w in words)[:limit] or "Role"


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def reviewed_draft(jid: str) -> dict | None:
    """The cover letter and Claude-written answers from this job's latest clean dry run.

    When the bot later submits for real, it sends these (what you saw in the screenshot)
    instead of drafting again. None if there's no clean dry run to reuse.
    """
    for folder in sorted(APPS_DIR.glob(f"*_{jid}"), reverse=True):
        try:
            report = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if report.get("blockers") or report.get("fill_problems") or not (folder / "filled.png").exists():
            return None
        letter = folder / "cover_letter.txt"
        return {"folder": folder.name,
                "cover_letter": letter.read_text(encoding="utf-8").strip() if letter.exists() else "",
                "answers": {q["key"]: q["answer"] for q in report.get("questions", [])
                            if q.get("answer") and str(q.get("source") or "").startswith("claude")},
                "warnings": report.get("warnings", [])}
    return None


def plan_job(row: dict, answers: dict, cfg: dict, reuse: dict | None = None) -> Plan:
    handler = HANDLERS[ats.provider(row["jd_url"])]
    plan = handler.load(row)
    plan.resume_pdf = str(PROJECT_DIR / answers["documents"]["resume_pdf"])

    ai_ban = bool(AI_BAN_RE.search(plan.job_text))
    if ai_ban:
        plan.blockers.append(Blocker("", "job description",
                                     "the posting asks applicants not to use AI; write this application yourself"))
    mine = resolve.overrides(tracker.job_id(row))  # what you answered on the dashboard for this job
    no_sponsor = NO_SPONSOR_JD_RE.search(plan.job_text)
    if no_sponsor and mine.get("accept_sponsor_hint"):
        plan.warnings.append(f"you chose to apply although the posting suggests no sponsorship: \"{no_sponsor.group(0)[:100]}\"")
    elif no_sponsor:
        plan.blockers.append(Blocker("", "job description",
                                     f"the posting suggests no visa sponsorship: \"{no_sponsor.group(0)[:120]}\""))

    to_draft = []
    for q in plan.questions:
        if q.kind == "file" and q.required and q.section not in ("resume", "cover_letter"):
            plan.blockers.append(Blocker(q.key, q.label, "asks for a file other than your resume or cover letter"))
        if q.kind in ("file", "hidden") or q.section == "cover_letter":
            continue  # files are attached by the handler; cover-letter boxes get the drafted letter
        if q.key in mine.get("answers", {}):
            plan.answers[q.key] = Answer(q.key, mine["answers"][q.key], "you, on the dashboard")
            continue
        result = fixed.resolve(q, answers, plan.company, cfg)
        if isinstance(result, Answer):
            plan.answers[q.key] = result
        elif isinstance(result, Blocker):
            if q.required:
                plan.blockers.append(result)
        else:
            to_draft.append(q)

    if ai_ban:
        return plan
    # Reuse what was reviewed in the dry run; draft only what it didn't cover.
    kept = (reuse or {}).get("answers", {})
    fresh = [q for q in to_draft if q.key not in kept]
    if fresh or not (reuse or {}).get("cover_letter"):
        drafted = drafter.draft(plan.company, plan.title, plan.job_text, fresh, answers,
                                effort=str(cfg.get("draft_effort", "medium")))
    else:
        drafted = {"cover_letter": "", "answers": {}, "fit_warnings": [], "dealbreakers": []}
    for q in to_draft:
        if q.key in kept:
            drafted["answers"][q.key] = (kept[q.key], True, "reviewed in the dry run")
    plan.cover_letter = (reuse or {}).get("cover_letter") or drafted["cover_letter"]
    plan.warnings += drafted["fit_warnings"] or (reuse or {}).get("warnings", [])
    for reason in drafted["dealbreakers"]:
        if mine.get("accept_dealbreakers"):
            plan.warnings.append(f"you chose to apply anyway: {reason}")
        else:
            plan.blockers.append(Blocker("", "fit", reason))
    for q in to_draft:
        text, confident, note = drafted["answers"].get(q.key, ("", False, "not answered"))
        if text and q.options:
            parts = [fixed.match_option(q.options, p) for p in text.split(" | ")]
            text = " | ".join(parts) if None not in parts else ""
            note = note if text else "answer wasn't one of the options"
        if text and confident:
            plan.answers[q.key] = Answer(q.key, text, "claude (from facts.md)")
        elif q.required:
            plan.blockers.append(Blocker(q.key, q.label, f"facts.md doesn't cover it ({note or 'Claude unsure'})"))

    drafted_text = plan.cover_letter + " " + " ".join(
        str(a.value) for a in plan.answers.values() if a.source.startswith("claude"))
    banned = drafter.forbidden_terms(drafted_text)
    if banned:
        plan.blockers.append(Blocker("", "drafted text", f"mentions terms that are off-limits: {banned}"))
    if len(plan.cover_letter.split()) > 230:
        plan.warnings.append(f"cover letter is {len(plan.cover_letter.split())} words (limit 200)")
    for q in plan.questions:
        if q.section == "cover_letter" and q.kind in ("text", "textarea") and plan.cover_letter:
            plan.answers[q.key] = Answer(q.key, plan.cover_letter, "drafted cover letter")
    return plan


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def save_plan(plan: Plan, row: dict, folder: Path, problems: list[str] | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "job.txt").write_text(f"{plan.company} — {plan.title}\n{row['jd_url']}\n\n{plan.job_text}\n",
                                    encoding="utf-8")
    report = {
        "company": plan.company, "title": plan.title, "form": plan.form_url,
        "blockers": [vars(b) for b in plan.blockers], "fill_problems": problems or [],
        "warnings": plan.warnings,
        "questions": [{"key": q.key, "label": q.label, "type": q.kind, "required": q.required, "options": q.options[:40],
                       "answer": (plan.answers[q.key].value if q.key in plan.answers else None),
                       "source": (plan.answers[q.key].source if q.key in plan.answers else None)}
                      for q in plan.questions],
    }
    (folder / "plan.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if plan.cover_letter:
        (folder / "cover_letter.txt").write_text(plan.cover_letter + "\n", encoding="utf-8")


def render_cover_letter_pdf(pdf_browser, text: str, answers: dict, out: Path) -> None:
    ident = answers.get("identity", {})
    contact = " · ".join(filter(None, [ident.get("location"), ident.get("email"), ident.get("phone")]))
    paragraphs = "".join(f"<p>{html.escape(p.strip())}</p>" for p in text.split("\n\n") if p.strip())
    page = pdf_browser.new_page()
    page.set_content(f"""<html><head><style>
      body {{ font: 11pt/1.5 Georgia, 'Times New Roman', serif; color: #111; margin: 0; }}
      h1 {{ font-size: 16pt; margin: 0 0 2px; }} .c {{ color: #444; font-size: 10pt; margin-bottom: 22px; }}
      p {{ margin: 0 0 12px; }}</style></head>
      <body><h1>{html.escape(ident.get('first_name', '') + ' ' + ident.get('last_name', ''))}</h1>
      <div class="c">{html.escape(contact)}</div>{paragraphs}</body></html>""")
    page.pdf(path=str(out), format="Letter", margin={"top": "0.9in", "bottom": "0.9in", "left": "1in", "right": "1in"})
    page.close()


def keep_cover_letter(plan: Plan, today: date) -> str:
    """Copy the submitted letter to cover_letters/ with the project's naming convention."""
    COVER_DIR.mkdir(exist_ok=True)
    stem = f"{one_word(plan.title)}_{one_word(plan.company)}_{today:%m-%d}"
    name, n = f"{stem}.txt", 2
    while (COVER_DIR / name).exists():
        name, n = f"{stem}_v{n}.txt", n + 1
    (COVER_DIR / name).write_text(plan.cover_letter + "\n", encoding="utf-8")
    return name


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

def record(jid: str, *, status: str | None = None, note: str = "", applied_on: str = "",
           cover_letter: str = "", resume_variant: str = "") -> None:
    """Update one row. Earlier 'bot:' notes are replaced, so the latest run's note is the one shown."""
    with tracker.LOCK:
        rows = tracker.load_rows()
        for row in rows:
            if tracker.job_id(row) != jid:
                continue
            if status:
                row["status"] = status
            if applied_on:
                row["date_applied"] = applied_on
            if resume_variant:
                row["resume_variant"] = resume_variant
            kept = [p.strip() for p in row["notes"].split(";") if p.strip() and not p.strip().startswith("bot:")]
            extra = [note] + ([f"cover_letter: {cover_letter}"] if cover_letter else [])
            row["notes"] = tracker.join_notes("; ".join(kept), "; ".join(e for e in extra if e))
            break
        tracker.save_rows(rows)


def submitted_today(company: str, today: str) -> tuple[int, int]:
    rows = [r for r in tracker.load_rows() if r["date_applied"] == today and "bot: applied" in r["notes"]]
    return len(rows), sum(r["company"].lower() == company.lower() for r in rows)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(*, dry_run: bool = False, limit: int | None = None, job_ids: set[str] | None = None,
        headed: bool | None = None, log=print) -> list[Outcome]:
    from playwright.sync_api import sync_playwright  # only the bot needs the venv

    cfg, answers = load_bot_config(), load_answers()
    missing = todo_fields(answers)
    if missing:
        raise SystemExit(f"Fill these in profile/answers.toml first: {', '.join(missing)}")
    resume = PROJECT_DIR / answers.get("documents", {}).get("resume_pdf", "")
    if not resume.is_file():
        raise SystemExit(f"Resume not found: {resume}")
    if not drafter.find_claude():
        raise SystemExit("Claude Code CLI not found. Set JOBBOT_CLAUDE to its path.")

    today = date.today()
    rows = {tracker.job_id(r): r for r in tracker.load_rows()}
    order = [v["id"] for v in rank.ranked(list(rows.values()), {"to_apply"})]
    if job_ids:
        order = [i for i in order if i in job_ids]
    state_name = person.state_name(answers.get("identity", {}).get("state", ""))
    headless = cfg["headless"] if headed is None else not headed
    # To solve a CAPTCHA yourself the browser has to be on screen: live runs open a visible window.
    solve_checks = bool(cfg.get("solve_checks_myself")) and not dry_run and bool(cfg["live_handlers"])
    if solve_checks:
        headless = False
    outcomes: list[Outcome] = []

    waiting: list[dict] = []  # submitted, waiting for a code typed into the dashboard

    def finish(item: dict, ok: bool, message: str) -> None:
        """Record a live submission's outcome and close its page."""
        jid, plan, folder, rel = item["jid"], item["plan"], item["folder"], item["rel"]
        try:
            item["page"].screenshot(path=str(folder / "after_submit.png"), full_page=True)
        except Exception:  # the page may have closed itself
            pass
        if ok:
            letter = keep_cover_letter(plan, today) if plan.cover_letter else ""
            record(jid, status="applied", applied_on=today.isoformat(),
                   resume_variant=str(cfg.get("resume_variant", "default")),
                   note=f"bot: applied {today} → {rel}", cover_letter=letter)
            outcomes.append(Outcome(jid, plan.company, plan.title, "applied", message, rel))
            log(f"  applied     {plan.company} — {message}")
        else:
            record(jid, status="needs_manual",
                   note=f"bot: finish in your browser {today}: {message}. Open the job, press Alt+J "
                        f"(the reviewed answers are ready), click Submit → {rel}")
            outcomes.append(Outcome(jid, plan.company, plan.title, "needs_manual", message, rel))
            log(f"  needs you   {plan.company}: {message}")
        codes.clear(jid)
        item["context"].close()

    def serve_codes(wait: bool) -> None:
        """Type codes entered on the dashboard; with wait, keep going until none are left."""
        while waiting:
            for item in list(waiting):
                if item.get("kind") == "check":
                    try:
                        done = confirmed(item["page"], item.get("baseline", ""))
                    except Exception:  # you closed the window
                        waiting.remove(item)
                        finish(item, False, "the jobbot window was closed before the check was solved")
                        continue
                    if done:
                        waiting.remove(item)
                        finish(item, True, "confirmation shown after you solved the check")
                    elif time.time() > item["until"]:
                        waiting.remove(item)
                        finish(item, False, f"the check wasn't solved within {CODE_WAIT_MINUTES} minutes")
                    continue
                code = codes.take(item["jid"])
                if code:
                    ok, message = item["handler"].enter_code(item["page"], code)
                    if not ok and message.startswith("wrong code") and item["tries"] < 2:
                        item["tries"] += 1
                        codes.update(item["jid"], status="wrong", message=f"{message}. Check the newest email and try again.")
                        log(f"  wrong code  {item['plan'].company}: asked for it again on the dashboard")
                        continue
                    waiting.remove(item)
                    finish(item, ok, message)
                elif time.time() > item["until"]:
                    waiting.remove(item)
                    finish(item, False, f"no code entered within {CODE_WAIT_MINUTES} minutes")
            if not wait or not waiting:
                return
            time.sleep(2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        pdf_browser = browser if headless else p.chromium.launch(headless=True)
        handled = 0
        for jid in order:
            serve_codes(wait=False)
            row = rows[jid]
            kind = ats.provider(row["jd_url"])
            if kind not in HANDLERS:
                site = {"workday": "Workday", "": "this site"}.get(kind, kind)
                record(jid, status="needs_manual",
                       note=f"bot: {site} form → open it in your browser and press Alt+J (jobbot helper)")
                outcomes.append(Outcome(jid, row["company"], row["role_title"], "no_handler", kind))
                log(f"  needs you   {row['company']} — {row['role_title'][:50]} (no {kind} handler yet)")
                continue
            live = not dry_run and kind in cfg["live_handlers"]
            if not live and not job_ids and "bot: dry run ok" in row["notes"]:
                continue  # already dry-run; approve the handler (or pick it again) to go further
            if limit and handled >= limit:
                break
            handled += 1
            handler = HANDLERS[kind]
            live = not dry_run and kind in cfg["live_handlers"]
            log(f"• {row['company']} — {row['role_title'][:60]}")

            reuse = reviewed_draft(jid) if live else None
            if reuse:
                log(f"  submitting the letter and answers from the dry run in {reuse['folder']}")
            try:
                plan = plan_job(row, answers, cfg, reuse)
            except (PlanError, drafter.DraftError, urllib.error.URLError, ValueError, KeyError) as e:
                record(jid, note=f"bot: couldn't plan {today} ({e})")
                outcomes.append(Outcome(jid, row["company"], row["role_title"], "error", str(e)))
                log(f"  error       {e}")
                continue

            folder = APPS_DIR / f"{today}_{one_word(plan.company)}_{jid}"
            rel = str(folder.relative_to(PROJECT_DIR))
            save_plan(plan, row, folder)
            if plan.cover_letter:
                pdf = folder / person.file_name("cover_letter")
                render_cover_letter_pdf(pdf_browser, plan.cover_letter, answers, pdf)
                plan.cover_letter_pdf = str(pdf)

            if plan.blockers:
                reasons = "; ".join(f"{b.label[:40]}: {b.reason}" for b in plan.blockers[:3])
                record(jid, status="needs_manual", note=f"bot: needs you {today} ({reasons}) → {rel}")
                outcomes.append(Outcome(jid, plan.company, plan.title, "needs_manual", reasons, rel))
                log(f"  needs you   {reasons}")
                continue

            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            try:
                problems = handler.fill(page, plan, state_name)
                page.screenshot(path=str(folder / "filled.png"), full_page=True)
            except Exception as e:  # page didn't load, selector timeout, ...
                problems = [f"form didn't load or fill: {type(e).__name__}: {str(e).splitlines()[0][:150]}"]
            save_plan(plan, row, folder, problems)

            if problems:
                record(jid, status="needs_manual", note=f"bot: couldn't fill {len(problems)} field(s) {today} → {rel}")
                outcomes.append(Outcome(jid, plan.company, plan.title, "needs_manual", "; ".join(problems[:3]), rel))
                log(f"  needs you   {problems[0]}")
            elif not live:
                warn = f" (warnings: {'; '.join(plan.warnings[:2])})" if plan.warnings else ""
                record(jid, note=f"bot: dry run ok {today}{warn} → {rel}")
                outcomes.append(Outcome(jid, plan.company, plan.title, "dry_run_ok", "; ".join(plan.warnings), rel))
                log(f"  dry run ok  screenshot: {rel}/filled.png{warn}")
            else:
                total, at_company = submitted_today(plan.company, today.isoformat())
                at_company += sum(1 for w in waiting if w["plan"].company == plan.company)
                if total + len(waiting) >= cfg["daily_limit"] or at_company >= cfg["per_company_per_day"]:
                    outcomes.append(Outcome(jid, plan.company, plan.title, "capped", "daily cap reached", rel))
                    log("  capped      daily or per-company limit reached; left in To apply")
                else:
                    ok, message = handler.submit(page)
                    item = {"jid": jid, "page": page, "context": context, "plan": plan, "folder": folder,
                            "rel": rel, "handler": handler, "tries": 0}
                    if not ok and message == NEEDS_CODE and hasattr(handler, "enter_code"):
                        # The site emailed a code to confirm a person is applying. Keep the page open;
                        # the person types the code into the dashboard (jobbot/codes.py).
                        page.screenshot(path=str(folder / "code_asked.png"), full_page=True)
                        sent_to = answers.get("identity", {}).get("email", "your email")
                        codes.request(jid, row["company"] or plan.company, row["role_title"] or plan.title,
                                      sent_to, CODE_WAIT_MINUTES)
                        item["until"] = time.time() + CODE_WAIT_MINUTES * 60
                        waiting.append(item)
                        record(jid, note=f"bot: waiting for the code {plan.company} emailed you {today}: "
                                         f"enter it on the dashboard → {rel}")
                        log(f"  waiting     {plan.company} emailed you a security code: enter it on the dashboard")
                        continue  # page stays open; the next application goes on meanwhile
                    ignored = "never reached" in message
                    if not ok and ("captcha" in message.lower() or ignored) and solve_checks:
                        # A CAPTCHA, or the site silently ignoring the bot's click: you finish it in this
                        # window (solve the check / click Submit yourself). The bot never does either.
                        page.bring_to_front()
                        from jobbot.runner import show_bot_window
                        show_bot_window()  # in front of your other apps, not just inside the browser
                        page.screenshot(path=str(folder / "check_asked.png"), full_page=True)
                        codes.request(jid, row["company"] or plan.company, row["role_title"] or plan.title,
                                      "", CODE_WAIT_MINUTES, kind="check")
                        codes.update(jid, message=("the site ignored the bot's click: click Submit yourself in the "
                                                   "jobbot browser window" if ignored else
                                                   "solve the CAPTCHA in the jobbot browser window"))
                        item["until"], item["kind"] = time.time() + CODE_WAIT_MINUTES * 60, "check"
                        item["baseline"] = page.inner_text("body")  # confirmation must appear after this
                        waiting.append(item)
                        record(jid, note=f"bot: waiting for you to solve the CAPTCHA in the jobbot window {today} → {rel}")
                        log(f"  waiting     {plan.company}: " + ("click Submit yourself" if ignored else "solve the CAPTCHA")
                            + " in the jobbot browser window")
                        continue
                    finish(item, ok, message)
                    continue  # finish() closed the page
            context.close()
        if waiting:
            log(f"Waiting up to {CODE_WAIT_MINUTES} minutes for you on {len(waiting)} application(s): "
                "codes go in the dashboard's box, CAPTCHAs get solved in the jobbot window.")
        serve_codes(wait=True)
        if pdf_browser is not browser:
            pdf_browser.close()
        browser.close()
    return outcomes
