"""Reads profile/answers.toml. Ranking needs only the salary numbers today; the
application bot (Day 2) reads the rest."""

from __future__ import annotations


try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

DEFAULT_SALARY_FLOOR = 110_000
DEFAULT_SALARY_TARGET = 150_000


def load_answers() -> dict:
    """profile/answers.toml (see jobbot/profile.py)."""
    from jobbot import profile as person
    return person.answers()


def salary_bounds() -> tuple[int, int]:
    prefs = load_answers().get("preferences", {})
    return (int(prefs.get("salary_floor", DEFAULT_SALARY_FLOOR)),
            int(prefs.get("salary_target", DEFAULT_SALARY_TARGET)))
