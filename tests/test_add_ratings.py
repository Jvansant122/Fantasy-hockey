"""add_ratings end to end on a tiny made-up league, with every NHL and MoneyPuck call faked.

fetch.py swallows rating errors so the site still publishes; this test is what catches a broken rating before merge."""
import json
import re
from datetime import date

import pytest
import requests

import rating

TODAY, MONDAY = date(2026, 10, 9), date(2026, 10, 5)
PAST = ["2026-10-01", "2026-10-03", "2026-10-05", "2026-10-07"]
WEEK = ["2026-10-05", "2026-10-07", "2026-10-09", "2026-10-11"]
TEAMS = ["TOR", "BOS"]
SKATERS = [  # id, name, team, pos, games missed (by index into PAST)
    (1, "Auston Matthews", "TOR", "C", set()),
    (2, "Morgan Rielly", "TOR", "D", {1, 2, 3}),
    (3, "David Pastrnak", "BOS", "R", set()),
    (4, "Charlie McAvoy", "BOS", "D", {3}),
]
GOALIES = [(11, "Joseph Woll", "TOR"), (12, "Anthony Stolarz", "TOR"), (13, "Jeremy Swayman", "BOS")]


class Resp:
    def __init__(self, payload=None, text=None, status=200):
        self.payload, self.text, self.status_code = payload, text, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def game_id(team, i):
    return 2026020000 + i * 10 + TEAMS.index(team)


def skater_row(pid, name, team, pos, i):
    return {"playerId": pid, "gameId": game_id(team, i), "gameDate": PAST[i], "teamAbbrev": team, "skaterFullName": name,
            "positionCode": pos, "timeOnIcePerGame": 1100 + pid * 10, "goals": i % 2, "assists": 1, "ppPoints": 1, "shPoints": 0,
            "shots": 3, "hits": 2, "blockedShots": 1 + (pos == "D"), "totalShotAttempts": 6, "ppTimeOnIce": 150}


def goalie_row(pid, name, team, i):
    started = (team == "BOS") or (pid == 11) == (i % 2 == 0)
    return {"playerId": pid, "gameId": game_id(team, i), "gameDate": PAST[i], "teamAbbrev": team, "goalieFullName": name,
            "gamesStarted": int(started), "wins": int(started), "losses": 0, "otLosses": 0, "goalsAgainst": 2 * started,
            "saves": 28 * started, "shutouts": 0}


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        params = params or {}
        if "moneypuck" in url:
            if "/2025/" in url:
                return Resp(status=404)  # last season's file missing: rating should carry on without it
            lines = ["playerId,situation,games_played,I_F_xGoals,OnIce_F_xGoals"]
            lines += [f"{pid},all,4,1.2,3.0" for pid, *_ in SKATERS] + ["1,5on5,4,0.8,2.0"]
            return Resp(text="\n".join(lines))
        if url.endswith(f"/schedule/{MONDAY.isoformat()}"):
            return Resp({"gameWeek": [{"date": d, "games": [{"gameType": 2, "awayTeam": {"abbrev": "BOS"}, "homeTeam": {"abbrev": "TOR"}}]}
                                      for d in WEEK]})
        m = re.search(r"/roster/(\w+)/current$", url)
        if m:
            team = m.group(1)
            def person(pid, name, pos):
                first, last = name.split(" ", 1)
                return {"id": pid, "firstName": {"default": first}, "lastName": {"default": last}, "positionCode": pos}
            return Resp({"forwards": [person(p, n, pos) for p, n, t, pos, _ in SKATERS if t == team and pos != "D"],
                         "defensemen": [person(p, n, pos) for p, n, t, pos, _ in SKATERS if t == team and pos == "D"],
                         "goalies": [person(p, n, "G") for p, n, t in GOALIES if t == team]})
        m = re.search(r"/stats/rest/en/(\w+/\w+)$", url)
        assert m, f"unexpected URL {url}"
        report, exp = m.group(1), params["cayenneExp"]
        season = int(re.search(r"seasonId=(\d+)", exp).group(1))
        dates = re.findall(r'gameDate[<>]="([\d-]+)"', exp)
        if not dates:  # season totals: only last season has any
            if season != 20252026:
                return Resp({"data": []})
            if report.startswith("goalie"):
                return Resp({"data": [{"playerId": pid, "gamesStarted": 40} for pid, *_ in GOALIES]})
            return Resp({"data": [{**skater_row(pid, name, team, pos, 0), "gamesPlayed": 80, "goals": 20, "assists": 30,
                                   "ppPoints": 15, "shots": 200, "hits": 60, "blockedShots": 50, "ppTimeOnIce": 12000}
                                  for pid, name, team, pos, _ in SKATERS]})
        lo, hi = dates
        days = [i for i, d in enumerate(PAST) if lo <= d <= hi] if season == 20262027 else []
        if report.startswith("goalie"):
            return Resp({"data": [goalie_row(pid, name, team, i) for pid, name, team in GOALIES for i in days]})
        return Resp({"data": [skater_row(pid, name, team, pos, i) for pid, name, team, pos, missed in SKATERS
                              for i in days if i not in missed]})


def espn(pid, name, team, pos, injury=None, games_left=3):
    return {"id": pid, "name": name, "team": team, "pos": pos, "injury": injury, "games_left": games_left}


@pytest.fixture
def run(monkeypatch, tmp_path):
    fake = FakeSession()
    monkeypatch.setattr(rating.requests, "Session", lambda: fake)
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})  # ignore any real data/news.json
    players = [
        espn(101, "Auston Matthews", "TOR", "C"),
        espn(102, "Morgan Rielly", "TOR", "D", injury="DAY_TO_DAY"),
        espn(103, "David Pastrnak", "BOS", "RW", injury="OUT"),
        espn(104, "Charlie McAvoy", "BOS", "D"),
        espn(105, "Not A Real Player", "BOS", "C"),
        espn(111, "Joseph Woll", "TOR", "G"),
        espn(112, "Anthony Stolarz", "TOR", "G"),
        espn(113, "Jeremy Swayman", "BOS", "G"),
    ]
    starters = [{"date": "2026-10-09", "games": [{"team": "TOR", "goalie": "Anthony Stolarz", "status": "Confirmed"},
                                                  {"team": "BOS", "goalie": "Jeremy Swayman", "status": "Likely"}]}]
    unmatched = rating.add_ratings(players, 2027, TODAY, MONDAY, starters)
    return {p["name"]: p for p in players}, unmatched, tmp_path / "injury_log.jsonl"


FIELDS = ("cr", "cr_fpg", "cr_games", "cr_dress", "cr_matched", "nhl_id", "cr_why", "cr_pct")


def test_every_player_gets_rating_fields(run):
    players, unmatched, _ = run
    assert unmatched == 1
    for p in players.values():
        for k in FIELDS:
            assert k in p, (p["name"], k)
        assert p["cr"] >= 0 and 0 <= p["cr_pct"] <= 100
        json.dumps(p)  # players.json must stay serialisable


def test_matching_and_injuries(run):
    players, _, log = run
    assert players["Auston Matthews"]["nhl_id"] == 1 and players["Auston Matthews"]["cr"] > 0
    assert players["David Pastrnak"]["cr"] == 0 and players["David Pastrnak"]["cr_dress"] == 0
    assert players["Morgan Rielly"]["cr_dress"] <= 0.5
    ghost = players["Not A Real Player"]
    assert ghost["cr_matched"] is False and ghost["cr_dress"] == rating.NOT_PLAYING_DRESS
    logged = [json.loads(line) for line in log.read_text().splitlines()]
    assert {r["status"] for r in logged} == {"OUT", "DAY_TO_DAY"}


def test_goalie_starts_follow_confirmed_starter(run):
    players, _, _ = run
    woll, stolarz, sway = players["Joseph Woll"], players["Anthony Stolarz"], players["Jeremy Swayman"]
    assert stolarz["cr_why"]["tonight"] == {"starting": True, "status": "Confirmed"}
    assert woll["cr_why"]["tonight"]["starting"] is False
    # two games left (Oct 9 and 11): TOR's two goalies split them, BOS's only goalie gets them all
    assert woll["cr_games"] + stolarz["cr_games"] == pytest.approx(2, abs=0.02)
    assert sway["cr_games"] == pytest.approx(2, abs=0.02)
    assert stolarz["cr_games"] >= 1
    assert woll["cr_dress"] is None


def test_win_prob_removes_the_margin():
    assert rating.win_prob(-110, -110) == pytest.approx(0.5)
    fav = rating.win_prob(-200, 170)
    assert 0.62 < fav < 0.67
    assert rating.win_prob(170, -200) == pytest.approx(1 - fav)


def odds_game(home, away, home_ml, away_ml, start="2026-10-09T23:00:00Z"):
    return {"start_utc": start, "home": {"team": home, "MONEY_LINE_2_WAY": home_ml}, "away": {"team": away, "MONEY_LINE_2_WAY": away_ml}}


def test_line_win_probs_skips_started_and_other_days():
    from datetime import datetime, timezone
    odds = {"games": [odds_game("TOR", "BOS", -200, 170),
                      odds_game("MTL", "OTT", -110, -110, start="2026-10-09T15:00:00Z"),  # already started
                      odds_game("NYR", "NJD", -110, -110, start="2026-10-10T23:00:00Z")]}  # tomorrow
    probs = rating.line_win_probs(odds, TODAY, now=datetime(2026, 10, 9, 16, tzinfo=timezone.utc))
    assert set(probs) == {"TOR", "BOS"} and probs["TOR"] > 0.6


def test_tonights_line_prices_the_goalie_start(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 9, 16, tzinfo=timezone.utc))
    starters = [{"date": "2026-10-09", "games": [{"team": "TOR", "goalie": "Anthony Stolarz", "status": "Confirmed"}]}]
    out = {}
    for name, odds in (("none", None), ("fav", {"games": [odds_game("TOR", "BOS", -250, 210)]})):
        players = [espn(112, "Anthony Stolarz", "TOR", "G"), espn(113, "Jeremy Swayman", "BOS", "G")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, starters, odds)
        out[name] = {p["name"]: p for p in players}
    assert "line" not in out["none"]["Anthony Stolarz"]["cr_why"]
    assert out["fav"]["Anthony Stolarz"]["cr_why"]["line"]["win_prob"] > 0.65
    assert out["fav"]["Anthony Stolarz"]["cr"] > out["none"]["Anthony Stolarz"]["cr"]
    assert out["fav"]["Jeremy Swayman"]["cr"] < out["none"]["Jeremy Swayman"]["cr"]
    # games left are unchanged; only points per start moves
    assert out["fav"]["Jeremy Swayman"]["cr_games"] == out["none"]["Jeremy Swayman"]["cr_games"]


def mp(gp, goals, ixg, assists, tm_goals, tm_xg, **kw):
    return {"gp": gp, "goals": goals, "ixg": ixg / gp, "assists": assists, "tm_goals": tm_goals, "tm_xg": tm_xg,
            "shots": 2.5 * gp, "hits": gp, "blocks": 0.5 * gp, **kw}


def test_expected_fp_and_luck_note():
    # scored on chances worth half as many goals: running hot
    hot = rating.expected_fp(mp(20, 12, 6, 10, 20, 20), None, False)
    assert hot["fpg"] > hot["xfpg"] and hot["luck"] == "hot"
    cold = rating.expected_fp(mp(20, 2, 8, 6, 12, 25), None, False)
    assert cold["luck"] == "cold"
    even = rating.expected_fp(mp(20, 6, 6, 10, 20, 20), None, False)
    assert "luck" not in even and abs(even["fpg"] - even["xfpg"]) < LUCK_TOL
    assert "luck" not in rating.expected_fp(mp(8, 12, 3, 10, 20, 20), None, False)  # too few games to say
    assert rating.expected_fp(None, None, False) is None


LUCK_TOL = 0.15
