"""Checks for saved answers (jobbot/saved.py) and how fixed.py uses them.

Run after changing either: python3 tests/test_saved_answers.py
Uses a temporary file, never profile/saved_answers.json.
"""

from __future__ import annotations

import os
from pathlib import Path as _P
# Tests run against a fixed test profile, never your own (and never expose your details).
os.environ["JOBBOT_PROFILE"] = str(_P(__file__).resolve().parent / "fixtures" / "profile")
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobbot import saved  # noqa: E402
from jobbot.apply import fixed  # noqa: E402
from jobbot.apply.model import Answer, Blocker, Question  # noqa: E402

YN = ["Yes", "No"]
CFG = {"accept_privacy_notices": True, "accept_arbitration_agreements": False, "accept_other_agreements": False}


def q(label: str, kind: str = "select", options: list[str] | None = None) -> Question:
    return Question(key="k", label=label, kind=kind, required=True,
                    options=YN if options is None and kind == "select" else (options or []))


def main() -> int:
    failures: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}: got {got!r}, wanted {want!r}")

    with tempfile.TemporaryDirectory() as tmp:
        saved.PATH = Path(tmp) / "saved_answers.json"
        saved.APPS_DIR = Path(tmp) / "applications"

        check("key normalizes company", saved.question_key("Have you worked for Unity Technologies?*", "Unity Technologies"),
              "have you worked for {company}")
        check("key strips suffix", saved.question_key("Are you bound by a Unity non-compete?", "Unity Technologies Inc."),
              "are you bound by a {company} non compete")

        ok, _ = saved.remember("Are you currently bound by a non-compete agreement?", "No", kind="select",
                               options=YN, company="Unity Technologies")
        check("remember non-compete", ok, True)
        hit = fixed.resolve(q("Are you currently bound by a non-compete agreement? *"), {}, "Adobe", CFG)
        check("saved answer beats legal blocker", (type(hit).__name__, getattr(hit, "value", None)), ("Answer", "No"))

        ok, why = saved.remember("Do you agree to the arbitration agreement?", "Yes", company="Unity")
        check("agreements are never saved", ok, False)
        hit = fixed.resolve(q("Have you previously applied to Adobe before?"), {}, "Adobe", CFG)
        check("applied-before stays manual", type(hit).__name__, "Blocker")

        saved.remember("Why do you want to work at Unity?", "I've built on Unity for years and Unity's XR work…",
                       kind="textarea", company="Unity Technologies")
        check("company-specific text stays with the company",
              fixed.resolve(q("Why do you want to work at Adobe?", "textarea"), {}, "Adobe", CFG), None)
        hit = fixed.resolve(q("Why do you want to work at Unity?", "textarea"), {}, "Unity Technologies", CFG)
        check("company-specific text reused for the same company", isinstance(hit, Answer), True)

        saved.remember("What is your notice period at your current employer?", "Two weeks", kind="text", company="Unity")
        hit = fixed.resolve(q("What is your notice period at your current employer?", "select",
                              ["Immediately", "Two weeks", "One month"]), {}, "Adobe", CFG)
        check("saved text fitted to options", getattr(hit, "value", None), "Two weeks")
        hit = fixed.resolve(q("What is your notice period at your current employer?", "select", ["30 days", "60 days"]),
                            {}, "Adobe", CFG)
        check("no option fits -> not answered from saved", isinstance(hit, Answer) and hit.source.startswith("saved"), False)

        ok, _ = saved.remember("Do you consent to the use of an AI interview assistant to help with note-taking?",
                               "No", company="StubHub")
        check("consents are never saved", ok, False)

        check("forget", saved.forget("are you currently bound by a non compete agreement"), True)
        hit = fixed.resolve(q("Are you currently bound by a non-compete agreement?"), {}, "Adobe", CFG)
        check("after forget the blocker is back", isinstance(hit, Blocker), True)

    for f in failures:
        print("FAIL " + f)
    total = 13
    print(f"{total - len(failures)}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
