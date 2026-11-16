"""jobbot command line.

  python3 -m jobbot serve        sync, check stale links in the background, open the dashboard
  python3 -m jobbot sync         pull the nightly scrape into tracker.csv
  python3 -m jobbot check        check links now: close dead postings, fill in posted dates
  python3 -m jobbot fit          screen roles against your profile (good / stretch / poor)
  python3 -m jobbot top -n 20    print the best-ranked fresh, eligible roles
  python3 -m jobbot add URL      add a job by link (goes straight to To apply)
  python3 -m jobbot install      set up the application bot (.venv with Playwright + Chromium)
  python3 -m jobbot update       pull the latest jobbot from the template repo
  python3 -m jobbot template DIR copy the shared code (no personal files) into DIR, the template repo

  .venv/bin/python -m jobbot apply --dry-run   fill forms for To apply rows, screenshot, don't submit
  .venv/bin/python -m jobbot apply             submit, for handlers listed in profile/bot.toml
"""

from __future__ import annotations

import argparse
import sys


def _print_sync(report: dict) -> None:
    print(report["git"])
    print(f"tracker: {report['rows_before']} -> {report['rows_after']} rows "
          f"(+{report['imported']} new, {report['duplicates_merged']} duplicates merged, "
          f"{report['closed']} marked closed, {report['dates_fixed']} dates fixed)")


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print(f"jobbot needs Python 3.11 or newer (this is {sys.version.split()[0]}). "
              "Get it from https://www.python.org/downloads/")
        return 1
    parser = argparse.ArgumentParser(prog="jobbot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="import the nightly scrape into tracker.csv")
    p_sync.add_argument("--no-pull", action="store_true", help="skip git pull")

    p_serve = sub.add_parser("serve", help="sync, then open the dashboard")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--no-sync", action="store_true")
    p_serve.add_argument("--no-pull", action="store_true")
    p_serve.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    p_serve.add_argument("--no-check", action="store_true", help="skip the background link check")
    p_serve.add_argument("--no-fit", action="store_true", help="skip the background fit check")

    p_check = sub.add_parser("check", help="check links of open rows")
    p_check.add_argument("--all", action="store_true", help="recheck rows checked in the last week too")
    p_check.add_argument("--limit", type=int, default=None, help="check at most N rows")
    p_check.add_argument("--dry-run", action="store_true", help="report only; don't change tracker.csv")

    p_fit = sub.add_parser("fit", help="screen open roles against profile/facts.md")
    p_fit.add_argument("--recheck", action="store_true", help="screen rows that already have a verdict too")
    p_fit.add_argument("--limit", type=int, default=None, help="screen at most N rows (best-ranked first)")
    p_fit.add_argument("--days", type=int, default=30, help="posted within N days (0 = any)")
    p_fit.add_argument("--rules-only", action="store_true", help="years/sponsorship rules only, no Claude")
    p_fit.add_argument("--dry-run", action="store_true", help="print verdicts; don't change tracker.csv")

    p_top = sub.add_parser("top", help="print the best-ranked roles")
    p_top.add_argument("-n", type=int, default=20)
    p_top.add_argument("--status", default="to_review")
    p_top.add_argument("--days", type=int, default=30, help="posted within N days (0 = any)")
    p_top.add_argument("--all-fits", action="store_true", help="include roles the fit check marked poor")

    p_apply = sub.add_parser("apply", help="run the application bot over To apply rows (needs .venv)")
    p_apply.add_argument("--dry-run", action="store_true", help="fill and screenshot only, even for live handlers")
    p_apply.add_argument("--limit", type=int, default=None, help="handle at most N applications")
    p_apply.add_argument("--id", action="append", default=[], help="only this job id (repeatable)")
    p_apply.add_argument("--headed", action="store_true", help="show the browser window")

    sub.add_parser("install", help="set up the application bot (.venv with Playwright + Chromium)")
    p_update = sub.add_parser("update", help="pull the latest jobbot from the template repo")
    p_update.add_argument("--from", dest="url", default=None, help="the template repo's URL (first time)")
    p_template = sub.add_parser("template", help="copy the shared code into a template repo folder")
    p_template.add_argument("dir")

    p_add = sub.add_parser("add", help="add a job by link")
    p_add.add_argument("url")
    p_add.add_argument("--company", default="")
    p_add.add_argument("--title", default="")
    p_add.add_argument("--location", default="")

    args = parser.parse_args(argv)
    if args.cmd in ("install", "update", "template"):
        from jobbot import maintain
        if args.cmd == "install":
            return maintain.install()
        return maintain.update(args.url) if args.cmd == "update" else maintain.template(args.dir)
    from jobbot import rank, tracker  # imported late so --help stays fast

    if args.cmd == "sync":
        _print_sync(tracker.sync(pull=not args.no_pull))
    elif args.cmd == "serve":
        if not args.no_sync:
            _print_sync(tracker.sync(pull=not args.no_pull))
        from jobbot.server import serve
        serve(port=args.port, open_browser=not args.no_open, check_links=not args.no_check,
              screen_fit=not args.no_fit)
    elif args.cmd == "check":
        from jobbot import linkcheck
        s = linkcheck.run(check_all=args.all, limit=args.limit, apply=not args.dry_run)
        print(f"checked {s['checked']}: {s['open']} open, {s['closed']} closed, "
              f"{s['unknown']} couldn't be read")
        print(f"tracker: {s['marked_closed']} marked closed, {s['posted_dates_filled']} posted dates filled"
              + (" (dry run, nothing written)" if args.dry_run else ""))
        for reason, n in s["closed_reasons"]:
            print(f"  {n:>4}  {reason}")
    elif args.cmd == "fit":
        from jobbot import fit
        rows = {tracker.job_id(r): r for r in tracker.load_rows()}
        s = fit.run(recheck=args.recheck, limit=args.limit, days=args.days or None,
                    use_claude=not args.rules_only, apply=not args.dry_run)
        for verdict in ("good", "stretch", "poor"):
            for jid, (v, reason) in s["results"].items():
                if v == verdict and jid in rows:
                    r = rows[jid]
                    print(f"  {v:<7} {r['company'][:18]:<18} {r['role_title'][:48]:<48} {reason}")
        print(f"screened {s['screened']}: {s['good']} good, {s['stretch']} stretch, {s['poor']} poor"
              + (f", {s['unscreened']} not screened" if s["unscreened"] else "")
              + (" (dry run, nothing written)" if args.dry_run else ""))
        if s["error"]:
            print(f"Claude error: {s['error']}")
    elif args.cmd == "top":
        views = rank.ranked(tracker.load_rows(), {args.status})
        views = [v for v in views if not v["below_floor"] and v["eligible"]
                 and (args.all_fits or v["fit"] != "poor")
                 and (not args.days or v["age_days"] <= args.days)][: args.n]
        for v in views:
            auto = "auto" if v["auto"] else v["ats"]
            print(f"{v['score']:>4}  {v['company'][:22]:<22} {v['title'][:46]:<46} "
                  f"{v['tier_label']:<17} {auto:<10} {(v['fit'] or '-'):<8} {v['salary'][:22]}")
    elif args.cmd == "apply":
        try:
            from jobbot.apply import engine
        except ImportError as e:
            print(f"The bot needs Playwright: run `python -m jobbot install`, then use the To apply tab's "
                  f"Run bot button (or .venv/bin/python -m jobbot apply; Windows: .venv\\Scripts\\python -m jobbot apply). ({e})")
            return 1
        outcomes = engine.run(dry_run=args.dry_run, limit=args.limit, job_ids=set(args.id) or None,
                              headed=args.headed or None)
        counts: dict[str, int] = {}
        for o in outcomes:
            counts[o.result] = counts.get(o.result, 0) + 1
        print("done: " + (", ".join(f"{n} {k.replace('_', ' ')}" for k, n in counts.items()) or "nothing in To apply"))
    elif args.cmd == "add":
        from jobbot.server import add_job
        job, message = add_job(args.url, args.company, args.title, args.location)
        print(f"{job['company']} — {job['title']} ({job['location'] or 'location unknown'}): {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
