"""fetch.main hands add_ratings every team's real ESPN start (handoff 755): on a Monday morning the last game is last
week's, outside the rating's schedule, so the lines check must see ESPN's start, not the 23:00 UTC fallback."""
from datetime import datetime, timezone

import leads
import test_fetch_main as M
from fake_espn import ET, League, sp_of


def test_monday_lines_check_gets_last_weeks_real_starts(monkeypatch, tmp_path):
    now = datetime(2026, 10, 12, 9, 30, tzinfo=ET).astimezone(timezone.utc)
    seen = []
    orig = leads.mark_lineups
    monkeypatch.setattr(leads, "mark_lineups", lambda players, lineups, abbrevs, last: (seen.append(dict(last)), orig(players, lineups, abbrevs, last))[1])
    lg = League(sp_of(now.astimezone(ET).date()), start_et=22)  # 10 PM ET = 02:00 UTC the next day
    d = M.run_fetch(monkeypatch, tmp_path, now, league=lg)
    assert d["rated"] and seen
    last = seen[-1]
    assert last, "no team's last game handed to the lines check"
    for team, start in last.items():
        assert not start.endswith("T23:00:00Z"), (team, start)
        assert start[11:13] == "02", (team, start)
