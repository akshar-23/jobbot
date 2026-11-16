# jobbot

A personal job-search assistant. Every night it checks the job boards of the companies you
choose, keeps the roles that fit you, and ranks them. You pick the ones you want. It then fills
in the applications with your answers and cover letters written by Claude from your own
facts. It submits only on the sites you allow, and only for roles you picked.

Everything about you stays in your own private copy. Claude runs on your own Claude subscription.

## What you need

- **A computer** (Windows or Mac) that can stay on while you apply.
- **A GitHub account** (free).
- **Python 3.11 or newer.**
  - Windows: get it from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"** during setup.
  - Mac: `brew install python`, or use the same site.
- **Claude Code**, signed in with a Claude plan that includes it. You can use the Claude desktop app,
  the VS Code extension or the installer: https://claude.com/claude-code
- **Chrome, Edge or Brave**, for the browser helper.

## Set up (once, about 20 minutes)

1. **Make your private copy.** On this repository's GitHub page, click **Use this template → Create a new
   repository** and choose **Private**. It will hold your resume, answers and applications, so it must stay
   private.
2. **Download it.**
   - The easy way: install GitHub Desktop (https://desktop.github.com), sign in, and choose
     **File → Clone repository**.
   - Or from a terminal: `git clone <your repo's URL>`.
3. **Install the bot.** In the folder, double-click **Install jobbot** (`Install jobbot.bat` on Windows,
   `Install jobbot.command` on Mac; the first time on a Mac, right-click → Open). It sets up a private
   Python environment and a browser the bot can drive.
4. **Start it.** Double-click **Start jobbot**. A page opens in your browser: the setup wizard.
   - Fill in each step. The Claude buttons read your resume, turn "the jobs I want" into search rules,
     and suggest companies, finding their job boards.
   - Read what Claude wrote, fix anything wrong, and click **Save**.
5. **Send your profile to GitHub.** In GitHub Desktop, commit everything and click **Push**. The nightly job
   search runs on GitHub, so it needs your profile there.
6. **Turn on the nightly search.** On your repo's GitHub page:
   - Open **Actions** and enable workflows.
   - Open **Nightly scrape** and click **Run workflow**.

   After that, it runs every night by itself.
7. **Install the browser helper** for Workday and other sites the bot can't do:
   - Open `chrome://extensions` (or `edge://extensions`) and turn on **Developer mode**.
   - Click **Load unpacked** and choose the `extension` folder.
   - On an application page, press **Alt+J**.

   More details are in `extension/README.md`.

Can't do these steps yourself? Open this folder in Claude Code and type `/setup`. Claude will walk you
through them and run the commands for you.

## Every day

1. **Start jobbot.** It pulls in last night's jobs and checks each new one against your profile.
2. **To review:** pick the roles you want. Each shows a fit verdict ("good fit", "stretch") with a reason.
3. **To apply:** the bot fills Greenhouse, Ashby and Lever applications.
   - **Until you tick a site under "Submit for real on"**, it only does dry runs: fills the form, saves a
     screenshot, and stops. Check a few screenshots before you tick a site.
   - **Other sites (Workday, company portals)** move to **Needs you**. Open them in your browser and press
     **Alt+J**. The helper fills what it can, and you click Submit.
4. **Answers:** questions jobbot couldn't answer wait here. Answer each once, and it reuses your answer
   from then on.
5. **Commit and push** your tracker now and then (GitHub Desktop: Commit, then Push). It's your history.

## Change your profile

Click **Profile & setup** on the dashboard. The files behind it are in `profile/`:
- `answers.toml`: your form answers
- `facts.md`: what Claude may say about you
- `search.toml`: your search rules
- `companies.json`: the companies it checks

After changing `facts.md`, re-check fit on the To review tab.

## Updates

Double-click **Update jobbot** to get the latest version. The first time, it asks for this template
repository's URL: run `python -m jobbot update --from <URL>` once (or ask Claude Code to). Your
`profile/` and tracker are never touched by updates.

## Ground rules jobbot keeps

- **Submits only roles you picked**, and only on sites you allowed, within daily limits.
- **Work authorization, salary and demographic answers come only from your own answers.** Claude never
  writes them.
- **Claude writes only from your facts file.** Anything it can't answer from your facts comes to you.
- **CAPTCHAs, logins, account creation and agreements you haven't allowed come to you.** It never gets
  around them.
- **No LinkedIn, Indeed or Handshake automation.** It uses company job boards only.
