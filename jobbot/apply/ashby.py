"""Ashby: plan from the job board's public GraphQL endpoint, fill jobs.ashbyhq.com/<org>/<id>/application.

The endpoint the job board itself uses (non-user-graphql, ApiJobPosting) returns every
field with its type, required flag and options, plus the demographic survey. On the
page, each field sits in a `.ashby-application-form-field-entry` whose <label for=…>
is the field's path, which is how fill() finds it.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from jobbot.apply.greenhouse import SUCCESS_RE, _choose_option, appeared

ASHBY_DONE_RE = re.compile(r"thanks for applying|application (was )?(successfully )?submitted", re.I)
from jobbot.apply.model import Plan, PlanError, Question

GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
FORM_URL = "https://jobs.ashbyhq.com/{org}/{job}/application"
USER_AGENT = "jobbot/1.0 (personal job search)"
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
ENTRY = ".ashby-application-form-field-entry"

QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    id title isListed descriptionHtml
    applicationForm { ...F }
    surveyForms { ...F }
  }
}
fragment F on FormRender {
  id
  sections { title fieldEntries { ... on FormFieldEntry { id field isRequired isHidden } } }
}"""

KINDS = {"String": "text", "Email": "text", "Phone": "text", "Number": "text", "SocialLink": "text",
         "LongText": "textarea", "Location": "location", "File": "file", "Boolean": "boolean",
         "ValueSelect": "select", "MultiValueSelect": "multiselect", "Date": "date"}


def _org_and_job(url: str) -> tuple[str, str]:
    segs = [s for s in urlsplit(url).path.split("/") if s]
    m = _UUID_RE.search(url)
    if not segs or not m:
        raise PlanError("couldn't work out the Ashby board and job id from the link")
    return segs[0], m.group(0)


def load(row: dict) -> Plan:
    org, job = _org_and_job(row["jd_url"])
    body = json.dumps({"operationName": "ApiJobPosting", "query": QUERY,
                       "variables": {"organizationHostedJobsPageName": org, "jobPostingId": job}}).encode()
    req = urllib.request.Request(GRAPHQL, data=body, headers={"Content-Type": "application/json",
                                                              "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    posting = (data.get("data") or {}).get("jobPosting")
    if not posting:
        raise PlanError(f"Ashby returned no posting ({json.dumps(data.get('errors'))[:120]})")

    questions: list[Question] = []
    forms = [("", posting.get("applicationForm"))] + [("eeo", f) for f in posting.get("surveyForms") or []]
    for section, form in forms:
        for sec in (form or {}).get("sections", []):
            for entry in sec.get("fieldEntries", []):
                f = entry.get("field") or {}
                path, title = f.get("path", ""), (f.get("title") or "").strip()
                kind = "hidden" if entry.get("isHidden") else KINDS.get(f.get("type"), "text")
                options = ["Yes", "No"] if kind == "boolean" else [v.get("label", "") for v in f.get("selectableValues") or []]
                sect = section
                if kind == "file":
                    sect = "resume" if path == "_systemfield_resume" else "cover_letter" if "cover letter" in title.lower() else ""
                elif kind in ("text", "textarea") and re.search(r"cover letter", title, re.I):
                    sect = "cover_letter"
                questions.append(Question(path, title, kind, bool(entry.get("isRequired")), options, sect))

    job_text = re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", posting.get("descriptionHtml") or ""))).strip()
    return Plan(handler="ashby", form_url=FORM_URL.format(org=org, job=job), company=row["company"],
                title=posting.get("title") or row["role_title"], job_text=job_text, questions=questions)


# ---------------------------------------------------------------------------
# Filling (Playwright page)
# ---------------------------------------------------------------------------

def _entry(page, key: str):
    return page.locator(f'{ENTRY}:has(label[for="{key}"])').first


def _shows_file(entry, filename: str, page, timeout_ms: int = 10000) -> bool:
    for _ in range(timeout_ms // 500):
        if filename in entry.inner_text():
            return True
        page.wait_for_timeout(500)
    return False


OPTIONS_JS = """(path) => [...document.querySelectorAll('input[type=radio], input[type=checkbox]')]
  .filter(i => (i.id && i.id.includes(path)) || (i.name && i.name.endsWith(path)))
  .map(i => { const l = i.id && document.querySelector(`label[for="${CSS.escape(i.id)}"]`);
              return (l ? l.innerText : (i.value !== 'on' ? i.value : i.name)).trim(); })"""
CLICK_JS = """([path, index]) => {
  const inputs = [...document.querySelectorAll('input[type=radio], input[type=checkbox]')]
    .filter(i => (i.id && i.id.includes(path)) || (i.name && i.name.endsWith(path)));
  const target = inputs[index];
  if (!target) return false;
  if (!target.checked) target.click();
  return target.checked;
}"""


def _check_option(page, key: str, wanted: str) -> bool:
    """Radio / checkbox options: inputs whose id contains the field path (survey radios are
    named <form>_<path>, outside the usual field entry). Clicks the input, confirms it's checked."""
    texts = page.evaluate(OPTIONS_JS, key)
    idx = _choose_option(texts, wanted) if texts else None
    if idx is None:
        return False
    ok = page.evaluate(CLICK_JS, [key, idx])
    page.wait_for_timeout(150)
    return bool(ok)


def _pick_location(page, entry, city: str, state: str) -> bool:
    box = entry.locator("input").first
    box.click()
    box.fill("")
    box.type(city, delay=35)
    page.wait_for_timeout(2500)
    options = page.get_by_role("option")
    for i, t in enumerate(options.all_inner_texts()):
        if t.lower().startswith(city.lower() + ",") and (not state or state.lower() in t.lower()):
            options.nth(i).click()
            return True
    return False


READBACK_JS = """(e) => {
  const kind = e.getAttribute('data-jobbot-kind');
  if (kind === 'boolean') return [...e.querySelectorAll('button')].some(b => b.getAttribute('aria-pressed') === 'true');
  if (kind === 'select' || kind === 'multiselect') {
    if (e.querySelector('input:checked')) return true;
    const combo = e.querySelector('input[role=combobox], input[type=text]');
    return !!(combo && combo.value.trim());
  }
  const field = e.querySelector('textarea, input:not([type=file]):not([type=checkbox]):not([type=radio])');
  return !!(field && field.value.trim());
}"""


def fill(page, plan: Plan, state_name: str) -> list[str]:
    page.goto(plan.form_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_selector('[id="_systemfield_name"]', timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    problems: list[str] = []

    # Files first: Ashby may parse the resume and prefill fields.
    for q in plan.questions:
        if q.kind != "file" or q.section not in ("resume", "cover_letter"):
            continue
        path = plan.resume_pdf if q.section == "resume" else plan.cover_letter_pdf
        if not path:
            continue
        entry = _entry(page, q.key)
        file_input = entry.locator("input[type=file]")
        if not file_input.count():
            file_input = page.locator(f'[id="{q.key}"]')
        file_input.set_input_files(path)
        if not _shows_file(entry, Path(path).name, page) and (q.required or q.section == "cover_letter"):
            problems.append(f"{q.label}: {Path(path).name} didn't attach")
    page.wait_for_timeout(2000)

    for q in plan.questions:
        a = plan.answers.get(q.key)
        if q.kind in ("file", "hidden") or not a or not a.value:
            continue
        entry = _entry(page, q.key)
        value = str(a.value)
        try:
            if q.kind in ("select", "multiselect"):
                for v in value.split(" | ") if q.kind == "multiselect" else [value]:
                    if not _check_option(page, q.key, v):
                        problems.append(f"{q.label}: couldn't select '{v}'")
                continue
            if not entry.count():
                if q.required:
                    problems.append(f"{q.label}: field not found on the page")
                continue
            entry.scroll_into_view_if_needed()
            if q.kind in ("text", "textarea"):
                page.fill(f'[id="{q.key}"]', value)
            elif q.kind == "date":
                box = entry.locator("input").first
                box.click()
                box.fill("")
                box.type(value, delay=20)
                page.keyboard.press("Enter")
                page.keyboard.press("Escape")
            elif q.kind == "location":
                if not _pick_location(page, entry, value, state_name):
                    problems.append(f"{q.label}: no city suggestion matched '{value}'")
            elif q.kind == "boolean":
                entry.get_by_role("button", name=value, exact=True).click()
        except Exception as e:
            problems.append(f"{q.label}: {type(e).__name__}: {str(e).splitlines()[0][:120]}")

    checked_js = """(path) => [...document.querySelectorAll('input[type=radio], input[type=checkbox]')]
      .some(i => ((i.id && i.id.includes(path)) || (i.name && i.name.endsWith(path))) && i.checked)"""
    for q in plan.questions:
        if not q.required or q.kind in ("hidden", "file"):
            continue
        if q.kind in ("select", "multiselect"):
            if not page.evaluate(checked_js, q.key):
                problems.append(f"{q.label}: nothing selected after filling")
            continue
        entry = _entry(page, q.key)
        if not entry.count():
            problems.append(f"{q.label}: field not found on the page")
            continue
        entry.evaluate("(e, k) => e.setAttribute('data-jobbot-kind', k)", q.kind)
        if not entry.evaluate(READBACK_JS):
            problems.append(f"{q.label}: still empty after filling")
    return problems


def _messages(node, out: list[str]) -> None:
    """Every human-readable message in a GraphQL reply (errors, validation results)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() in ("message", "errormessage", "errormessages", "messages") and isinstance(v, (str, list)):
                out += [v] if isinstance(v, str) else [str(x) for x in v if x]
            else:
                _messages(v, out)
    elif isinstance(node, list):
        for v in node:
            _messages(v, out)


def _reply_summary(replies: list) -> tuple[bool | None, str]:
    """(accepted?, what Ashby's server said) for the GraphQL calls made after clicking Submit."""
    said, ops = [], []
    for r in replies:
        op = (urlsplit(r.url).query.split("op=")[-1].split("&")[0]) if "op=" in r.url else "graphql"
        ops.append(op)
        try:
            body = r.json()
        except Exception:
            continue
        found: list[str] = []
        _messages(body, found)
        if "submit" in op.lower():
            if body.get("errors") or found:
                return False, f"Ashby's server answered {op}: {'; '.join(found)[:200] or 'an error'}"
            if "success" in json.dumps(body).lower():
                return True, f"Ashby accepted it ({op})"
        said += found
    if not any("submit" in o.lower() for o in ops):
        return None, ("the Submit click never reached Ashby's server (its in-page check, often reCAPTCHA, "
                      "stopped it)" + (f"; calls seen: {', '.join(ops[:4])}" if ops else ""))
    return None, f"Ashby's server didn't say ({', '.join(ops[:4])})" + (f": {'; '.join(said)[:150]}" if said else "")


def submit(page) -> tuple[bool, str]:
    button = page.get_by_role("button", name=re.compile(r"submit application", re.I))
    if not button.count():
        return False, "no Submit button found"
    replies: list = []
    listener = lambda r: replies.append(r) if "graphql" in r.url and r.request.method == "POST" else None
    page.on("response", listener)
    try:
        before = page.inner_text("body")
        button.first.click()
        for _ in range(60):
            page.wait_for_timeout(500)
            body = page.inner_text("body")
            if appeared(SUCCESS_RE, before, body) or appeared(ASHBY_DONE_RE, before, body):
                return True, "confirmation shown"
            error = re.search(r"(couldn.t|could not|unable to) submit[^.]*|flagged as spam[^.]*|something went wrong[^.]*", body, re.I)
            if error:
                return False, error.group(0)[:120]
            challenge = page.locator('iframe[title*="challenge" i], iframe[src*="recaptcha"][src*="bframe"], iframe[src*="hcaptcha.com"]')
            if challenge.count() and challenge.first.is_visible():
                return False, "CAPTCHA challenge appeared"
            accepted, said = _reply_summary(replies)
            if accepted is not None:
                return accepted, said
        return False, "no confirmation after 30s: " + _reply_summary(replies)[1]
    finally:
        page.remove_listener("response", listener)
