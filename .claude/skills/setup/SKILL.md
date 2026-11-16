---
name: setup
description: Install and set up jobbot for a new user on Windows or Mac (Python check, bot install, first start, GitHub push, nightly search, browser helper). Use when someone types /setup or asks to set up, install or start jobbot.
---

# Set up jobbot

You're installing jobbot for someone who may never have used a terminal. Run every command
yourself, explain each step in one plain sentence, and never ask them to type commands.
Work from the project folder. Detect the OS first (`ver` on Windows, `uname` otherwise) and
use `py -3` on Windows, `python3` on Mac/Linux.

1. **Python 3.11+**: run `<python> --version`. If it's missing or older, tell them to install it
   from https://www.python.org/downloads/ (Windows: tick "Add python.exe to PATH"; Mac: or
   `brew install python`) and stop until they say it's done.
2. **Private repo check**: run `git remote -v`. If it points at the shared template (not their
   own copy), stop and explain they need "Use this template → Create a new repository → Private"
   on GitHub and to open that copy instead. Their profile, resume and tracker must live only in a
   private repo.
3. **Install the bot**: run `<python> -m jobbot install`. It creates `.venv`, installs Playwright
   and Chromium, and reports whether Claude Code was found. If it wasn't, tell them to sign in to
   Claude Code (the app or `claude` in a terminal) and run step 3 again.
4. **Start jobbot**: tell them to double-click **Start jobbot** (`Start jobbot.bat` on Windows,
   `Start jobbot.command` on Mac). The setup wizard opens in their browser. Walk them through it
   if they want: their details, upload the resume and click "Read my resume with Claude", work
   authorization, "Turn this into search rules with Claude", "Suggest companies with Claude",
   standard answers, Save. Remind them to read what Claude wrote in the facts and fix anything wrong.
5. **Push the profile**: after they've saved, run `git add -A`, show `git status --short`, and
   commit with "Set up my jobbot profile". Then ask them to push (GitHub Desktop: Push origin;
   or run `git push` if their credentials work from here).
6. **Nightly search**: tell them to open their repo on GitHub → Actions → enable workflows →
   Nightly scrape → Run workflow. Once it finishes, restarting jobbot pulls the jobs in.
7. **Browser helper**: tell them to open `chrome://extensions` (or `edge://extensions`), turn on
   Developer mode, click Load unpacked and choose the `extension` folder; then Alt+J on any
   application page.

Finish with what happens next: pick roles on To review, the bot dry-runs Greenhouse / Ashby /
Lever applications, and they tick a site under "Submit for real on" only after checking a few
screenshots.
