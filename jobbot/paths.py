"""Project paths. Set JOBBOT_HOME to point jobbot at a copy of the project (used for testing)."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("JOBBOT_HOME") or Path(__file__).resolve().parent.parent)
TRACKER_PATH = PROJECT_DIR / "tracker.csv"
POSTINGS_DIR = PROJECT_DIR / "scraping" / "postings"
OPEN_JOBS_PATH = PROJECT_DIR / "scraping" / "state" / "open_jobs.json"
ANSWERS_PATH = PROJECT_DIR / "profile" / "answers.toml"
WEB_DIR = Path(__file__).resolve().parent / "web"
