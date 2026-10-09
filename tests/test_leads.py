"""Daily Faceoff page parsing in leads.py, on a canned page."""
import json

import pytest

import leads


class Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, timeout=None, **kw):
        for key, props in self.pages.items():
            if key in url:
                body = json.dumps({"props": {"pageProps": props}})
                return Resp(f'<html><script id="__NEXT_DATA__" type="application/json">{body}</script></html>')
        return Resp("<html>nothing here</html>")


@pytest.fixture(autouse=True)
def clear_teams():
    leads.NHL_TEAM.clear()
    yield
    leads.NHL_TEAM.clear()


def test_fetch_starters_maps_teams_and_both_sides():
    s = FakeSession({
        "line-combinations": {"sortedTeams": [{"name": "Toronto Maple Leafs", "shortName": "TOR"},
                                              {"name": "Nashville Predators", "shortName": "NAS"}]},
        "starting-goalies": {"date": "2026-10-09", "data": [{
            "homeTeamName": "Toronto Maple Leafs", "awayTeamName": "Nashville Predators",
            "homeGoalieName": "Anthony Stolarz", "awayGoalieName": "Juuse Saros",
            "homeNewsStrengthName": "Confirmed", "awayNewsStrengthName": "Likely", "dateGmt": "2026-10-09T23:00:00Z"}]},
    })
    from datetime import date
    out = leads.fetch_starters(s, date(2026, 10, 9))
    assert out["date"] == "2026-10-09"
    by_team = {g["team"]: g for g in out["games"]}
    assert by_team["TOR"]["goalie"] == "Anthony Stolarz" and by_team["TOR"]["status"] == "Confirmed" and by_team["TOR"]["home"]
    assert by_team["NSH"]["opp"] == "TOR" and by_team["NSH"]["status"] == "Likely"  # Daily Faceoff's NAS is the NHL's NSH


def test_page_without_data_raises():
    with pytest.raises(ValueError):
        leads.page_data(FakeSession({}), "https://example.com/x")


# The day-of log (data/log/*.json.gz) is scored against actual points weeks later (findings section 25),
# so its shape has to stay stable even though nothing reads it day to day.
LOG_KEYS = {"run_at", "starters", "errors", "odds", "injuries", "lineups", "players"}
LOG_PLAYER_KEYS = {"id", "nhl_id", "name", "team", "pos", "owner", "injury", "proj_ppg", "games", "games_left",
                   "cr", "cr_fpg", "cr_dress", "cr_games"}


def read_log(path):
    import gzip
    with gzip.open(path, "rt") as f:
        return json.load(f)


def test_write_log_records_feeds_and_survives_failures(monkeypatch, tmp_path):
    from datetime import datetime
    monkeypatch.setattr(leads, "LOG_DIR", tmp_path)
    monkeypatch.setattr(leads, "fetch_lineups", lambda s: {"TOR": {"players": [["Auston Matthews", "f1", None, False]]}})
    def down(s):
        raise ConnectionError("ESPN down")
    monkeypatch.setattr(leads, "fetch_injuries", down)
    players = [{"id": 1, "nhl_id": 8479318, "name": "Auston Matthews", "team": "TOR", "pos": "C", "owner": "other", "owned": 99.9,
                "injury": None, "proj_ppg": 4.1, "games": 4, "games_left": 2, "cr": 7.9, "cr_fpg": 4.0, "cr_dress": 0.98,
                "cr_games": 1.96, "cr_why": {"not": "logged"}}]
    leads.write_log(datetime(2026, 10, 9, 12, 30), players, [{"date": "2026-10-09", "games": []}], {"starters 2026-10-10": "x"},
                    None, odds={"games": []})
    (path,) = tmp_path.glob("*.json.gz")
    assert path.name == "2026-10-09-1230.json.gz"
    rec = read_log(path)
    assert LOG_KEYS - {"injuries"} <= set(rec)
    assert "ESPN down" in rec["errors"]["injuries"] and "starters 2026-10-10" in rec["errors"]
    assert set(rec["players"][0]) >= LOG_PLAYER_KEYS and "cr_why" not in rec["players"][0]


def test_latest_committed_log_has_the_scored_fields():
    from conftest import ROOT
    logs = sorted((ROOT / "data" / "log").glob("*.json.gz"))
    if not logs:
        pytest.skip("no logs yet")
    rec = read_log(logs[-1])
    assert LOG_KEYS <= set(rec), LOG_KEYS - set(rec)
    assert rec["players"] and LOG_PLAYER_KEYS <= set(rec["players"][0])


def test_league_log_keeps_only_league_fields():
    """Transactions and rosters for the manager simulation, without owner or account details."""
    tx = [{"id": "a1", "type": "FREEAGENT", "status": "EXECUTED", "teamId": 3, "scoringPeriodId": 4, "memberId": "{SECRET}",
           "items": [{"type": "ADD", "playerId": 10, "toTeamId": 3, "fromTeamId": 0, "extra": 1}]}]
    teams = [{"id": 3, "owners": ["{SECRET}"], "primaryOwner": "{SECRET}", "transactionCounter": {"matchupAcquisitionTotals": {"1": 2}},
              "roster": {"entries": [{"playerId": 10, "lineupSlotId": 3, "acquisitionType": "ADD", "playerPoolEntry": {"x": 1}}]}}]
    log = leads.league_log(tx, teams, 4)
    assert "SECRET" not in json.dumps(log)
    assert log["transactions"][0]["items"][0] == {"type": "ADD", "playerId": 10, "fromTeamId": 0, "toTeamId": 3,
                                                   "fromLineupSlotId": None, "toLineupSlotId": None, "isKeeper": None}
    assert log["rosters"]["3"][0]["lineupSlotId"] == 3 and log["moves_used"]["3"] == {"1": 2}


def test_mark_lineups_flags_skaters_left_out_of_daily_faceoffs_lines():
    players = [{"name": "Tim Stützle", "team": "OTT", "pos": "C"}, {"name": "Scratch Guy", "team": "OTT", "pos": "D"},
               {"name": "Hurt Guy", "team": "OTT", "pos": "LW"}, {"name": "Other", "team": "SJ", "pos": "C"}]
    lineups = {"OTT": {"players": [["Tim Stutzle", "f1", None, False], ["Hurt Guy", "ir", "ir", False]]}, "SJS": {"error": "x"}}
    leads.mark_lineups(players, lineups, {"SJ": "SJS"})
    assert [p["dfo_out"] for p in players] == [False, True, True, None]
