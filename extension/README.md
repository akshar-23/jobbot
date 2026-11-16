# jobbot helper (browser extension)

Fills the job application open in your own browser, using the same answers and
drafting as the application bot. Use it for Workday, company portals, and anything
in the dashboard's **Needs you** tab. You stay logged in, you review, and you click
the site's own Next / Submit. The extension never submits.

## Install (Chrome, Edge, Brave, Arc)

1. Start jobbot (double-click **Start jobbot**, or `python -m jobbot serve`) and keep it running while you apply.
2. Open `chrome://extensions` (Edge: `edge://extensions`, Brave: `brave://extensions`).
3. Turn on **Developer mode** (top right).
4. Click **Load unpacked** and choose the `extension` folder inside your jobbot folder.
5. Pin **jobbot helper** to the toolbar (puzzle-piece icon → pin).

After editing files in this folder, click the reload arrow on the extension's card.

## Use

1. Open the application page (sign in to Workday first if it asks).
2. Click the **jobbot** button, or press **Alt+J** (Option+J on a Mac).
3. A panel shows what it filled (green outlines) and what needs you (amber outlines,
   with the reason). Click an item in the list to jump to it.
4. Check the page, then click the site's own **Next**. On the next page, click jobbot again.
5. After you submit, click **Mark applied** in the panel. It updates `tracker.csv`
   (or adds the job if it wasn't tracked).

**Copy cover letter** puts the drafted letter on your clipboard for paste-in fields.

**Save my answers**: when jobbot leaves questions for you (amber), answer them on the page, then
click this. jobbot saves your answers (`profile/saved_answers.json`) and fills them on every form
from then on, in the extension and in the bot. Review or delete them on the dashboard's
**Answers** tab. Answers that name a company are only reused for that company; questions about AI
use, "applied here before" and agreements are never saved.

After updating jobbot, click the reload arrow on the extension's card in `brave://extensions`.

## What it fills, and what it leaves for you

- Fixed answers (contact details, work authorization, sponsorship, demographics,
  education, address) come from `profile/answers.toml`. Free-text and fit questions are
  drafted by Claude from `profile/facts.md`. Same rules as the bot (`jobbot/apply/fixed.py`).
- The resume is attached as `First_Last_Resume.pdf` (your name). Files are attached first, then it
  waits a few seconds: many sites read the resume and refill name, phone and location.
- It never changes a field that already has a value, and never puts your resume into "Autofill from resume"
  boxes (those refill the whole form a few seconds later and wipe what was typed).
- After filling it checks the page kept everything and refills anything the site cleared.
- Left for you (amber): agreements you haven't allowed in `profile/bot.toml`, questions about
  AI use, legal/clearance questions, "applied here before?", your street address until you
  set `address.line1`, uploads other than resume/cover letter, and anything facts.md
  doesn't cover.

## Workday notes

Workday is several pages (My Information, My Experience, Application Questions,
Voluntary Disclosures, Self Identify, Review). Click jobbot on each page.
On **My Experience**, Workday's own "Autofill with resume" fills work history and
education more reliably than any filler; use that, then click jobbot for the rest.

## Troubleshooting

- **"jobbot isn't running"**: double-click Start jobbot (or run `python -m jobbot serve`).
- **Nothing happens on click**: the page may be a `chrome://` or PDF page, or the form is
  inside an embedded frame from another site. Open the form's own page (right-click the
  form → "Open frame in new tab") and click jobbot there.
- **A field was skipped or wrong**: open DevTools (Cmd+Option+J) on that page and run
  `__jobbot.log` to see each step the filler took.
