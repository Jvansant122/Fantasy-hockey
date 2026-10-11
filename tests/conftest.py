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


def reopen_tonight(d):
    """Sunday-evening data has every game of the week started, so nothing is left to rate. Tests that need games left
    put tonight's games back to before puck drop (skaters at their dress chance); other data comes back unchanged."""
    if any(p["games_left"] > 0 for p in d["players"]):
        return d
    tonight = next((n["sp"] for n in d["nights"] if not n["past"]), None)
    for p in d["players"]:
        if p["pos"] != "G" and tonight in p.get("nights", []) and p.get("today_start"):
            dress = p.get("cr_dress_next") or 0
            p.update(today_start=None, games_left=1, cr_games=dress, cr=round((p.get("cr_fpg") or 0) * dress, 2))
    return d
