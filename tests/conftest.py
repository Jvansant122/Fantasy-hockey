import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(autouse=True)
def no_retry_waits(monkeypatch):
    """rating.nhl_get backs off between retries; the fakes fail on purpose, so don't wait for real."""
    import rating
    monkeypatch.setattr(rating.time, "sleep", lambda s: None)


@pytest.fixture(autouse=True)
def _rating_files_in_tmp(monkeypatch, tmp_path):
    """add_ratings writes data/injury_notes.json: tests write a temporary copy instead; never wait between roster retries."""
    import rating
    monkeypatch.setattr(rating, "INJURY_NOTES", tmp_path / "injury_notes.json")
    monkeypatch.setattr(rating, "ROSTER_WAITS", (0, 0, 0))
