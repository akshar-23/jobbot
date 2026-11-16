"""Would this row pass the scraper's filters today?

Old rows were added under looser rules. Rather than keep a second copy of the
rules, this loads them from scraping/scraper.py, so tightening a filter there
also cleans up the dashboard.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from jobbot import profile as person

SCRAPER_PATH = Path(__file__).resolve().parent.parent / "scraping" / "scraper.py"


_loaded: dict = {}


def _rules():
    """The scraper module, reloaded when profile/search.toml or answers.toml changes."""
    stamp = (person.search_mtime(), person.path("answers.toml").stat().st_mtime if person.path("answers.toml").exists() else 0)
    if _loaded.get("stamp") != stamp:
        spec = importlib.util.spec_from_file_location("jobbot_scraper_rules", SCRAPER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _loaded.update(stamp=stamp, module=module)
    return _loaded["module"]


def check(row: dict) -> tuple[bool, str]:
    """(eligible, reason it isn't). Uses title, company and location only."""
    rules = _rules()
    ok, why = rules.title_verdict(row["role_title"], row["company"])
    if not ok:
        return False, why
    verdict, reason = rules.location_verdict(row["location"])
    if verdict == "nonus":
        return False, reason
    rule = rules.COMPANY_RULES.get(row["company"])
    if rule and rule["new_grad"]:
        posting = rules.Posting(company=row["company"], role_title=row["role_title"], jd_url=row["jd_url"])
        if not rules.has_new_grad_signal(posting):
            return False, f"{row['company']}: no new-grad signal in title"
    why = rules.blocks_you(row["role_title"])
    if why:
        return False, f"title: {why}"
    return True, ""
