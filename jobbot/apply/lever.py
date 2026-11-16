"""Lever: plan from the server-rendered apply page, fill jobs.lever.co/<company>/<id>/apply.

Lever has no public questions API, but its apply page is plain HTML: standard fields
sit in <li class="application-question"> blocks, and each custom-question card carries
a hidden cards[<id>][baseTemplate] input with the card's JSON definition (field type,
required flag, options). Radio and checkbox values are the option text.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from jobbot.apply.greenhouse import SUCCESS_RE, appeared
from jobbot.apply.model import Plan, PlanError, Question

APPLY = "https://jobs.lever.co/{company}/{job}/apply"
API = "https://api.lever.co/v0/postings/{company}/{job}"
USER_AGENT = "jobbot/1.0 (personal job search)"
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
CARD_KINDS = {"multiple-choice": "select", "multiple-select": "multiselect", "dropdown": "dropdown",
              "text": "text", "textarea": "textarea", "file-upload": "file"}
EEO_LABELS = {"eeo[gender]": "Gender", "eeo[race]": "Race", "eeo[veteran]": "Veteran status",
              "eeo[disability]": "Disability status"}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _company_and_job(url: str) -> tuple[str, str]:
    segs = [s for s in urlsplit(url).path.split("/") if s]
    m = _UUID_RE.search(url)
    if not segs or not m:
        raise PlanError("couldn't work out the Lever company and job id from the link")
    return segs[0], m.group(0)


def load(row: dict) -> Plan:
    company, job = _company_and_job(row["jd_url"])
    page = _get(APPLY.format(company=company, job=job))
    if 'id="btn-submit"' not in page:
        raise PlanError("no application form on the Lever page (posting closed?)")

    questions: list[Question] = []
    # Standard fields: resume, name, email, phone, location, current company, links, comments.
    for block in re.findall(r'<li class="application-question[^"]*"[^>]*>(.*?)</li>', page, re.S):
        if "baseTemplate" in block or "cards[" in block:
            continue
        label_html = re.search(r'<div class="application-label[^"]*"[^>]*>(.*?)</div>', block, re.S)
        label = _clean(label_html.group(1)) if label_html else ""
        required = "✱" in label or "required" in (label_html.group(0) if label_html else "")
        label = label.replace("✱", "").strip()
        field = re.search(r'<(input|textarea|select)\b[^>]*\bname="([^"]+)"[^>]*>', block)
        if not field:
            continue
        tag, name = field.group(1), field.group(2)
        ftype = re.search(r'type="([^"]+)"', field.group(0))
        if name == "resume":
            questions.append(Question("resume", label, "file", required, section="resume"))
        elif name == "comments":
            questions.append(Question("comments", label, "textarea", False, section="cover_letter"))
        elif name == "location":
            questions.append(Question("location", label, "location", required, section="location"))
        elif tag == "textarea" or (ftype and ftype.group(1) in ("text", "email", "tel", "url")):
            questions.append(Question(name, label, "textarea" if tag == "textarea" else "text", required))

    # Custom question cards, from their JSON definitions.
    for m in re.finditer(r'<input[^>]*name="cards\[([^\]]+)\]\[baseTemplate\]"[^>]*>', page):
        value = re.search(r'value="([^"]*)"', m.group(0))
        if not value:
            continue
        card = json.loads(html.unescape(value.group(1)))
        for i, f in enumerate(card.get("fields", [])):
            kind = CARD_KINDS.get(f.get("type"), "text")
            options = [o.get("text", "") for o in f.get("options") or []]
            questions.append(Question(f"cards[{m.group(1)}][field{i}]", f.get("text", ""), kind,
                                      bool(f.get("required")), options))

    # Demographic selects.
    for name, label in EEO_LABELS.items():
        sel = re.search(rf'<select[^>]*name="{re.escape(name)}"[^>]*>(.*?)</select>', page, re.S)
        if sel:
            opts = [_clean(o) for o in re.findall(r"<option[^>]*>(.*?)</option>", sel.group(1), re.S)]
            questions.append(Question(name, label, "dropdown", False,
                                      [o for o in opts if o and not o.lower().startswith("select")], section="eeo"))

    try:
        api = json.loads(_get(API.format(company=company, job=job)))
        parts = [api.get("descriptionPlain", "")] + [f"{l.get('text', '')}: {_clean(l.get('content', ''))}"
                                                     for l in api.get("lists", [])] + [api.get("additionalPlain", "")]
        job_text, title = " ".join(p for p in parts if p).strip(), api.get("text") or row["role_title"]
    except (OSError, ValueError):
        job_text, title = _clean(page)[:14000], row["role_title"]
    return Plan(handler="lever", form_url=APPLY.format(company=company, job=job), company=row["company"],
                title=title, job_text=job_text, questions=questions)


# ---------------------------------------------------------------------------
# Filling (Playwright page)
# ---------------------------------------------------------------------------

def _attr(name: str) -> str:
    return '[name="' + name.replace('"', '\\"') + '"]'


def _pick_location(page, city: str, state: str) -> bool:
    box = page.locator("#location-input")
    if not box.count():
        return False
    box.click()
    box.fill("")
    box.type(city, delay=35)
    page.wait_for_timeout(2500)
    # Suggestions look like "San Francisco, CA, USA" (state codes, not names).
    options = page.locator(".dropdown-results .dropdown-location")
    texts = [t.strip() for t in options.all_inner_texts()]
    matches = [i for i, t in enumerate(texts) if t.lower().startswith(city.lower() + ",")]
    us = [i for i in matches if re.search(r"\b(usa|united states)\b", texts[i], re.I)
          or (state and state.lower() in texts[i].lower())]
    pick = (us or matches or [None])[0]
    if pick is None:
        return False
    options.nth(pick).click()
    page.wait_for_timeout(300)
    return True


READBACK_JS = """([name, kind]) => {
  const els = [...document.querySelectorAll(`[name="${name}"]`)];
  if (!els.length) return 'missing';
  if (kind === 'select' || kind === 'multiselect') return els.some(e => e.checked) ? 'ok' : 'empty';
  const v = (els[0].value || '').trim();
  return v && !/^select/i.test(v) ? 'ok' : 'empty';
}"""


def fill(page, plan: Plan, state_name: str) -> list[str]:
    page.goto(plan.form_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_selector('input[name="name"]', timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    problems: list[str] = []

    # Resume first: Lever parses it and prefills name, email and links. The upload is done
    # when Lever stores it (hidden resumeStorageId gets a value).
    resume = next((q for q in plan.questions if q.section == "resume"), None)
    if resume and plan.resume_pdf:
        page.set_input_files('input[name="resume"]', plan.resume_pdf)
        for _ in range(30):
            if page.evaluate("() => !!(document.querySelector('[name=resumeStorageId]') || {}).value"):
                break
            page.wait_for_timeout(500)
        else:
            problems.append(f"{resume.label}: {Path(plan.resume_pdf).name} didn't upload")
        page.wait_for_timeout(1500)

    for q in plan.questions:
        a = plan.answers.get(q.key)
        if q.kind in ("file", "hidden") or not a or not a.value:
            continue
        value = str(a.value)
        try:
            if q.kind in ("text", "textarea"):
                page.fill(_attr(q.key), value)
            elif q.kind == "location":
                if not _pick_location(page, value, state_name):
                    page.fill("#location-input", value)  # free text is accepted when nothing matches
            elif q.kind == "dropdown":
                page.select_option(_attr(q.key), label=value)
            elif q.kind in ("select", "multiselect"):
                for v in value.split(" | ") if q.kind == "multiselect" else [value]:
                    page.locator(f'{_attr(q.key)}[value="{v}"]').check()
        except Exception as e:
            problems.append(f"{q.label}: {type(e).__name__}: {str(e).splitlines()[0][:120]}")

    for q in plan.questions:
        if not q.required or q.kind in ("hidden", "file"):
            continue
        key = "location" if q.kind == "location" else q.key
        state = page.evaluate(READBACK_JS, [key, q.kind])
        if state != "ok":
            problems.append(f"{q.label}: still {state} after filling")
    return problems


def submit(page) -> tuple[bool, str]:
    button = page.locator("#btn-submit")
    if not button.count():
        return False, "no Submit button found"
    before = page.inner_text("body")
    button.click()
    for _ in range(40):
        page.wait_for_timeout(500)
        if "/thanks" in page.url or appeared(SUCCESS_RE, before, page.inner_text("body")):
            return True, "confirmation shown"
        challenge = page.locator('iframe[src*="hcaptcha.com"][title*="challenge" i], iframe[title*="hCaptcha challenge" i]')
        if challenge.count() and challenge.first.is_visible():
            return False, "hCaptcha challenge appeared"
        errors = page.locator(".application-error, .error-message").all_inner_texts()
        if any(e.strip() for e in errors):
            return False, "form errors: " + "; ".join(e.strip()[:80] for e in errors if e.strip())[:200]
    return False, "no confirmation after 20s"
