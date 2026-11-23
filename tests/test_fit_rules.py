"""Checks for the title rules (scraping/scraper.py) and the fit check's rules (jobbot/fit.py).

Run after changing either: .venv/bin/python tests/test_fit_rules.py  (or plain python3)
"""

from __future__ import annotations

import os
from pathlib import Path as _P
# Tests run against a fixed test profile, never your own (and never expose your details).
os.environ["JOBBOT_PROFILE"] = str(_P(__file__).resolve().parent / "fixtures" / "profile")
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobbot import eligibility, fit  # noqa: E402
from jobbot.common import fit_tag, with_fit_tag  # noqa: E402

rules = eligibility._rules()

# (title, company, should pass)
TITLES = [
    ("Software Engineer I", "Twitch", True),
    ("Software Engineer II", "Stripe", True),
    ("Software Development Engineer II, Prime Video", "Amazon", True),
    ("Software Engineer IC2", "Microsoft", True),
    ("Gameplay Programmer", "Insomniac", True),
    ("Machine Learning Engineer, Recommendations", "Roblox", True),
    ("Software Engineer, Payments", "Stripe", True),
    ("Backend / API Engineer, Radar", "Stripe", True),          # Radar is a fraud product, not RF
    ("Software Dev Engineer I - Security Platform", "Twitch", True),
    ("Software Engineer, Reliability", "Roblox", True),
    ("Systems Engineer, DNS", "Cloudflare", True),
    ("Software Engineer III", "Google", False),
    ("Software Engineer IC3", "Microsoft", False),
    ("Senior Software Engineer", "Stripe", False),
    ("NVIDIA 2027 Internships: Software Engineering", "NVIDIA", False),
    ("Contract Student Worker - Software Engineer", "Zoox", False),
    ("Research Scientist, Robotics Research - PhD New College Grad 2026", "NVIDIA", False),
    ("GPU Verification Engineer - New College Grad 2026", "NVIDIA", False),
    ("Physical Design Engineer", "NVIDIA", False),
    ("Deep Learning Compiler Engineer", "NVIDIA", False),
    ("Site Reliability Engineer - Observability", "Adobe", False),
    ("Security Engineer, Privy", "Stripe", False),
    ("Data Center Network Operations Engineer", "Cloudflare", False),
    ("Data Scientist, Analytics Enablement", "OpenAI", True),    # the fit check decides
    ("Reporting and Analytics Developer", "Cooper Norcross", False),
    ("Data Scientist, FX Quantitative Research", "Stripe", False),
    ("Forward Deployed Engineer, Agentic Platform (Korea)", "Cohere", False),
    ("Customer Succes Engineer", "Adobe", False),
    ("CONTRACT - Gameplay Software Engineer", "Nintendo", False),
    ("Gameplay Programmer - Contract", "Studio", False),
    ("Smart Contract Engineer", "Coinbase", True),
    ("Expert Engineer, Security - Central Technology", "Activision", False),
    ("Founding Developer Tooling Engineer - Middle East", "Applied Intuition", False),
    ("Gameplay Engineer, Combat - STARCRAFT", "Blizzard Entertainment", True),
]

# (location, verdict)
LOCATIONS = [
    ("PERM - N/A", "nonus"),
    ("Stuttgart", "nonus"),
    ("KSA - RYD Garage", "nonus"),
    ("London - King - Ampersand; Barcelona - King - PG (United Kingdom)", "nonus"),
    ("Sunnyvale, CA", "us"),
    ("Irvine, CA", "us"),
]

# (job description text, minimum years required)
YEARS = [
    ("Minimum qualifications:\n- 3+ years of professional software experience\nPreferred:\n- 6+ years of experience with Go", 3),
    ("Preferred qualifications\n- 5+ years of industry experience", None),
    ("Basic Qualifications\n- 5+ years of non-internship professional software development experience\n"
     "Preferred Qualifications\n- 2+ years of experience", 5),
    ("You have 2-4 years of experience building backend systems", 2),
    ("Founded 10 years ago, we build games.", None),
    ("What we need to see:\nBS or MS with 8+ years of relevant experience or PhD with 5+ years of experience", 5),
]


def main() -> int:
    failures = 0
    for title, company, want in TITLES:
        ok, why = rules.title_verdict(title, company)
        if ok != want:
            failures += 1
            print(f"FAIL title {title!r}: got {'pass' if ok else 'drop'} ({why}), wanted {'pass' if want else 'drop'}")
    for loc, want in LOCATIONS:
        got = rules.location_verdict(loc)[0]
        if got != want:
            failures += 1
            print(f"FAIL location {loc!r}: got {got}, wanted {want}")
    for text, want in YEARS:
        got = fit.min_years(text)
        if got != want:
            failures += 1
            print(f"FAIL years {text[:50]!r}: got {got}, wanted {want}")

    notes = with_fit_tag("src:simplify; new-grad; fit: poor — old", "good", "C#/Unity tools; strong match")
    if fit_tag(notes) != ("good", "C#/Unity tools, strong match") or notes.count("fit:") != 1:
        failures += 1
        print(f"FAIL fit tag round trip: {notes!r}")
    if fit.with_hedge("good", "Unity match") != ("stretch", "may not sponsor H-1B · Unity match"):
        failures += 1
        print("FAIL hedge downgrade")
    hedge = ("the Company may not be able to employ candidates for this role who have United States work "
             "authorization related to certain U.S. visa categories, or support future H-1B sponsorship")
    if not fit.hedged(hedge) or fit.rule_verdict({"role_title": "Software Engineer"}, hedge):
        failures += 1
        print("FAIL hedge should stay visible")
    if fit.rule_verdict({"role_title": "SWE"}, "We are unable to sponsor work visas (e.g., F-1 OPT, H-1B).") is None:
        failures += 1
        print("FAIL explicit no-sponsorship should be poor")

    if fit._usable("Raydar - Current Openings") or not fit._usable(
            "About the role. " * 20 + "Requirements: 2+ years of experience with Python."):
        failures += 1
        print("FAIL junk-page check")

    total = len(TITLES) + len(LOCATIONS) + len(YEARS) + 5
    print(f"{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
