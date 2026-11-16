"""Local dashboard: pick roles to apply to, skip the rest, add jobs by link.

Standard library only. Listens on 127.0.0.1. Write requests must carry an
X-Jobbot header (other websites can't send it) and a localhost Host header
(blocks DNS-rebinding tricks).
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from jobbot import assist, ats, fit, linkcheck, rank, runner, saved, tracker
from jobbot.apply import draft as drafter
from jobbot.common import TRACKER_COLUMNS
from jobbot.paths import WEB_DIR

# Statuses the dashboard may set. "applied" is for applications sent by hand;
# the bot (Day 2) sets applied / needs_manual itself.
SETTABLE = {"to_review", "to_apply", "skipped", "applied"}
TAB_STATUSES = {
    "to_review": {"to_review"},
    "to_apply": {"to_apply"},
    "needs_manual": {"needs_manual"},
    "applied": {"applied", "interviewing", "offer", "rejected", "ghosted"},
    "done": {"skipped", "closed"},
}
_LOCK = tracker.LOCK
CHECK = linkcheck.Progress()


def _code_stamp() -> float:
    """Newest change to jobbot's own code. If it's newer than when this server started, the page
    asks you to restart: an old server can't serve new features (the security-code box, …)."""
    root = Path(__file__).resolve().parent
    return max((f.stat().st_mtime for f in root.rglob("*.py")), default=0.0)


STARTED_STAMP = _code_stamp()
FIT = fit.Progress()


def start_link_check(then_fit: bool = False) -> bool:
    """Check stale links in a background thread (then screen fit). False if already running."""
    if CHECK.running:
        return False
    CHECK.running = True

    def work() -> None:
        try:
            linkcheck.run(progress=CHECK, log=lambda _msg: None)
        finally:
            CHECK.running = False
        if then_fit:  # after the link check, so closed postings aren't screened
            start_fit_check()

    threading.Thread(target=work, daemon=True).start()
    return True


def start_fit_check() -> bool:
    """Screen unscreened roles in a background thread. False if one is already running."""
    if FIT.running:
        return False
    FIT.running = True

    def work() -> None:
        try:
            fit.run(progress=FIT, log=lambda _msg: None)
        except Exception as e:  # shown on the dashboard's status line
            FIT.running, FIT.error = False, f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()
    return True


def add_job(url: str, company: str = "", title: str = "", location: str = "") -> tuple[dict, str]:
    """Add a job by link as to_apply. Returns (row view, message)."""
    found = ats.fetch_metadata(url)
    row = {c: "" for c in TRACKER_COLUMNS}
    row.update(
        date_added=date.today().isoformat(),
        company=company or found.get("company", ""),
        role_title=title or found.get("role_title", ""),
        jd_url=url,
        location=location or found.get("location", ""),
        salary_range=found.get("salary_range", ""),
        sponsors_h1b="unclear",
        status="to_apply",
        notes="src:manual",
    )
    with _LOCK:
        rows = tracker.load_rows()
        key = tracker.row_key(row)
        for existing in rows:
            if tracker.row_key(existing) == key:
                if existing["status"] == "to_review":
                    existing["status"] = "to_apply"
                    tracker.save_rows(rows)
                    return rank.ranked([existing])[0], "Already tracked; moved to To apply."
                return rank.ranked([existing])[0], f"Already tracked (status: {existing['status']})."
        rows.append(row)
        tracker.save_rows(rows)
    note = found.get("error") or ("" if row["company"] and row["role_title"]
                                  else "Couldn't read company/title; fill them in.")
    return rank.ranked([row])[0], note or "Added to To apply."


class Handler(BaseHTTPRequestHandler):
    server_version = "jobbot"

    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _local(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self) -> None:
        if not self._local():
            return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        parts = urlsplit(self.path)
        if parts.path.startswith("/api/ext/"):
            # Extension endpoints return personal data: same header check as writes, so a
            # web page can't read them (a custom header forces a CORS preflight we never allow).
            if self.headers.get("X-Jobbot") != "1":
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            query = parse_qs(parts.query)
            if parts.path == "/api/ext/ping":
                return self._json({"ok": True})
            if parts.path == "/api/ext/file":
                path = assist.file_for(query.get("kind", [""])[0], query.get("url", [""])[0])
                if not path:
                    return self._json({"error": "no such file"}, HTTPStatus.NOT_FOUND)
                return self._send(200, path.read_bytes(), "application/pdf")
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        if parts.path == "/":
            from jobbot import profile as person
            if not person.is_set_up():  # a fresh copy: the setup wizard first
                return self._send(200, (WEB_DIR / "setup.html").read_bytes(), "text/html; charset=utf-8")
            return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if parts.path == "/setup":
            return self._send(200, (WEB_DIR / "setup.html").read_bytes(), "text/html; charset=utf-8")
        if parts.path == "/api/profile":
            from jobbot import profile_io
            return self._json(profile_io.snapshot())
        if parts.path == "/api/jobs":
            tab = parse_qs(parts.query).get("tab", ["to_review"])[0]
            rows = tracker.load_rows()
            jobs = rank.ranked(rows, TAB_STATUSES.get(tab, {"to_review"}))
            if tab in ("to_apply", "needs_manual"):
                from jobbot import resolve
                for j in jobs:  # the bot found the form's own address (often not the posting's page)
                    j["form_url"] = _form_url(j["id"])
                    if j["status"] == "needs_manual":
                        j["resolvable"] = resolve.answerable_count(j["id"])
            return self._json(jobs)
        if parts.path == "/api/summary":
            counts: dict[str, int] = {}
            for row in tracker.load_rows():
                for tab, statuses in TAB_STATUSES.items():
                    if row["status"] in statuses:
                        counts[tab] = counts.get(tab, 0) + 1
            return self._json(counts)
        if parts.path == "/api/check":
            return self._json(vars(CHECK))
        if parts.path == "/api/fit":
            return self._json(vars(FIT))
        if parts.path == "/api/meta":
            from jobbot import profile as person
            names = [name for _, name, _ in rank.location_tiers()]
            return self._json({"name": person.full_name(), "set_up": person.is_set_up(),
                               "outdated": _code_stamp() > STARTED_STAMP + 1,
                               "tiers": names + ["Other US / remote", "Unclear"]})
        if parts.path == "/api/answers":
            return self._json({"saved": saved.load(), "pending": saved.pending()})
        if parts.path == "/api/settings":
            return self._json(runner.bot_settings())
        if parts.path == "/api/apply":
            return self._json(runner.STATE.public())
        if parts.path == "/api/codes":
            from jobbot import codes
            return self._json(codes.waiting())
        if parts.path == "/api/resolve":
            from jobbot import resolve
            info = resolve.items(parse_qs(parts.query).get("id", [""])[0])
            return self._json(info or {"error": "no bot run for this job"}, 200 if info else HTTPStatus.NOT_FOUND)
        if parts.path == "/api/shot":
            jid = parse_qs(parts.query).get("id", [""])[0]
            shot = _latest_shot(jid)
            if not shot:
                return self._json({"error": "no screenshot"}, HTTPStatus.NOT_FOUND)
            return self._send(200, shot.read_bytes(), "image/png")
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _profile_post(self, path: str, body: dict) -> None:
        from jobbot.setup_ai import SetupAIError
        try:
            return self._json(_profile_routes(path, body))
        except LookupError:
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (SetupAIError, ValueError) as e:
            return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:  # always answer the page
            return self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        if not self._local() or self.headers.get("X-Jobbot") != "1":
            return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad request"}, HTTPStatus.BAD_REQUEST)
        path = urlsplit(self.path).path

        if path.startswith("/api/ext/"):
            try:
                if path == "/api/ext/plan":
                    return self._json(assist.plan(body))
                if path == "/api/ext/cover-letter":
                    return self._json({"cover_letter": assist.cover_letter(body)})
                if path == "/api/ext/applied":
                    return self._json({"message": assist.mark_applied(body)})
                if path == "/api/ext/remember":
                    return self._json(assist.remember(body))
            except drafter.DraftError as e:
                return self._json({"error": f"Drafting failed: {e}"}, HTTPStatus.BAD_GATEWAY)
            except Exception as e:  # always answer the extension, never leave it waiting
                return self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        if path == "/api/status":
            ids, status = set(body.get("ids") or []), body.get("status")
            if status not in SETTABLE or not ids:
                return self._json({"error": "bad status or no ids"}, HTTPStatus.BAD_REQUEST)
            with _LOCK:
                rows = tracker.load_rows()
                updated = 0
                for row in rows:
                    if tracker.job_id(row) in ids:
                        row["status"] = status
                        if status == "applied" and not row["date_applied"]:
                            row["date_applied"] = date.today().isoformat()
                        updated += 1
                tracker.save_rows(rows)
            message = ""
            if status == "to_apply" and runner.bot_settings()["auto_run_on_pick"]:
                _, message = runner.start(sorted(ids))
            return self._json({"updated": updated, "bot": message})

        if path == "/api/add":
            url = (body.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return self._json({"error": "Paste a full link starting with https://"}, HTTPStatus.BAD_REQUEST)
            if ats.provider(url) in ("linkedin", "handshake"):
                return self._json({"error": "That's a LinkedIn/Handshake link. Open the job, click "
                                            "Apply, and paste the company-site link instead."},
                                  HTTPStatus.BAD_REQUEST)
            job, message = add_job(url, (body.get("company") or "").strip(),
                                   (body.get("title") or "").strip(), (body.get("location") or "").strip())
            if job.get("status") == "to_apply" and runner.bot_settings()["auto_run_on_pick"]:
                runner.start([job["id"]])
                message += " The bot is on it."
            return self._json({"job": job, "message": message})

        if path == "/api/check/start":
            return self._json({"started": start_link_check()})
        if path == "/api/fit/start":
            return self._json({"started": start_fit_check()})
        if path == "/api/apply/start":
            ids = [str(i) for i in body.get("ids") or []] or None
            ok, message = runner.start(ids, dry_run=bool(body.get("dry_run")))
            return self._json({"started": ok, "message": message}, 200 if ok else HTTPStatus.CONFLICT)
        if path == "/api/settings":
            return self._json(runner.set_bot_settings(body))
        if path == "/api/window/front":
            return self._json({"shown": runner.show_bot_window()})
        if path == "/api/resolve":
            from jobbot import resolve
            jid = str(body.get("id", ""))
            try:
                message = resolve.apply(jid, body)
            except ValueError as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            started, run_msg = runner.start([jid])
            return self._json({"message": message + (" The bot is on it." if started else f" {run_msg}")})
        if path == "/api/codes":
            from jobbot import codes
            try:
                codes.provide(str(body.get("id", "")), str(body.get("code", "")))
            except ValueError as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            return self._json({"ok": True})
        if path.startswith("/api/profile"):
            return self._profile_post(path, body)
        if path == "/api/answers/save":
            ok, why = saved.remember((body.get("question") or "").strip(), body.get("answer"),
                                     kind=body.get("kind") or "text", options=body.get("options") or [],
                                     company=(body.get("company") or "").strip(),
                                     only_this_company=bool(body.get("only_this_company")) or None)
            return self._json({"saved": ok, "why": why}, 200 if ok else HTTPStatus.BAD_REQUEST)
        if path == "/api/answers/delete":
            return self._json({"deleted": saved.forget(body.get("key", ""), body.get("company", ""))})

        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def _profile_routes(path: str, body: dict) -> dict:
    """The setup wizard's saves and its Claude helpers (jobbot/profile_io.py, jobbot/setup_ai.py)."""
    import base64
    from jobbot import boards, profile_io, setup_ai
    from jobbot.paths import PROJECT_DIR
    if path == "/api/profile":
        return {"written": profile_io.save(body)}
    if path == "/api/profile/resume":
        return {"path": profile_io.save_resume(base64.b64decode(body.get("data", "")),
                                               body.get("first_name", ""), body.get("last_name", ""))}
    if path == "/api/profile/board":
        return {"boards": boards.probe((body.get("name") or "").strip())}
    cities = [c for c in body.get("cities") or [] if c]
    if path == "/api/profile/ai/resume":
        pdf = (PROJECT_DIR / (body.get("path") or "")).resolve()
        if PROJECT_DIR.resolve() not in pdf.parents or not pdf.is_file():
            raise ValueError("Upload your resume first.")
        return setup_ai.read_resume(pdf)
    if path == "/api/profile/ai/search":
        return setup_ai.suggest_search(body.get("facts", ""), body.get("goal", ""), cities, bool(body.get("needs_sponsorship")))
    if path == "/api/profile/ai/companies":
        return {"companies": setup_ai.suggest_companies(body.get("facts", ""), body.get("goal", ""), cities,
                                                        bool(body.get("needs_sponsorship")))}
    raise LookupError(path)


def _form_url(jid: str) -> str:
    """The application form's own address from the bot's latest plan for this job ("" if none)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", jid or ""):
        return ""
    for folder in sorted(assist.APPS_DIR.glob(f"*_{jid}"), reverse=True):
        try:
            return json.loads((folder / "plan.json").read_text(encoding="utf-8")).get("form", "") or ""
        except (OSError, ValueError):
            continue
    return ""


def _latest_shot(jid: str):
    """The newest dry-run or submit screenshot for a job id."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", jid or ""):
        return None
    for folder in sorted(assist.APPS_DIR.glob(f"*_{jid}"), reverse=True):
        for name in ("after_submit.png", "filled.png"):
            if (folder / name).is_file():
                return folder / name
    return None


def serve(port: int = 8765, open_browser: bool = True, check_links: bool = True,
          screen_fit: bool = True) -> None:
    from jobbot.apply.engine import load_bot_config
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"jobbot dashboard: {url}  (Ctrl+C to stop)")
    screen_fit = screen_fit and bool(load_bot_config().get("fit_on_serve", True))
    if check_links:
        start_link_check(then_fit=screen_fit)
        print("Checking links not checked in the last 7 days, in the background"
              + (", then screening new roles for fit." if screen_fit else "."))
    elif screen_fit:
        start_fit_check()
        print("Screening new roles for fit in the background.")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
