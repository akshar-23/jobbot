# jobbot: job search assistant

You're helping the person who owns this copy of jobbot run a structured job search. Their
profile is in `profile/` and their personal context, targets and decisions are imported here:

@profile/about.md

If that file is missing, this copy hasn't been set up: send them to the setup page (start jobbot
with the Start file or `python -m jobbot serve`; it opens on first run) or `/setup` in Claude Code.

## Where things are

```
profile/            everything about the person (their private repo only; never in the template)
  about.md          context for you: who they are, targets, resume variants, dated decisions
  facts.md          the only facts allowed in drafted letters and answers (plus writing rules)
  answers.toml      fixed form answers: contact, work authorization, salary, demographics. Never AI-written
  search.toml       what to look for: title rules, level, years, locations, job sources, fit-check guide
  companies.json    company job boards the nightly scraper checks
  bot.toml          bot settings: live handlers, caps, agreements, fit model, auto-run, forbidden_terms
  saved_answers.json their own answers to questions nothing else covers
profile.example/    a sample profile (a fictional new-grad animator); read when profile/ is missing
tracker.csv         every role seen and its status
jobbot/             the local tool (dashboard, sync, ranking, link check, fit check, bot, extension API)
  profile.py        the one reader of profile/; profile_io.py writes it (setup wizard)
  apply/            the application bot (one handler per application system)
extension/          browser helper for Workday and anything the bot can't do (load unpacked)
scraping/scraper.py nightly job on GitHub Actions; postings/ and state/ are its output
tests/              run after changing rules; they use tests/fixtures/profile, never the real profile
applications/       per-application drafts, plans and screenshots (gitignored)
resumes/, cover_letters/, jds/, reports/   the person's documents
Start/Install/Update jobbot.(bat|command)   double-click launchers (Windows / Mac)
```

`python` below means `python3` on Mac and `py -3` (or `python`) on Windows. The bot's own
Python is `.venv/bin/python` (Mac) or `.venv\Scripts\python.exe` (Windows); `python -m jobbot install` creates it.

## What you do not do

- **Modify the source resumes in `resumes/`.** Tailored copies go to `resumes/tailored/`, and only when asked.
- **Change resume layout, formatting or page count.** Content suggestions only.
- **Submit an application the person didn't pick.** Only rows they moved to `to_apply`, under the guardrails below.
- **Generate work-authorization, sponsorship, salary or demographic answers with AI.** They come from `answers.toml` or their saved answers.
- **Bypass CAPTCHAs, bot checks or logins.** Those applications go to `needs_manual`.
- **Automate or scrape LinkedIn, Handshake, Indeed, Glassdoor or ZipRecruiter** (including outreach). Direct company sources only; the person pastes external apply links by hand.
- **Run the scraper interactively.** That's the nightly job; you build and debug it, testing pieces against copies.
- **Put personal data into shared files** (code, `CLAUDE.md`, `profile.example/`, `tests/`). Personal things go in `profile/`.

## Operational guidelines

- Concise output: report what you did in 1-3 lines.
- Updating tracker rows is fine (git can undo); deleting needs confirmation. Show what you changed.
- Do file and data work yourself rather than asking the person to.
- Don't fabricate. If a posting doesn't say whether it sponsors visas, write "unclear".
- Commit only when asked; the person pushes (their GitHub key may not be available to you).

---

## Resume tailoring

**Default: suggestions only**, in this format, which the person applies in Word (in-place .docx edits drift):

```
RESUME VARIANT TO START FROM: [variant]

SUMMARY LINE — Replace with:
"[new summary line]"            (or: "no change needed")

SKILLS SECTION — Changes:
- Add [keyword] to [which bullet]
- Reorder: move [X] before [Y] in [bullet]
- Remove [keyword] (dilutes signal for this JD)      (or: "no changes needed")

BULLET REWRITES (only if needed):
In [Role at Company], change bullet [N] from "[old]" to "[new]"   (or: "no bullet rewrites needed")

PROJECT SECTION — Changes (only if needed):
Swap [current project] for [new project from the inventory in about.md]   (or: "no project swaps needed")

COURSEWORK — Replace with:
"Coursework: [new list]"        (or: "no change needed")
```

Suggestions may cover skills wording and order, light bullet rephrasing (never new accomplishments), project
selection, the summary line and coursework. They may not touch layout, fonts, spacing, section order, tables,
page count (flag a rewrite that would spill onto a second page), education facts, employment facts, or
anything `about.md` marks as fixed (e.g. publication entries). If asked for a structural change, push back:
"That's a structural change, not a content tweak. Want to do it anyway, or find a content-level alternative?"

Only when the person says "apply these" / "make the tailored .docx": edit the source with `python-docx`
(only the runs that change), save to `resumes/tailored/{Role}_{Company}_{MM-DD}.docx`, set the tracker
row's `resume_variant` to `tailored:{filename}`, and remind them to do a 30-second visual diff before sending.

## Cover letters

Plain-text files in `cover_letters/`, named `{Role}_{Company}_{MM-DD}.txt` (role and company one word each,
`_v2` for a same-day duplicate). Follow the cover-letter rules in `facts.md` (4 short paragraphs, 200 words
max, a hook tied to something concrete in the posting). Use only `facts.md`; if the posting needs something
they lack, flag it instead of claiming it. Tell them to skip clearly bad fits (seniority far beyond theirs,
or "no sponsorship" when they need it). Then report the filename, word count and resume variant, and update
the tracker row (`resume_variant`, `cover_letter: {filename}` in notes). If they paste a letter written
elsewhere, just save it and update the tracker.

---

## The nightly scraper

`scraping/scraper.py` runs on GitHub Actions (`.github/workflows/scrape.yml`, 07:00 UTC, and "Run workflow"
by hand). It reads `profile/companies.json` (Greenhouse, Lever, Ashby, Workable, SmartRecruiters by API;
Workday by its public search; Amazon by its search API) and, when `[sources] simplify_new_grad` is on, the
public SimplifyJobs New-Grad list. **It never edits `tracker.csv`.** It writes `scraping/postings/YYYY-MM-DD.csv`
(new postings) and `scraping/state/open_jobs.json` (postings still listed). `python -m jobbot sync` (also run by
`serve`) pulls those commits and imports them, so the nightly commit and local edits never conflict.
Rate limit: one request per 2-3 seconds per host. Dedupe is by job identity (`jobbot.common.job_identity`),
plus company + title for Simplify rows. 20-50 new postings a day is normal; 100+ means the filters are loose.

**Filter rules.** Generic ones live in the code: US or remote-US only; interns, students, senior / staff /
principal / director titles and non-US countries in the title are dropped; sponsorship / citizenship
wording drops a posting only for someone who needs sponsorship (or isn't a citizen), per `answers.toml`.
Everything specific to the person is in `profile/search.toml`: title `include` / `not_target` /
`outside_background` / `other_tracks` lists, `max_level` and `level_nouns`, `max_years_required`
(postings asking more are dropped unless they invite new grads), manager / lead / PhD / contract /
apprenticeship switches, `[[company_rules]]`, `[sources]` and `[[locations]]` (ranking order). The scraper
builds its regexes from it through `jobbot/profile.py`; the dashboard re-applies them to old rows through
`jobbot/eligibility.py`. When uncertain, keep the row with `notes = "flagged: [reason]"`. Run
`python tests/test_fit_rules.py` after changing the rules.

## Tracker (`tracker.csv`)

Columns: `date_added, date_posted, company, role_title, jd_url, location, salary_range, sponsors_h1b,
resume_variant, status, referral_contact, date_applied, response_received, response_date, next_action,
next_action_date, notes`. Statuses: `to_review`, `to_apply`, `applied`, `interviewing`, `offer`, `rejected`,
`ghosted`, `skipped`, `closed` (no longer listed), `needs_manual` (the bot couldn't finish). Notes tags:
`src:simplify`, `src:manual`, `new-grad`, `flagged: …`, `closed: …`, `cover_letter: {filename}`,
`fit: good|stretch|poor — reason`, `bot: …`. Dates ISO; sync fixes Excel's M/D/YYYY and merges duplicate
rows keeping the most advanced status. When the person reports an action ("I applied to X", "X rejected me"),
update the row.

## The local tool (`jobbot/`)

- `python -m jobbot serve`: sync, background link check then fit check, and the dashboard at
  http://127.0.0.1:8765 (the setup wizard on first run; later the "Profile & setup" link). Tabs: To review,
  To apply, Needs you, Applied, Skipped/closed, Answers. Default view: posted in the last 30 days, passes the
  filters, not a poor fit.
- `python -m jobbot fit`: reads each new role's job description (board APIs, Workday, Workable, Microsoft,
  page JSON-LD; cached in `.jobbot/jd/`), rules first (too many years, sponsorship / citizenship for people
  who need it), then Claude (`fit_model` in `bot.toml`, 6 per call) judges good / stretch / poor against
  `facts.md` using `search.toml [fit] guide`. No readable description → stretch, never hidden. Hedged
  sponsorship wording ("may not … support future H-1B sponsorship") is never better than stretch.
  `--recheck` after `facts.md` changes.
- `python -m jobbot check`: closes dead postings (never deletes), fills `date_posted`; rechecked weekly.
- `python -m jobbot sync` / `top -n 20` / `add URL` / `install` / `update` / `template DIR`.
- Ranking (`rank.py`, no AI): location order from `search.toml`, freshness (+25 at ≤3 days to −15 past 60),
  salary vs `answers.toml` floor/target, new-grad signal, fit verdict (good +10, poor −40).

**Submission policy.** The person picks every role. The bot submits only rows they moved to `to_apply`,
only on application systems they ticked as live (`live_handlers`), within `daily_limit` /
`per_company_per_day`, and:
- work authorization, sponsorship, salary, demographic and contact answers come only from `answers.toml`
  or their saved answers; a required field still `TODO` blocks submission;
- letters and custom answers come only from `facts.md`; anything it can't answer → `needs_manual`;
- CAPTCHAs, logins, account creation, Workday and custom portals → `needs_manual`;
- every run saves `applications/<date>_<Company>_<id>/` (`job.txt`, `plan.json`, the letter, `filled.png`);
- new handlers start as dry runs (fill, screenshot, stop) until the person ticks them.

**How the bot works** (`jobbot/apply/`, `python -m jobbot apply [--dry-run] [--limit N] [--id JOBID] [--headed]`
with the bot's Python, or the To apply tab's "Run bot" buttons / auto-run on pick, `jobbot/runner.py`):
1. Handlers `greenhouse.py` (public API questions; fills the embed form), `ashby.py` (GraphQL questions +
   survey; `/application`), `lever.py` (server-rendered `/apply`; hCaptcha may send it to Needs you). Each has
   `load(row) -> Plan`, `fill(page, plan, state)`, `submit(page)`.
2. `fixed.py` answers from `answers.toml`: contact, links (LinkedIn, portfolio, reel, ArtStation, GitHub,
   Scholar), work authorization and sponsorship, visas held (a status question, not sponsorship), OPT dates,
   citizenship, salary, demographics (stored as meanings: "No" finds "I am not a protected veteran"; a race list
   picks the first option a form offers, never "American Indian" for "Indian"; orientation / transgender always
   declined), education (degree names from `education.degree`), location (all 50 states), company history,
   relatives, government employment, accommodation, essential functions, onsite / hybrid. `[misc]` yes/no
   fields set to `""` mean "ask me". **Saved answers** (`saved.py`) come right after the AI-policy check: the
   person's own answers, keyed by the normalized question with the company name as `{company}`; an answer
   naming a company stays with that company. Blockers (→ Needs you): AI-use attestations (the bot drafts with
   AI, so it never attests it didn't), legal / background questions without a saved answer, "applied here
   before", oddly phrased sponsorship questions, agreements `bot.toml` doesn't allow. Keep
   `tests/test_fixed_rules.py` and `tests/test_saved_answers.py` passing.
3. `draft.py` sends the rest plus the posting to Claude Code (`claude -p`, no tools, JSON schema, system
   prompt from `facts.md`, run from a temp dir): cover letter, answers, fit warnings, dealbreakers. A
   dealbreaker, a posting that bans AI or rules out sponsorship the person needs, or a `forbidden_terms` word
   → `needs_manual`.
4. Files first (Greenhouse re-parses the resume), every required field read back; problems → `needs_manual`.
5. Successful submissions: letter copied to `cover_letters/`, row → `applied`, `resume_variant` from `bot.toml`,
   `after_submit.png` (the confirmation page) saved. A live run reuses the cover letter and Claude-written answers
   from the job's latest clean dry run (`engine.reviewed_draft`), so what's sent is what the person reviewed;
   only questions the dry run didn't cover are drafted fresh. No confirmation seen → `needs_manual`, never `applied`.
   A confirmation counts only if it appears after Submit (`greenhouse.appeared`, `engine.confirmed(page, before)`):
   Greenhouse forms show the job description, which can end "Thank you for your interest…". Some companies
   (Ashby especially) send no confirmation email; the `after_submit.png` screenshot is the proof.
6. The To apply tab's "I applied myself" button only records an application made outside jobbot (it asks first);
   it never sends anything.
7. **Answer here** (Needs you rows, `jobbot/resolve.py`): when a run stopped on decisions (an agreement, a
   question without an answer, an AI-use question, a fit dealbreaker, a "no sponsorship" hint), the person
   answers in a dialog; answers go to that job's `applications/…/overrides.json`, `plan_job` uses them first
   (source "you, on the dashboard"; "apply anyway" turns dealbreakers and sponsorship hints into warnings), the
   row returns to To apply and the bot runs it again. "Remember" (plain questions only) also saves the answer
   for every form. Logins, bot checks, extra uploads and AI-banning postings aren't answerable there.
   The To apply tab also has "Agree for me to:" checkboxes for `accept_privacy_notices`,
   `accept_other_agreements` and `accept_arbitration_agreements` (each asks first).
8. **Not done, on purpose:** running the bot in the person's own browser profile, or disguising it, to get past
   the sites' bot checks. That's evading the check, which this project rules out.

**Live submission meets anti-bot checks (found 2026-11-15).** Greenhouse emails an 8-character "confirm you're a
human" code after Submit, and Ashby silently turns the automated browser away. jobbot never reads that code
from email or disguises the browser. **Greenhouse codes go through the dashboard** (`jobbot/codes.py`): the bot
keeps the page open (`code_asked.png`), moves on to the next application, and the dashboard shows a "security code
is needed" banner on every tab (title "(1) code needed"); the person pastes the code from their email, the bot
types it (`greenhouse.enter_code`) and submits; a wrong code is asked for again (2 retries); after
`CODE_WAIT_MINUTES` (15) without a code the row goes to Needs you. **CAPTCHAs** (Ashby showed one to the bot on 2026-11-15; Lever uses hCaptcha): with "Let me solve CAPTCHAs in
the bot's window" (`solve_checks_myself` in `bot.toml`, a To apply checkbox), live runs use a visible browser;
on a CAPTCHA the bot brings that window to the front, the dashboard's waiting box says "solve the CAPTCHA in
the jobbot window", and the bot watches the page for the site's confirmation (15 minutes), carrying on with
other applications meanwhile. The same hand-off covers Ashby silently ignoring the bot's
click ("never reached Ashby's server"): the window comes to the front and the person
clicks Submit there. The bot never solves or bypasses a CAPTCHA itself, and never clicks Submit in the person's
own browser. Ashby blocks the automated browser reliably; the dependable Ashby path is "open application form"
+ Alt+J in the person's own browser. Without the setting, it goes
to Needs you. Ashby's submit now records its server's reply (`ashby._reply_summary`): "the click never reached Ashby's
server" (its in-page check stopped it), "Ashby's server answered …: <message>" (a fixable form problem), or
accepted. Phone boxes whose dial code comes from a separate field (Greenhouse's "Country", Workday's
"Country Phone Code") get the local number only; the bot also picks "United States" in Greenhouse's
"Country" (otherwise "+1 +1 …"). For Ashby (if it rejects the bot) and anything else, the flow is: the bot dry-runs (drafts the letter and answers,
fills the form, screenshot), then the person finishes in their own browser: the dashboard's "open application
form" link (the form's own address from `plan.json`), Alt+J (the helper reuses the dry run's letter and
answers from the job's `applications/` folder), the site's Submit, "Mark applied". A failed live submit lands
in Needs you with exactly those steps. Greenhouse's page-only demographic questions (Hispanic, then Race)
are answered from `answers.toml` like the rest.

**Browser helper** (`extension/`, Manifest V3, `activeTab` + `scripting`, loaded unpacked in Chrome / Edge /
Brave): on Alt+J it scans the page's empty fields (including Workday listboxes and react-select boxes), asks
`POST /api/ext/plan` (`assist.py`: same rules, saved answers and drafter as the bot), fills files first, types
search boxes key by key, never overwrites a filled field and never submits. "Save my answers" stores what the
person typed into amber fields (`/api/ext/remember`). Since content.js v3: it never attaches the resume to "Autofill from resume" boxes (they refill the form
seconds later and wipe what was typed), fills Yes/No button pairs (Ashby), finds suggestions rendered anywhere
on the page and picks a single option (not the list's wrapper), skips dropdowns already showing a choice, and
after filling waits 1.5 s, refills anything the page cleared, and reports optional fields left blank.
The helper reuses the bot's reviewed dry-run answers for the job (they win over its own older drafts).
Tested on real Ashby and Greenhouse forms: everything required filled in one click. All `/api/ext/*` routes need the `X-Jobbot: 1` header.

**Setup and sharing.** The setup wizard (`web/setup.html`, `profile_io.py`) writes `profile/`; its Claude
buttons (`setup_ai.py`) read the resume PDF into a draft `facts.md`, turn "the jobs I want" into
`search.toml` rules and a fit guide, and suggest companies, whose public boards `boards.py` finds.
`python -m jobbot template DIR` copies the shared code (never `profile/`, tracker, resumes or letters) into
the template repo and stops if any personal detail shows up; `python -m jobbot update` pulls the template's
latest shared files into a personal copy.

## Morning triage

"Show me what came in overnight": `to_review` rows added since the last triage, grouped by the tiers in
`about.md`, one line each (company, role, location, salary if known, why it's interesting, its fit verdict),
with a suggested action (`to_apply`, `skipped`, or flag for more info). Wait for decisions, then update the
tracker. For roles moved to `to_apply`, suggest a resume variant and offer a cover letter.

## Reporting and follow-ups

Status report: applied this week / month, by tier, active interviews, `to_review` backlog, and follow-ups:
`applied` more than 10 business days ago with no response → offer a follow-up email draft (never send it).

## First session checklist

1. If `profile/about.md` is missing, point them to setup (above) and stop.
2. Check git is initialized; suggest it if not.
3. Make sure `cover_letters/` and `resumes/tailored/` exist.
4. Run `python -m jobbot sync`, then give a 2-line status from `tracker.csv`: total roles, counts by status,
   how many `to_review` and how old they are.
5. Wait for instructions.
