"""Greenhouse: plan from the public job API, fill the embedded application form.

Every Greenhouse job is filled through the embed URL
(job-boards.greenhouse.io/embed/job_app?for=<board>&token=<job id>), which
works even for companies whose own careers site hides the hosted form (Stripe).
Field ids on that form match the API's field names, so the plan maps 1:1.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import quote

from jobbot.apply.fixed import match_option
from jobbot.apply.model import Answer, NEEDS_CODE, Plan, PlanError, Question
from jobbot.common import job_identity
from jobbot.linkcheck import _company_config, _greenhouse_slug

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job}?questions=true"
EMBED = "https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={job}"
USER_AGENT = "jobbot/1.0 (personal job search)"
FIELD_KINDS = {"input_text": "text", "textarea": "textarea", "input_file": "file",
               "multi_value_single_select": "select", "multi_value_multi_select": "multiselect",
               "input_hidden": "hidden"}
_TAG_RE = re.compile(r"<[^>]+>")

VERIFY_RE = re.compile(r"verification code (was|has been) sent|enter the \d+-character code|confirm you.re a human"
                       r"|security code", re.I)
def appeared(rx: re.Pattern, before: str, now: str) -> bool:
    """Did this confirmation text show up after Submit? Forms often show the job description,
    and a description ending "Thank you for your interest…" mustn't read as a confirmation."""
    return len(rx.findall(now)) > len(rx.findall(before))


SUCCESS_RE = re.compile(r"thank you for (applying|your (application|interest))|application (has been )?(submitted|received)"
                        r"|we.ve received your application", re.I)


def _text(html_content: str) -> str:
    s = _TAG_RE.sub(" ", html.unescape(html_content or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def load(row: dict) -> Plan:
    """Read the job and its questions from the API. Nothing is filled yet."""
    gh_slugs, _ = _company_config()
    slug = _greenhouse_slug(row["jd_url"], row["company"], gh_slugs)
    ident = job_identity(row["jd_url"])
    if not slug or not ident.startswith("gh:"):
        raise PlanError("couldn't work out the Greenhouse board and job id from the link")
    job = ident[3:]
    req = urllib.request.Request(API.format(slug=quote(slug), job=job), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    questions: list[Question] = []
    for q in data.get("questions", []):
        fields = q.get("fields") or []
        if not fields:
            continue
        first = fields[0]
        kind = FIELD_KINDS.get(first.get("type"), "text")
        options = [v.get("label", "") for v in first.get("values") or []]
        section = first["name"] if first["name"] in ("resume", "cover_letter") else ""
        questions.append(Question(first["name"], q.get("label", ""), kind, bool(q.get("required")), options, section))
    for q in data.get("location_questions", []) or []:
        if any(f.get("type") == "input_text" for f in q.get("fields") or []):
            questions.append(Question("candidate-location", q.get("label", "Location"), "location",
                                      bool(q.get("required")), section="location"))
    for group in data.get("compliance", []) or []:
        for q in group.get("questions", []) or []:
            for f in q.get("fields") or []:
                questions.append(Question(f["name"], q.get("label", f["name"]), "select",
                                          bool(q.get("required")),
                                          [v.get("label", "") for v in f.get("values") or []], section="eeo"))
    education = str(data.get("education") or "")
    if education.startswith("education_"):
        required = education == "education_required"
        questions += [Question("school--0", "School", "search", required, section="education"),
                      Question("degree--0", "Degree", "select", required, section="education")]

    return Plan(handler="greenhouse", form_url=EMBED.format(slug=quote(slug), job=job),
                company=data.get("company_name") or row["company"], title=data.get("title") or row["role_title"],
                job_text=_text(data.get("content", "")), questions=questions)


# ---------------------------------------------------------------------------
# Filling (Playwright page)
# ---------------------------------------------------------------------------

def _sel(key: str) -> str:
    return f'[id="{key}"]'


def _shows_file(page, filename: str, timeout_ms: int = 10000) -> bool:
    """Greenhouse replaces the file input with the file's name and a Remove button once uploaded."""
    for _ in range(timeout_ms // 500):
        if page.evaluate("(n) => [...document.querySelectorAll('p, span')].some(e => e.textContent.trim() === n)", filename):
            return True
        page.wait_for_timeout(500)
    return False


def _choose_option(texts: list[str], wanted: str) -> int | None:
    low = [t.strip().lower() for t in texts]
    w = wanted.strip().lower()
    if w in low:
        return low.index(w)
    # "United States+1"-style labels: the wanted text plus a non-word suffix.
    suffixed = [i for i, t in enumerate(low) if t.startswith(w) and not t[len(w):len(w) + 1].isalpha()]
    if len(suffixed) == 1:
        return suffixed[0]
    hit = match_option(texts, wanted)
    return texts.index(hit) if hit else None


def _pick(page, key: str, wanted: str, search: bool = False) -> bool:
    """Open a react-select, optionally type to search, click the matching option."""
    box = page.locator(_sel(key))
    if not box.count():
        return False
    box.scroll_into_view_if_needed()
    for typed in ([wanted[:40]] if search else [wanted[:30], ""]):
        box.click()
        if typed:
            box.fill("")
            box.type(typed, delay=15)
        page.wait_for_timeout(1800 if search else 400)
        listbox = box.get_attribute("aria-controls")
        if not listbox:
            page.keyboard.press("Escape")
            continue
        options = page.locator(f'[id="{listbox}"] [role=option]')
        idx = _choose_option(options.all_inner_texts(), wanted)
        if idx is not None:
            options.nth(idx).click()
            page.wait_for_timeout(250)
            return True
        page.keyboard.press("Escape")
    return False


def _pick_location(page, city: str, state: str) -> bool:
    box = page.locator(_sel("candidate-location"))
    if not box.count():
        return False
    box.scroll_into_view_if_needed()
    box.click()
    box.fill("")
    box.type(city, delay=30)
    page.wait_for_timeout(2500)
    listbox = box.get_attribute("aria-controls")
    if not listbox:
        return False
    options = page.locator(f'[id="{listbox}"] [role=option]')
    texts = options.all_inner_texts()
    for i, t in enumerate(texts):
        if t.lower().startswith(city.lower() + ",") and (not state or state.lower() in t.lower()):
            options.nth(i).click()
            return True
    return False


READBACK_JS = """(keys) => keys.map(k => {
  const el = document.getElementById(k);
  if (!el) return [k, 'missing'];
  const control = el.closest('.select__control');
  if (control) return [k, control.querySelector('.select__single-value, .select__multi-value') ? 'ok' : 'empty'];
  return [k, el.value && el.value.trim() ? 'ok' : 'empty'];
})"""


DOM_ONLY_EEO = ("hispanic_ethnicity", "race", "gender", "veteran_status", "disability_status")


EEO_FIELDS = {"hispanic_ethnicity": "hispanic_latino", "race": "race_ethnicity", "gender": "gender",
              "veteran_status": "veteran_status", "disability_status": "disability_status"}


def _eeo_dom_only(page, planned: set[str]) -> None:
    """Greenhouse shows some demographic questions only on the page (Hispanic/Latino first,
    then Race once that's answered). Answer them from answers.toml like any other EEO question
    (its decline option when the person declines, or when no option fits)."""
    from jobbot import profile as person
    from jobbot.apply.fixed import DECLINE_RE, _eeo
    eeo = person.answers().get("voluntary_disclosures", {})
    for _ in range(2):  # a second pass catches questions revealed by the first
        for key in DOM_ONLY_EEO:
            box = page.locator(_sel(key))
            if key in planned or not box.count():
                continue
            box.scroll_into_view_if_needed()
            box.click()
            page.wait_for_timeout(400)
            listbox = box.get_attribute("aria-controls")
            options = page.locator(f'[id="{listbox}"] [role=option]') if listbox else None
            texts = [t.strip() for t in options.all_inner_texts()] if options else []
            field = EEO_FIELDS[key]
            got = _eeo(Question(key, key, "select", False, texts), field, eeo.get(field, ""), "")
            wanted = getattr(got, "value", "") or next((t for t in texts if DECLINE_RE.search(t)), "")
            if wanted in texts:
                options.nth(texts.index(wanted)).click()
                planned.add(key)
            else:
                page.keyboard.press("Escape")
            page.wait_for_timeout(300)

def fill(page, plan: Plan, state_name: str) -> list[str]:
    """Fill the form from the plan. Returns problems (empty list = every required field is filled)."""
    page.goto(plan.form_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_selector(_sel("first_name"), timeout=20000)
    try:  # files attached before the page's scripts are ready are silently ignored
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    problems: list[str] = []

    # Newer Greenhouse forms take the dial code (+1) from their separate "Country" dropdown and
    # add it to the phone box's number: typing "+1 …" there too gave "+1 +1 …". Local number only.
    dial_country = (page.locator(".iti " + _sel("phone")).count() and page.locator(_sel("country")).count()
                    and "country" not in plan.answers)
    if dial_country:
        from jobbot import profile as person
        for key, ans in plan.answers.items():
            if ans.source.endswith("identity.phone") and isinstance(ans.value, str):
                plan.answers[key] = Answer(key, person.national_phone(ans.value), ans.source + " (local number)")

    # Files first: Greenhouse parses an uploaded resume and resets name and email.
    # A file counts as attached only once Greenhouse shows its name on the page.
    for q in plan.questions:
        if q.kind != "file":
            continue
        path = plan.resume_pdf if q.key == "resume" else plan.cover_letter_pdf if q.key == "cover_letter" else ""
        if not path:
            continue
        name = Path(path).name
        for _attempt in range(2):
            if page.locator(_sel(q.key)).count():
                page.set_input_files(_sel(q.key), path)
            if _shows_file(page, name):
                break
        else:
            if q.required or q.key == "cover_letter":
                problems.append(f"{q.label}: {name} didn't attach")
    page.wait_for_timeout(2000)

    for q in plan.questions:
        a = plan.answers.get(q.key)
        try:
            if q.kind in ("file", "hidden") or not a or not a.value:
                continue
            if q.section == "eeo" and not page.locator(_sel(q.key)).count():
                continue  # shown only after another answer; handled below
            if q.kind in ("text", "textarea"):
                page.fill(_sel(q.key), str(a.value))
            elif q.kind == "location":
                if not _pick_location(page, str(a.value), state_name):
                    problems.append(f"{q.label}: no city suggestion matched '{a.value}'")
            elif q.kind in ("select", "search"):
                if not _pick(page, q.key, str(a.value), search=q.kind == "search"):
                    problems.append(f"{q.label}: couldn't select '{a.value}'")
            elif q.kind == "multiselect":
                for v in str(a.value).split(" | "):
                    if not _pick(page, q.key, v):
                        problems.append(f"{q.label}: couldn't select '{v}'")
        except Exception as e:  # one bad widget shouldn't hide the rest of the report
            problems.append(f"{q.label}: {type(e).__name__}: {str(e).splitlines()[0][:120]}")

    if dial_country:  # the separate "Country" box that supplies the phone's +1
        country = person.answers().get("identity", {}).get("country", "United States")
        if not _pick(page, "country", country):
            problems.append(f"phone country: couldn't select {country}")

    try:
        _eeo_dom_only(page, {q.key for q in plan.questions if q.section == "eeo" and page.locator(_sel(q.key)).count()})
    except Exception as e:
        problems.append(f"demographic questions: {type(e).__name__}")

    # Files were verified by name above (their inputs are gone once attached).
    required = [q.key for q in plan.questions if q.required and q.kind not in ("hidden", "file")]
    for key, state in page.evaluate(READBACK_JS, required):
        if state != "ok":
            label = next(q.label for q in plan.questions if q.key == key)
            problems.append(f"{label}: still {state} after filling")
    return problems


def submit(page) -> tuple[bool, str]:
    """Click Submit and wait for a confirmation. Never retries, never works around a CAPTCHA."""
    button = page.locator("button[type=submit], input[type=submit]").filter(has_text=re.compile("submit", re.I))
    if not button.count():
        return False, "no Submit button found"
    before = page.inner_text("body")
    button.first.click()
    for _ in range(40):
        page.wait_for_timeout(500)
        if "confirmation" in page.url or appeared(SUCCESS_RE, before, page.inner_text("body")):
            return True, "confirmation page shown"
        challenge = page.locator('iframe[title*="challenge" i], iframe[src*="recaptcha"][src*="bframe"], iframe[src*="hcaptcha.com"]')
        if challenge.count() and challenge.first.is_visible():
            return False, "CAPTCHA challenge appeared"
        if VERIFY_RE.search(page.inner_text("body")[-3000:]):
            # Greenhouse's check that a person is applying. Never worked around: the bot waits for
            # the code the person reads from their email and types into the dashboard (codes.py).
            return False, NEEDS_CODE
    errors = page.locator("[aria-invalid=true], .error, .field-error").all_inner_texts()
    return False, "no confirmation after 20s" + (f"; form errors: {errors[:3]}" if errors else "")


WRONG_CODE_RE = re.compile(r"(invalid|incorrect|expired|wrong|didn.t match|does not match)[^.\n]{0,60}"
                           r"|code[^.\n]{0,40}(invalid|incorrect|expired)", re.I)


def enter_code(page, code: str) -> tuple[bool, str]:
    """Type the security code the person entered in the dashboard, submit, wait for a confirmation."""
    before = page.inner_text("body")
    boxes = page.locator('input[maxlength="1"]:visible')
    if boxes.count() >= len(code):
        boxes.first.click()
        page.keyboard.type(code, delay=60)  # most code boxes move to the next box by themselves
        if any(not boxes.nth(i).input_value() for i in range(len(code))):
            for i, ch in enumerate(code):
                boxes.nth(i).fill(ch)
    else:
        single = page.locator('input[autocomplete="one-time-code"]:visible, input[name*="code" i]:visible, '
                              'input[id*="code" i]:visible, input[aria-label*="code" i]:visible')
        if not single.count():
            return False, "couldn't find where to type the code"
        single.first.fill(code)
    page.wait_for_timeout(400)
    button = page.locator("button[type=submit], input[type=submit]").filter(has_text=re.compile("submit", re.I))
    if button.count() and button.first.is_enabled():
        button.first.click()
    for _ in range(40):
        page.wait_for_timeout(500)
        body = page.inner_text("body")
        if "confirmation" in page.url or appeared(SUCCESS_RE, before, body):
            return True, "confirmation page shown"
        wrong = WRONG_CODE_RE.search(body[-3000:])
        if wrong and VERIFY_RE.search(body[-3000:]):
            return False, f"wrong code: {wrong.group(0)[:80]}"
    return False, "no confirmation after the code"
