"""Findings 565: after the night's first puck a free agent whose only game left is tonight's must stay in the Week's
lineup sums with the chance he had before the puck, so his teammates whose game hasn't started don't move."""
from datetime import datetime, timezone

import pytest

import rating
from test_add_ratings import FakeSession, espn, rate

TONIGHT = "2026-10-09"


class LastNightSession(FakeSession):
    """The fake league with tonight (Oct 9) as the week's last game: Sunday's TOR-BOS game is dropped."""

    def get(self, url, params=None, headers=None, timeout=None):
        r = super().get(url, params, headers, timeout)
        if "/schedule/" in url and isinstance(r.payload, dict) and "gameWeek" in r.payload:
            r.payload = {"gameWeek": [d for d in r.payload["gameWeek"] if d["date"] <= TONIGHT]}
        return r


def test_free_agents_last_game_stays_in_the_lineup_sums_after_the_first_puck(monkeypatch, tmp_path):
    starts = {TONIGHT: "2026-10-09T23:30:00Z"}  # TOR-BOS at 7:30 PM ET; another game started at 7
    first = "2026-10-09T23:00:00Z"
    monkeypatch.setattr(rating, "LINEUP_SPOTS", {False: 1, True: 1})  # few enough that BOS's forwards fill the spots
    rows_seen = {}
    real = rating.week_slack

    def spy(rows, *a, **k):
        rows_seen["rows"] = rows
        return real(rows, *a, **k)
    monkeypatch.setattr(rating, "week_slack", spy)

    def run(clock, fa_left):
        monkeypatch.setattr(rating, "utcnow", lambda: clock)
        # ESPN has the free agent OUT tonight: his game counts about 0, which the boosted share for players not on the site
        # (what main fell back to) ignores, as for the injured free agents on CAR Oct 24 and MIN Oct 25
        ps = [dict(espn(108, "Zack Bolduc", "BOS", "LW", injury="OUT", games_left=fa_left), owner="freeagent", today_start=first),
              dict(espn(103, "David Pastrnak", "BOS", "RW", games_left=1), owner="other", today_start=starts[TONIGHT]),
              dict(espn(101, "Auston Matthews", "TOR", "C", games_left=1), owner="mine", today_start=starts[TONIGHT])]
        out = rate(monkeypatch, tmp_path, ps, fake=LastNightSession(starts=starts))
        return out, {r["p"]["name"]: r for r in rows_seen["rows"]}
    before, rows0 = run(datetime(2026, 10, 9, 22, 55, tzinfo=timezone.utc), 1)
    after, rows1 = run(datetime(2026, 10, 9, 23, 10, tzinfo=timezone.utc), 0)
    # his game tonight no longer counts for him, but it still takes a lineup spot, at the chance from before the puck
    assert after["Zack Bolduc"]["cr"] == 0
    assert "Zack Bolduc" in rows1 and rows1["Zack Bolduc"]["dates"] == []
    (d, q, *_), = rows1["Zack Bolduc"]["others"]
    assert d == TONIGHT and q == pytest.approx(rows0["Zack Bolduc"]["per_game"][0])
    # his teammate's game hasn't started: nothing of his moves
    keys = ("cr", "cr_games", "cr_dress", "cr_dress_next")
    assert tuple(after["David Pastrnak"][k] for k in keys) == tuple(before["David Pastrnak"][k] for k in keys)
    assert after["David Pastrnak"]["cr_why"].get("week_slack") == before["David Pastrnak"]["cr_why"].get("week_slack")
