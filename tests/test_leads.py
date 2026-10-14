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


def test_mark_lineups_matches_daily_faceoff_spellings():
    players = [{"name": "Gabe Perreault", "team": "NYR", "pos": "RW"}, {"name": "Elias Pettersson", "team": "VAN", "pos": "C"},
               {"name": "Elias N. Pettersson", "team": "VAN", "pos": "D"}, {"name": "Mason Perreault", "team": "NYR", "pos": "C"},
               {"name": "Jake Middleton", "team": "CGY", "pos": "D"}, {"name": "Jacob Smith", "team": "CGY", "pos": "C"}]
    lineups = {"NYR": {"players": [["Gabriel Perreault", "f1", None, False]]},
               "VAN": {"players": [["Elias Pettersson", "f1", None, False], ["Elias Nils Pettersson", "d3", None, False]]},
               "CGY": {"players": [["Jacob Middleton", "d2", None, False], ["Jake Smith", "f4", None, False],
                                   ["Jacob Smithers", "f2", None, False]]}}
    leads.mark_lineups(players, lineups, {})
    assert [p["dfo_out"] for p in players] == [False, False, False, True, False, False]
    # the defenceman scratched: the forward's exact name never stands in for him
    lineups["VAN"]["players"].pop()
    leads.mark_lineups(players, lineups, {})
    assert players[2]["dfo_out"] is True and players[1]["dfo_out"] is False


def test_mark_lineups_ignores_a_lineup_from_before_the_last_game():
    # posted for the game he then sat: it says nothing about the next one (findings 182)
    players = [{"name": "Emil Andrae", "team": "CBJ", "pos": "D"}, {"name": "Tim Stützle", "team": "OTT", "pos": "C"}]
    lineups = {"CBJ": {"updated": "2026-10-10T22:43:00.000Z", "players": [["Emil Andrae", "d3", None, False]]},
               "OTT": {"updated": "2026-10-11T15:00:00.000Z", "players": [["Tim Stutzle", "f1", None, False]]}}
    last = {"CBJ": "2026-10-10T23:00:00Z", "OTT": "2026-10-10T23:00:00Z"}
    leads.mark_lineups(players, lineups, {}, last)
    assert [p["dfo_out"] for p in players] == [None, False]
    leads.mark_lineups(players, lineups, {})  # no game times: every lineup counts
    assert [p["dfo_out"] for p in players] == [False, False]
    assert leads.older("2026-10-10T22:43:00.000Z", "2026-10-10T23:00:00Z") and not leads.older(None, "2026-10-10T23:00:00Z")


def test_last_logged_reuses_a_recent_good_copy(tmp_path, monkeypatch):
    """An injury or lineup feed outage reuses the newest logged copy under a day old (findings 230)."""
    import gzip
    from datetime import datetime
    monkeypatch.setattr(leads, "LOG_DIR", tmp_path)
    def log(name, rec):
        with gzip.open(tmp_path / f"{name}.json.gz", "wt") as f:
            json.dump(rec, f)
    log("2026-10-10-0610", {"injuries": {"old": 1}, "errors": {}})
    log("2026-10-11-0910", {"injuries": {"new": 1}, "errors": {}})
    log("2026-10-11-1240", {"errors": {"injuries": "boom"}})
    now = datetime(2026, 10, 11, 17, 10)
    assert leads.last_logged("injuries", now) == {"new": 1}
    assert leads.last_logged("injuries", datetime(2026, 10, 12, 12, 0)) is None  # older than a day
    assert leads.last_logged("lineups", now) is None


class JsonSession:
    def __init__(self, by_url):
        self.by_url, self.urls = by_url, []

    def get(self, url, timeout=None, params=None, **kw):
        self.urls.append(url)
        body = next(v for k, v in self.by_url.items() if k in url)
        r = Resp(json.dumps(body))
        r.json = lambda: body
        return r


NHL_YESTERDAY = {"currentOddsDate": "2026-10-11", "lastUpdatedUTC": "2026-10-11T16:00:00Z", "games": [
    {"gameId": 1, "startTimeUTC": "2026-10-11T23:00:00Z",
     "homeTeam": {"abbrev": "TOR", "odds": [{"description": "MONEY_LINE_2_WAY", "value": -150.0}]},
     "awayTeam": {"abbrev": "BOS", "odds": [{"description": "MONEY_LINE_2_WAY", "value": 130.0}]}}]}
ESPN_TODAY = {"events": [
    {"id": "9", "date": "2026-10-12T23:00Z", "competitions": [{
        "competitors": [{"homeAway": "home", "team": {"abbreviation": "NJ"}}, {"homeAway": "away", "team": {"abbreviation": "OTT"}}],
        "odds": [{"moneyline": {"home": {"close": {"odds": "-130"}}, "away": {"close": {"odds": "EVEN"}}}}]}]},
    {"id": "10", "date": "2026-10-13T00:00Z", "competitions": [{  # no line posted yet: left out
        "competitors": [{"homeAway": "home", "team": {"abbreviation": "MIN"}}, {"homeAway": "away", "team": {"abbreviation": "VGK"}}]}]}]}


def test_morning_odds_come_from_espn_until_the_nhl_feed_rolls_over():
    """The NHL partner feed shows yesterday's lines until about noon ET; morning runs take today's from ESPN (findings 232)."""
    from datetime import date
    s = JsonSession({"partner-game": NHL_YESTERDAY, "site.api.espn.com": ESPN_TODAY})
    odds = leads.fetch_odds(s, date(2026, 10, 12), {"NJ": "NJD"})
    assert odds["odds_date"] == "2026-10-12" and odds["source"] == "espn"
    assert odds["games"] == [{"game_id": "9", "start_utc": "2026-10-12T23:00:00Z",
                              "home": {"team": "NJD", "MONEY_LINE_2_WAY": -130.0}, "away": {"team": "OTT", "MONEY_LINE_2_WAY": 100.0}}]
    # once the NHL feed is on today, it is used as before, and ESPN isn't asked
    s = JsonSession({"partner-game": NHL_YESTERDAY, "site.api.espn.com": ESPN_TODAY})
    odds = leads.fetch_odds(s, date(2026, 10, 11), {"NJ": "NJD"})
    assert odds["odds_date"] == "2026-10-11" and odds["games"][0]["home"]["team"] == "TOR"
    assert not any("espn" in u for u in s.urls)
    # ESPN down: the NHL feed's games are kept
    s = JsonSession({"partner-game": NHL_YESTERDAY})
    assert leads.fetch_odds(s, date(2026, 10, 12))["odds_date"] == "2026-10-11"


def test_fetch_news_keeps_each_players_latest_item(monkeypatch, tmp_path):
    """Daily Faceoff injury and roster-move news (findings 360 (1)): pages read until the stored items, flags parsed
    as the research did, older items pruned."""
    from datetime import datetime, timezone
    def item(pid, ts, details, cat="Injury"):
        return {"playerFiveVFiveId": pid, "createdAt": ts, "details": details, "fantasyDetails": "", "newsCategoryName": cat}
    s = FakeSession({
        "news/injuries/1": {"data": {"lastPage": 5, "data": [item(1, "2026-11-19T10:00:00.000Z", "Smith will miss the rest of the season after surgery."),
                                                              item(2, "2026-11-18T10:00:00.000Z", "Jones is day-to-day.")]}},
        "news/injuries/2": {"data": {"lastPage": 5, "data": [item(2, "2026-11-12T10:00:00.000Z", "Jones is out indefinitely."),
                                                              item(1, "2026-11-10T10:00:00.000Z", "Smith is day-to-day.")]}},
        "news/injuries/3": {"data": {"lastPage": 5, "data": []}},
        "news/roster-moves/1": {"data": {"lastPage": 1, "data": [item(3, "2026-11-19T09:00:00.000Z", "Doe has been assigned to Syracuse (AHL).", "Send Down"),
                                                                   item(4, "2026-11-17T09:00:00.000Z", "Roe was recalled from Laval.", "Call Up")]}},
    })
    monkeypatch.setattr(leads.time, "sleep", lambda x: None)
    stored = {"version": leads.NEWS_VERSION, "injuries": {"9": {"ts": "2026-11-10T11:00:00", "season": False, "surgery": False},
                           "8": {"ts": "2025-01-01T09:00:00", "season": True, "surgery": False}}}
    out = leads.fetch_news(s, datetime(2026, 11, 20, 12, tzinfo=timezone.utc), stored)
    assert out["injuries"]["1"] == {"ts": "2026-11-19T10:00:00", "season": True, "surgery": True, "indefinite": False,
                                    "season_ts": "2026-11-19T10:00:00", "nf": ["out", "season", "surgery"]}  # newest item wins
    # a later, milder item keeps the date of his latest "indefinitely" item
    assert out["injuries"]["2"]["indefinite"] is False and out["injuries"]["2"]["indefinite_ts"] == "2026-11-12T10:00:00"
    assert out["injuries"]["2"]["season"] is False and "9" in out["injuries"] and "8" not in out["injuries"]  # a year old: pruned
    assert out["roster-moves"]["3"]["down"] and not out["roster-moves"]["3"]["up"]
    assert out["roster-moves"]["4"]["up"] and not out["roster-moves"]["4"]["down"]
    assert out["fetched"] == "2026-11-20T12:00Z"
    # every recent item with the Week dress's flags (handoffs 406-408)
    assert [(r["ts"], r["f"]) for r in out["recent"]["2"]] == [("2026-11-12T10:00:00", ["indefinite", "out"]), ("2026-11-18T10:00:00", ["dtd"])]
    assert out["recent"]["3"] == [{"ts": "2026-11-19T09:00:00", "cat": "move", "f": ["down"]}]
    # items stored under older wording rules are read again: the stored player 9 is gone, the pages refill the rest
    again = leads.fetch_news(s, datetime(2026, 11, 20, 12, tzinfo=timezone.utc), {**stored, "version": 1})
    assert "9" not in again["injuries"] and again["injuries"]["1"] == out["injuries"]["1"]
    monkeypatch.setattr(leads, "NEWS_FILE", tmp_path / "dfo_news.json")
    leads.save_news(out)
    assert leads.load_news() == out


def test_news_flags_for_the_week_dress():
    """Handoffs 406-408: practice, skating, could-return, return and out wording, and the stated timeline in days."""
    f = lambda t: leads.news_flags({"details": t})
    assert f("Hughes was a full participant in practice Tuesday.")["full"] and f("He wore a no-contact jersey.")["limited"]
    assert f("He skated on his own Monday.")["skate"] and f("He is a game-time decision.")["gtd"]
    assert f("He will return to the lineup Thursday.")["ret"] and f("He has been ruled out Saturday.")["out"]
    assert f("He is expected to miss 4-6 weeks.")["tl"] == 35 and f("He'll miss his 10th straight game.")["tl"] is None
    assert f("Out a couple weeks.")["tl"] == 14 and f("He is day-to-day.")["tl"] is None


def test_news_flags_season_level_and_indefinite_wording():
    """Findings 371: "until the playoffs", "end of the regular season", "won't return this season" are season-level;
    "out indefinitely" and "long-term" are their own flag, but long-term injured reserve is not."""
    f = lambda t: leads.news_flags({"details": t})
    for t in ("Hamilton is out until at least the playoffs.", "He will not return before the end of the regular season.",
              "Barzal won't play again this season.", "Out for the season with a torn ACL."):
        assert f(t)["season"], t
    assert f("McAvoy is out indefinitely.")["indefinite"] and not f("McAvoy is out indefinitely.")["season"]
    assert not f("He was placed on long-term injured reserve.")["indefinite"] and not f("Day-to-day with a bruise.")["indefinite"]
    # handoff 401: negations and past seasons are not flags, and season phrases need an absence
    for t in ("Morrissey's injury is not expected to be a long-term issue.", "He missed significant time last season.",
              "Sanderson won't be out long-term."):
        assert not f(t)["indefinite"], t
    for t in ("He was healthy again in the playoffs.", "Paul will be ready for the season opener.",
              "Tkachuk is expected to play for the season opener."):
        assert not f(t)["season"], t
    assert f("He will miss the remainder of the 2024-25 season.")["season"]
    assert f("He is sidelined for the rest of the regular season.")["season"]
    # handoff 410: returns in time for the playoffs are not season-level; absences until then still are
    for t in ("He is fully healthy for the playoffs.", "He should be ready for the playoffs.", "He's back in time for the playoffs."):
        assert not f(t)["season"], t
    assert f("He is out until the playoffs.")["season"] and f("He will miss the remainder of the 2024-25 regular season.")["season"]
    # handoff 404: negated surgery
    for t in ("Out 3-4 weeks, no surgery required.", "He will not need surgery.", "He won't need surgery.",
              "He avoided surgery.", "Surgery is not required."):
        assert not f(t)["surgery"], t
    assert f("He underwent surgery on Tuesday.")["surgery"] and f("He needs surgery and is out.")["surgery"]
