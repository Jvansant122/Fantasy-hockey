"""add_ratings end to end on a tiny made-up league, with every NHL and MoneyPuck call faked.

fetch.py swallows rating errors so the site still publishes; this test is what catches a broken rating before merge."""
import json
import math
import re
from datetime import date, timedelta

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
    (5, "Elias Pettersson", "TOR", "C", set()),  # two teammates with one name (findings 112)
    (6, "Elias Pettersson", "TOR", "D", set()),
    (7, "Zachary Bolduc", "BOS", "R", set()),  # ESPN calls him Zack
    (8, "Brad Marchand", "BOS", "L", {0, 1, 2, 3}),  # suspended, no games yet (findings 118)
    (9, "Ryan Spooner", "BOS", "C", {0, 1, 2, 3}),  # healthy regular who hasn't played yet
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
            "saves": 28 * started, "shotsAgainst": 30 * started, "shutouts": 0}


class FakeSession:
    def __init__(self, scratched=(4,), rail_ok=True, moved=None, short_last=None, missed=None, short_date=PAST[-1], away_last=(),
                 bye=False, roster_down=(), last_team=None, starts=None, no_team_xg=False):
        self.calls = []
        self.no_team_xg = no_team_xg  # MoneyPuck's team file missing: the picks fall back to goal differential
        self.starts = starts or {}  # game date: its start time (UTC) in the NHL week schedule (findings 241)
        self.bye = bye  # BOS has no game this week (TOR plays MTL instead), like 14 teams the week of Feb 1 (findings 230)
        self.roster_down = set(roster_down)  # teams whose NHL roster can't be read
        self.last_team = last_team or {}  # player id: his team last season, when not his team now
        self.missed = missed or {}  # player id: games he missed, replacing SKATERS' set
        self.short_last = short_last or {}  # player id: his ice time in seconds in the last game (left early)
        self.short_date = short_date
        self.away_last = set(away_last)  # player ids who missed all of last season but played the season before  # ... on this date (his last game when he sat the team's last)
        self.moved = moved or {}  # player id: his first n games this season were for the other team (a trade)
        self.scratched, self.rail_ok = set(scratched), rail_ok  # healthy scratches on each game page (McAvoy sat the last one)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        params = params or {}
        if "moneypuck" in url and url.endswith("teams.csv"):
            if self.no_team_xg:
                return Resp(status=404)
            last = "/2025/" in url  # last season: TOR +0.5 xG a game over 82; this season TOR +1 over 4
            rows = [("TOR", 250.0, 209.0, 82) if last else ("TOR", 14.0, 10.0, 4), ("BOS", 200.0, 200.0, 82) if last else ("BOS", 10.0, 10.0, 4)]
            lines = ["team,name,team,position,situation,games_played,xGoalsFor,xGoalsAgainst"]
            lines += [f"{t},{t},{t},Team Level,{s},{g},{f},{a}" for t, f, a, g in rows for s in ("all", "5on5")]
            return Resp(text="\n".join(lines))
        if "moneypuck" in url:
            if "/2025/" in url or "/2024/" in url:
                return Resp(status=404)  # last season's file missing: rating should carry on without it
            lines = ["playerId,situation,games_played,I_F_xGoals,OnIce_F_xGoals"]
            lines += [f"{pid},all,4,1.2,3.0" for pid, *_ in SKATERS] + ["1,5on5,4,0.8,2.0"]
            return Resp(text="\n".join(lines))
        if url.endswith(f"/schedule/{MONDAY.isoformat()}"):
            away = "MTL" if self.bye else "BOS"
            return Resp({"gameWeek": [{"date": d, "games": [{"gameType": 2, "awayTeam": {"abbrev": away}, "homeTeam": {"abbrev": "TOR"},
                                                             **({"startTimeUTC": self.starts[d]} if d in self.starts else {})}]}
                                      for d in WEEK]})
        m = re.search(r"/gamecenter/(\d+)/right-rail$", url)
        if m:
            if not self.rail_ok:
                return Resp(status=503)
            team = TEAMS[int(m.group(1)) % 10]
            i = (int(m.group(1)) - 2026020000) // 10
            def side(t):
                return {"scratches": [{"id": pid} for pid, _, tm, _, missed in SKATERS
                                      if tm == t and i in self.missed.get(pid, missed) and pid in self.scratched]}
            return Resp({"gameInfo": {"awayTeam": side("BOS"), "homeTeam": side(team)}})
        if url.endswith("/standings-season"):
            return Resp({"seasons": [{"id": 20252026, "standingsEnd": "2026-04-17"}]})
        if "/standings/" in url:  # last season's final table, then this season's
            gp = 82 if url.endswith("2026-04-17") else 5
            return Resp({"standings": [{"teamAbbrev": {"default": "TOR"}, "goalFor": 3 * gp, "goalAgainst": 2 * gp, "gamesPlayed": gp,
                                        "wins": gp * 6 // 10, "otLosses": gp // 10},
                                       {"teamAbbrev": {"default": "BOS"}, "goalFor": 2 * gp, "goalAgainst": 3 * gp, "gamesPlayed": gp,
                                        "wins": gp * 4 // 10, "otLosses": gp // 10}]})
        m = re.search(r"/roster/(\w+)/current$", url)
        if m:
            team = m.group(1)
            if team in self.roster_down:
                return Resp(status=503)
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
        if report == "skater/bios":  # birth dates: Matthews 29 on the run date, Bolduc 20, the D Pettersson 38
            births = {1: "1997-09-17", 7: "2006-01-01", 6: "1988-05-11"}
            return Resp({"data": [{"playerId": pid, "birthDate": births.get(pid)} for pid, *_ in SKATERS]})
        if "gamesStarted=0" in exp:  # last season's relief appearances: Woll won one in relief
            rows = [{"playerId": 11, "gamesStarted": 0, "wins": 1, "losses": 0, "otLosses": 0, "goalsAgainst": 1, "saves": 12,
                     "shotsAgainst": 13, "shutouts": 0}] if season == 20252026 else []
            return Resp({"data": rows})
        dates = re.findall(r'gameDate[<>]="([\d-]+)"', exp)
        if not dates:  # season totals: only last season has any (and the one before, for away_last players)
            if season != 20252026 and not (season == 20242025 and self.away_last):
                return Resp({"data": []})
            if report.startswith("goalie"):
                return Resp({"data": [{"playerId": pid, "gamesStarted": 40, "wins": 22, "losses": 12, "otLosses": 6, "goalsAgainst": 110,
                                  "saves": 1000, "shotsAgainst": 1110, "shutouts": 3, "teamAbbrevs": self.last_team.get(pid, team)} for pid, _, team in GOALIES]})
            return Resp({"data": [{**skater_row(pid, name, team, pos, 0), "teamAbbrevs": self.last_team.get(pid, team), "gamesPlayed": 80, "goals": 20, "assists": 30,
                                   "ppPoints": 15, "shots": 200, "hits": 60, "blockedShots": 50, "ppTimeOnIce": 12000}
                                  for pid, name, team, pos, _ in SKATERS if (pid in self.away_last) == (season == 20242025)]})
        lo, hi = dates
        days = [i for i, d in enumerate(PAST) if lo <= d <= hi] if season == 20262027 else []
        if report.startswith("goalie"):
            return Resp({"data": [goalie_row(pid, name, team, i) for pid, name, team in GOALIES for i in days]})
        other = lambda t: TEAMS[1 - TEAMS.index(t)]
        rows = [skater_row(pid, name, other(team) if i < self.moved.get(pid, 0) else team, pos, i)
                for pid, name, team, pos, missed in SKATERS for i in days if i not in self.missed.get(pid, missed)]
        for r in rows:
            if r["playerId"] in self.short_last and r["gameDate"] == self.short_date:
                r["timeOnIcePerGame"] = self.short_last[r["playerId"]]
        return Resp({"data": rows})


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
        espn(106, "Elias Pettersson", "TOR", "C"),
        espn(107, "Elias N. Pettersson", "TOR", "D"),
        espn(108, "Zack Bolduc", "BOS", "LW"),
        espn(109, "Brad Marchand", "BOS", "LW", injury="SUSPENSION"),
        espn(110, "Ryan Spooner", "BOS", "C"),
        espn(111, "Joseph Woll", "TOR", "G"),
        espn(112, "Anthony Stolarz", "TOR", "G"),
        espn(113, "Jeremy Swayman", "BOS", "G"),
    ]
    starters = [{"date": "2026-10-09", "games": [{"team": "TOR", "goalie": "Anthony Stolarz", "status": "Confirmed"},
                                                  {"team": "BOS", "goalie": "Jeremy Swayman", "status": "Likely"}]}]
    # ESPN's injury note: surgery is a long absence, so Pastrnak's week goes to 0 (findings section 58)
    injuries = [{"athlete_id": 103, "short": "Pastrnak (upper body) will undergo surgery Tuesday.", "long": "", "date": "2026-10-08T12:00Z"}]
    unmatched = rating.add_ratings(players, 2027, TODAY, MONDAY, starters, injuries=injuries)
    return {p["name"]: p for p in players}, unmatched, tmp_path / "injury_log.jsonl"


FIELDS = ("cr", "cr_fpg", "cr_games", "cr_dress", "cr_dress_next", "cr_matched", "nhl_id", "cr_why", "cr_pct")


def test_every_player_gets_rating_fields(run):
    players, unmatched, _ = run
    assert unmatched == 1
    for p in players.values():
        for k in FIELDS:
            assert k in p, (p["name"], k)
        assert p["cr"] >= 0 and 0 <= p["cr_pct"] <= 100
        assert isinstance(p["sat_last"], bool)
        assert p["cr_dress_next"] is None or 0 <= p["cr_dress_next"] <= 1
        if p["cr_dress"] is not None and p["games_left"]:
            assert p["cr_games"] == pytest.approx(p["games_left"] * p["cr_dress"], abs=0.02)
        # Proj GP: games so far plus a share of the 84-game season's rest, never more than a full season
        assert isinstance(p["proj_gp"], int) and 0 <= p["proj_gp"] <= rating.SEASON_GAMES, (p["name"], p["proj_gp"])
        json.dumps(p)  # players.json must stay serialisable


def test_matching_and_injuries(run):
    players, _, log = run
    assert players["Auston Matthews"]["nhl_id"] == 1 and players["Auston Matthews"]["cr"] > 0
    assert players["David Pastrnak"]["cr"] == 0 and players["David Pastrnak"]["cr_dress"] == 0
    assert players["David Pastrnak"]["cr_why"]["back"] == {"games": "16"}
    assert players["Morgan Rielly"]["cr_dress"] <= 0.5
    ghost = players["Not A Real Player"]
    # no NHL match, no ESPN games this season or last, no projection: a retired or minor-league name, not rated (handoff 453)
    assert ghost["cr_matched"] is False and ghost["cr"] == ghost["cr_games"] == ghost["cr_dress"] == 0 and ghost["cr_why"]["no_record"]
    logged = [json.loads(line) for line in log.read_text().splitlines()]
    assert {r["status"] for r in logged} == {"OUT", "DAY_TO_DAY", "SUSPENSION"}


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


def test_confirmed_starter_on_waivers_leaves_tonight_to_his_team(monkeypatch, tmp_path):
    """On waivers until tomorrow, a goalie confirmed for tonight doesn't count tonight's start: with it, his team's goalies
    summed above its games and the 8 PM publish check blocked the run (Oct 13, CAR and VAN)."""
    from datetime import datetime, timezone
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 9, 16, tzinfo=timezone.utc))
    starters = [{"date": "2026-10-09", "games": [{"team": "TOR", "goalie": "Anthony Stolarz", "status": "Confirmed"}]}]
    woll = dict(espn(111, "Joseph Woll", "TOR", "G", games_left=2), owner="other", today_start="2026-10-09T23:00:00Z")
    stolarz = dict(espn(112, "Anthony Stolarz", "TOR", "G", games_left=1), owner="waivers", today_start=None)
    rating.add_ratings([woll, stolarz], 2027, TODAY, MONDAY, starters)
    # tonight is Stolarz's start but not his to count, and Woll sits it: only Oct 11 is left between them
    assert stolarz["cr_why"]["exp_starts"] + woll["cr_why"]["exp_starts"] == pytest.approx(1, abs=0.02)
    assert stolarz["cr_why"]["p_today"] == 0 and stolarz["cr_why"]["exp_starts"] <= 1
    # his start chances by night are only on nights after he clears, and sum to his expected starts (handoff 492)
    assert set(stolarz["cr_g_nights"]) == {"2026-10-11"}
    assert sum(stolarz["cr_g_nights"].values()) == pytest.approx(stolarz["cr_games"], abs=0.02)


def test_waiver_goalie_starts_come_from_the_nights_after_he_clears(monkeypatch, tmp_path):
    """Clearing waivers with one of his team's two games left, a goalie keeps only that night's start chance, and his
    expected games are that chance, not half his chances over both nights (handoff 492: Askarov 1.02 vs 0.51)."""
    from datetime import datetime, timezone
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 9, 16, tzinfo=timezone.utc))
    woll = dict(espn(111, "Joseph Woll", "TOR", "G", games_left=2), owner="other", today_start="2026-10-09T23:00:00Z")
    stolarz = dict(espn(112, "Anthony Stolarz", "TOR", "G", games_left=1), owner="waivers", today_start=None)
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 8, 16, tzinfo=timezone.utc))
    woll["games_left"], woll["today_start"] = 2, None
    rating.add_ratings([woll, stolarz], 2027, TODAY - timedelta(days=1), MONDAY, [])  # Oct 9 and 11 still ahead
    assert set(stolarz["cr_g_nights"]) == {"2026-10-11"}
    assert sum(stolarz["cr_g_nights"].values()) == pytest.approx(stolarz["cr_games"], abs=0.02)
    assert sum(woll["cr_g_nights"].values()) == pytest.approx(woll["cr_games"], abs=0.02)


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
    monkeypatch.setattr(rating, "team_strength", lambda s, season: {})  # isolate the line from schedule pricing
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


def test_early_season_backup_not_written_off():
    """One opening-night start shouldn't read as a 100/0 split (findings section 41)."""
    model = rating.Model()
    dates = ["2026-10-09", "2026-10-11", "2026-10-13", "2026-10-15"]
    prev = {1: 50, 2: 32}
    early = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": [("2026-10-07", 1)]}, prev, dates, TODAY)
    assert early[2] > 0.8  # the backup still gets about a third of the next 4
    # from the 10th game on, the shrink is gone and recent starts decide
    hist = [(f"2026-09-{d:02d}", 1) for d in range(10, 20)]
    late = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, prev, dates, TODAY)
    assert late[2] < early[2]


def test_sat_last_game_drops_dress_chance():
    """A regular who sat the team's last game plays far fewer games next week (correlation research section 6)."""
    model = rating.Model()
    team = list(range(1, 13))
    def chance(played):
        gl = [{"game": g, "team": "TOR", "toi": 16.0, "date": f"2026-10-{g:02d}"} for g in played]
        f = {"dressed3": sum(g in played for g in team[-3:]) / 3, "dressed10": sum(g in played for g in team[-10:]) / 10,
             "std_toi": 16.0, "is_D": 0.0}
        return model.dress_logit(rating.dress_features(gl, "TOR", team, f, date(2026, 10, 13)))
    regular, sat_last = chance(team), chance(team[:-1])
    assert regular > 0.85 and sat_last < regular - 0.25
    assert chance(team[:-4]) < sat_last  # out longer, less likely still


def test_usage_times_efficiency_rewards_more_ice_time():
    mu = rating.Model().sk["ux_means"]
    f = {"std_gp": 10, "prev_gp": 82, "std_toi": 18, "prev_toi": 18, "std_pptoi": 2, "prev_pptoi": 2, "l5_pptoi": 2,
         "std_goals": 0.4, "prev_goals": 0.4, "std_assists": 0.5, "prev_assists": 0.5, "std_points": 0.9, "prev_points": 0.9,
         "std_ppPoints": 0.3, "prev_ppPoints": 0.3, "std_shots": 3, "prev_shots": 3, "std_hits": 1, "prev_hits": 1,
         "std_blockedShots": 0.5, "prev_blockedShots": 0.5, "std_ixg": 0.35, "prev_ixg": 0.35, "std_fp": 2.0, "prev_fp": 2.0}
    more, less = rating.ux_features({**f, "l5_toi": 21}, "F", mu), rating.ux_features({**f, "l5_toi": 15}, "F", mu)
    assert all(more[k] >= less[k] > 0 for k in more) and more["sx_ux"] > less["sx_ux"]
    assert rating.ux_features({"std_gp": 0}, "F", mu)["sx_ux"] is None


def test_week_luck_and_role_inputs():
    f = {"std_gp": 10, "std_goals": 0.2, "std_ixg": 0.4, "prev_goals": 0.5, "prev_ixg": 0.3, "std_shots": 3.0,
         "last_toi": 19.0, "l10_toi": 16.0, "r1_top_line": 1.0, "std_top_line": 0.25, "r1_pp1": 0.0, "std_pp1": 0.9}
    x = rating.week_extras(f)
    assert x["luck_neg"] == pytest.approx(0.1) and x["luck_pos"] == 0  # 0.2 below xG, half weight at 10 games
    assert x["luck_posp"] == pytest.approx(0.2) and x["luck_negp"] == 0
    assert x["luck_sh"] == pytest.approx(0.3)
    assert x["j1_up"] == pytest.approx(3.0) and x["j1_dn"] == 0
    assert x["new_top"] == pytest.approx(0.75) and x["new_pp1"] == 0
    blank = rating.week_extras({"std_gp": 0})
    assert all(blank[k] is None for k in ("luck_neg", "luck_negp", "luck_sh", "j1_up", "new_top"))  # left to the training means
    assert all(blank[k] == 0 for k in ("ag_y", "ag_o", "r_ml", "r_orun", "r_back_fp"))  # unknown age reads 27; no absence
    back = rating.week_extras({"age_m": 21.5, "missed_last": 1.0, "out_run": 14.0, "std_fp": 2.0, "sx_fp": 1.5})
    assert back["ag_y"] == pytest.approx(2.5) and back["ag_o"] == 0 and rating.week_extras({"age_m": 33})["ag_o"] == 4
    assert back["r_orun"] == 10 and back["r_orun_fp"] == 20 and back["r_ml_fp"] == 2 and back["r_ml_sx"] == 1.5


def test_lineup_context_missed_run():
    team = ["g1", "g2", "g3", "g4", "g5"]
    gl = lambda games: [{"game": x, "team": "TOR", "pos": "C", "toi": 15.0, "pptoi": 1.0} for x in games]
    ctx = rating.lineup_context({1: gl(["g1", "g2"]), 2: gl(["g1", "g2", "g3", "g5"]), 3: gl(team)}, {"TOR": team})
    assert (ctx[1]["out_run"], ctx[1]["back_last"], ctx[1]["missed_last"]) == (3, 0, 1)
    assert (ctx[2]["out_run"], ctx[2]["back_last"]) == (0, 1)
    assert (ctx[3]["out_run"], ctx[3]["back_last"]) == (0, 0)


def test_no_game_yet_uses_last_seasons_role():
    """A skater with no game yet this season takes his last-game inputs from last season, not the training means
    (findings 291 (2), regression of findings 213)."""
    prev = {r: 1.0 for r in rating.GAME_RATES} | {"gp": 80, "toi": 22.5, "pptoi": 3.1, "shtoi": 1.4, "pp_rank": 2,
                                                  "toi_rank": 1, "pp1": 1.0, "top_line": 1.0, "pp_share": 0.95}
    f = rating.skater_features([], prev, None, None, ["g1"])
    assert (f["last_toi"], f["toi_l3"], f["pp_l1"], f["pp_l3"]) == (22.5, 22.5, 3.1, 3.1)
    assert (f["r1_pp1"], f["r3_top_line"], f["r1_toi_rank"], f["r1_pp_share"], f["sh_l5"]) == (1.0, 1.0, 1, 0.95, 1.4)
    rates = {1: {"pos": "C", "gp": 80, "toi": 20.0, "pptoi": 4.0}, 2: {"pos": "D", "gp": 82, "toi": 24.0, "pptoi": 2.0}}
    rating.prev_ranks(rates, {1: "TOR", 2: "TOR"})
    assert rates[1]["pp_share"] == 1.0 and rates[2]["pp_share"] == 0.5


def test_injured_starter_hands_starts_to_backup():
    """An injured no. 1's games go to his partner (findings section 48)."""
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 21, 2))]
    dates = ["2026-10-22", "2026-10-24", "2026-10-26", "2026-10-28"]
    healthy = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, dates, date(2026, 10, 22))
    hurt = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, dates, date(2026, 10, 22), injury={1: 0.0})
    assert healthy[1] > healthy[2]
    assert hurt[1] == 0 and hurt[2] == pytest.approx(4.0)


def test_no1_rests_on_back_to_backs_still_ahead(monkeypatch):
    """The no. 1 is counted for fewer starts on a back-to-back still ahead, and slightly more on his ordinary nights
    (findings 231, handoffs 133 and 190)."""
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 21, 2))]
    pair, spaced = ["2026-10-23", "2026-10-24"], ["2026-10-23", "2026-10-25"]
    def starts(dates, on):
        if not on:
            monkeypatch.setattr(rating, "B2B_AHEAD_NIGHT1", 0.0)
            monkeypatch.setattr(rating, "B2B_AHEAD_NIGHT2_NEXT", 0.0)
            monkeypatch.setattr(rating, "NO1_ORDINARY_LOGIT", 0.0)
        out = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, dates, date(2026, 10, 22))
        monkeypatch.undo()
        return out[1]
    assert starts(pair, True) < starts(pair, False)
    assert starts(spaced, True) > starts(spaced, False)


def test_no1_back_in_net_after_resting_night_2(monkeypatch):
    """The no. 1 started night 1 and the backup takes night 2: the game after goes back to the no. 1 (handoff 223)."""
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 21, 2))] + [("2026-10-21", 1)]
    dates = ["2026-10-22", "2026-10-24"]  # night 2 tomorrow, then the game after it
    def third(v):
        monkeypatch.setattr(rating, "AFTER_NO1_REST_OTHER_LOGIT", v)
        nights = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, dates, date(2026, 10, 22), by_date=nights)
        return nights
    on, off = third(-1.0), third(0.0)
    assert on[1]["2026-10-24"] > off[1]["2026-10-24"]
    assert on[1]["2026-10-22"] == pytest.approx(off[1]["2026-10-22"])  # night 2 itself is unchanged


def test_rest_of_season_dress_share():
    """Season rating availability: a regular plays most of the rest of the season, a player out 10 games much less,
    but more than zero (findings section 56)."""
    model = rating.Model()
    team = list(range(1, 21))
    def share(played):
        gl = [{"game": g, "team": "TOR", "toi": 17.0, "date": f"2026-11-{g:02d}"} for g in played]
        f = {"dressed3": sum(g in played for g in team[-3:]) / 3, "dressed10": sum(g in played for g in team[-10:]) / 10,
             "std_toi": 17.0, "l5_toi": 17.0, "std_pptoi": 2.0, "is_D": 0.0}
        df = rating.dress_features(gl, "TOR", team, f, date(2026, 11, 21))
        x = rating.season_dress_features(gl, "TOR", team, f, df, {"gp": 80}, 2.0, model.season_lr["markov"])
        return model.season_dress(x)
    regular, hurt = share(team), share(team[:10])
    assert regular > 0.85 and 0.05 < hurt < regular - 0.2


def test_injury_caps_and_notes():
    """OUT and IR skaters keep a small chance to dress unless the note says it's a long absence (findings section 58)."""
    assert rating.SKATER_INJURY_CAP["OUT"] == 0.35 and rating.SKATER_INJURY_CAP["INJURY_RESERVE"] == 0.15
    assert rating.SKATER_INJURY_CAP["DAY_TO_DAY"] == 0.5 and rating.SKATER_INJURY_CAP["SUSPENSION"] == 0
    day = date(2026, 10, 9)
    note = lambda short, long_="": rating.injury_note({"short": short, "long": long_, "date": "2026-10-08T12:00Z"}, day)
    assert not note("Smith (lower body) won't play Thursday.")["long"]
    assert note("Smith (knee) is expected to miss 4-6 weeks.")["long"]
    assert not note("Smith (illness) is expected to miss a few days.")["long"]
    assert note("Smith was placed on long-term injured reserve.")["long"]
    assert note("Smith will miss the remainder of the season.")["season"]
    assert not note("Smith returned to practice.", "He had surgery last summer.")["long"]  # recaps don't count
    assert rating.usually_back("OUT", True, None) == {"games": "4"}
    assert rating.usually_back("DAY_TO_DAY", False, None) == {"games": "0-1"}
    assert rating.usually_back("OUT", True, note("Smith is expected to miss 4-6 weeks."))["days"] == 34


def test_idle_cap_grades_by_days_since_last_game():
    assert [rating.idle_cap(d) for d in (15, 21, 22, 35, 36, 80)] == [0.5, 0.5, 0.2, 0.2, 0.1, 0.1]


def test_rest_of_week_goalie_starts_priced_by_schedule(run):
    """TOR is stronger and at home in every game, so its starts are worth more than BOS's (findings section 70)."""
    by_name, _, _ = run
    assert by_name["Joseph Woll"]["cr_why"]["sched"] > 0 > by_name["Jeremy Swayman"]["cr_why"]["sched"]


def test_same_name_teammates_and_nicknames_match(run):
    """Vancouver's two Elias Petterssons split by position, and ESPN's "Zack" finds the NHL's "Zachary" (findings 112)."""
    by_name, _, _ = run
    assert by_name["Elias Pettersson"]["nhl_id"] == 5 and by_name["Elias N. Pettersson"]["nhl_id"] == 6
    assert by_name["Zack Bolduc"]["nhl_id"] == 7
    assert not by_name["Not A Real Player"]["cr_matched"]


def test_nickname_needs_same_team_and_last_name():
    index = {"zachary bolduc": {7: ("BOS", "R")}, "zach smith": {8: ("BOS", "C")}}
    assert rating.nickname_cands(index, "Zack Bolduc", "BOS") == {7: ("BOS", "R")}
    assert rating.nickname_cands(index, "Zack Bolduc", "TOR") == {}
    assert rating.nickname_cands(index, "Mike Bolduc", "BOS") == {}


def test_unplayed_regulars_keep_a_season_share(run):
    """A suspended regular takes the short-injury share and a healthy regular who hasn't played his row of the unplayed table, not the idle cap (findings 118, 350 (1))."""
    by_name, _, _ = run
    for name, share in (("Brad Marchand", rating.PRESEASON_INJURED["short"]), ("Ryan Spooner", rating.unplayed_share(80, 4))):
        p = by_name[name]
        assert p["cr_why"]["gp"] == 0
        assert p["cr_season"] == pytest.approx(p["cr_why"]["season_fpg"] * share, abs=0.01)


def test_goalie_new_team_starts_rise():
    """A goalie who started for another team this season and has 1-3 starts for this one gets +1.0 on his start
    logit; with more starts here, or no other team, nothing changes (findings 156)."""
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 21, 2))]  # goalie 2: 3 starts

    def p2(movers):
        p = {}
        out = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, ["2026-10-22", "2026-10-24"],
                                   date(2026, 10, 22), today_p=p, movers=movers)
        return p[2], out
    base, out0 = p2(set())
    moved, out1 = p2({2})
    assert moved > base + 0.05
    assert out1[1] + out1[2] == pytest.approx(out0[1] + out0[2])  # still one starter per game
    assert p2({1})[0] == pytest.approx(base)  # goalie 1 has 7 starts here: past the first few


def test_goalie_new_team_season_share_counts_from_arrival():
    """A goalie who came from another team has his Season start share counted from his first start here, not over
    the team's whole season; other goalies' shares are unchanged (findings 157)."""
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i < 6 else (2 if i % 2 else 1)) for i, d in enumerate(range(1, 21, 2))]  # 2 arrives at game 7

    def shares(movers):
        sh = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, ["2026-10-22"], date(2026, 10, 22),
                             season_share=sh, movers=movers)
        return sh
    base, moved = shares(set()), shares({2})
    assert sum(base.values()) == pytest.approx(rating.SEASON_SHARE_SCALE, abs=0.05)  # ~6% of starts go to goalies not here yet
    assert moved[2] > base[2] + 0.05
    assert moved[1] == pytest.approx(base[1])


def test_departed_goalie_takes_no_starts():
    """A goalie in the team's recent starts who is no longer on its roster gets none of its starts; with no roster
    the recent starters stand in (findings 160)."""
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 3 else 2) for i, d in enumerate(range(1, 21, 2))]
    dates = ["2026-10-22", "2026-10-24"]
    out = rating.goalie_starts(model, "TOR", [1, 3], {"TOR": hist}, {1: 50, 2: 32, 3: 10}, dates, date(2026, 10, 22))
    assert 2 not in out and sum(out.values()) == pytest.approx(2)
    assert 2 in rating.goalie_starts(model, "TOR", [], {"TOR": hist}, {1: 50, 2: 32}, dates, date(2026, 10, 22))


def test_last_start_result_shifts_the_next_games(monkeypatch):
    """A bad start (5+ against, or pulled after 3+) lowers the starter's chance in the next game and a little in
    the one after; a win raises it and a loss lowers it in the next game only (findings 131-133)."""
    monkeypatch.setattr(rating, "NO1_ORDINARY_LOGIT", 0.0)  # this test is about the last-start shifts alone
    monkeypatch.setattr(rating, "LAST6_OTHER_LOGITS", {})  # the history ends 1 1 1 0 1 (findings 246)
    monkeypatch.setattr(rating, "PLAIN_SECOND_LOGIT", 0.0)  # its game-after-next cut is tested on its own (findings 249)
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 21, 2))]
    dates = ["2026-10-22", "2026-10-24", "2026-10-26"]

    def run(shift, n):
        p = {}
        out = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, dates[:n], date(2026, 10, 22),
                                   today_p=p, last_start=(1, shift) if shift else None)
        return p[1], out

    bad = rating.last_start_shift({"ga": 5, "toi": 3600, "wins": 0, "losses": 1, "otl": 0})
    win = rating.last_start_shift({"ga": 2, "toi": 3600, "wins": 1, "losses": 0, "otl": 0})
    loss = rating.last_start_shift({"ga": 2, "toi": 3600, "wins": 0, "losses": 0, "otl": 1})
    assert bad == (rating.BAD_START_LOGIT, rating.BAD_START_LOGIT_2)
    assert win == (rating.WIN_LOGIT, 0.0) and loss == (rating.LOSS_LOGIT, 0.0)
    # a full-game loss is cut deeper the more he let in, short of a bad start (findings 225)
    graded = [rating.last_start_shift({"ga": ga, "toi": 3600, "wins": 0, "losses": 1, "otl": 0})[0] for ga in (2, 3, 4)]
    assert graded[0] > graded[1] > graded[2] > bad[0]
    shutout = rating.last_start_shift({"ga": 0, "toi": 3600, "wins": 1, "losses": 0, "otl": 0})
    hurt = rating.last_start_shift({"ga": 1, "toi": 1500, "wins": 0, "losses": 0, "otl": 0})
    assert shutout[0] > win[0] and hurt[0] < bad[0]
    p_ok, ok = run(None, 1)
    p_bad, b1 = run(bad, 1)
    p_win, _ = run(win, 1)
    p_loss, _ = run(loss, 1)
    assert p_bad < p_loss < p_ok < p_win < run(shutout, 1)[0]
    assert run(hurt, 1)[0] < 0.6 * p_bad  # a starter who left without a decision (hurt) starts next far less often
    assert b1[1] + b1[2] == pytest.approx(ok[1] + ok[2])  # the team still has one starter per game
    # two games: the bad start also costs a little in the second; a win or loss costs nothing there
    _, ok2 = run(None, 2)
    _, bad2 = run(bad, 2)
    _, win2 = run(win, 2)
    _, next_only2 = run((rating.BAD_START_LOGIT, 0.0), 2)
    assert bad2[1] < next_only2[1] - 0.01 and ok2[1] > bad2[1]
    assert win2[1] - ok2[1] == pytest.approx(run(win, 1)[1][1] - ok[1], abs=0.05)
    assert rating.bad_start({"ga": 5, "toi": 3600}) and rating.bad_start({"ga": 3, "toi": 2000})
    assert not rating.bad_start({"ga": 3, "toi": 3600}) and not rating.bad_start({"ga": 2, "toi": 1200})


def test_goalie_who_left_without_a_decision_stays_out_all_week(monkeypatch):
    """The no-decision cut lasts past the next game, and ends once Daily Faceoff confirms him (findings 164)."""
    monkeypatch.setattr(rating, "PLAIN_SECOND_LOGIT", 0.0)  # its game-after-next cut is tested on its own (findings 249)
    monkeypatch.setattr(rating, "LAST6_OTHER_LOGITS", {})  # the history ends 1 1 1 0 1 (findings 246)
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 21, 2))]
    dates = ["2026-10-22", "2026-10-24", "2026-10-26", "2026-10-28"]
    hurt = rating.last_start_shift({"ga": 1, "toi": 1500, "wins": 0, "losses": 0, "otl": 0})
    assert len(hurt) >= 7 and all(z < -1 for z in hurt)

    def per_game(shift, confirmed=None):
        out = []
        for n in range(1, len(dates) + 1):
            e = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, dates[:n], date(2026, 10, 22),
                                     confirmed=confirmed, last_start=(1, shift) if shift else None)
            out.append(e[1] - sum(out))
        return out

    ok, out, next_only = per_game(None), per_game(hurt), per_game((rating.NO_DECISION_LOGIT, 0.0))
    assert all(o < k - 0.2 for o, k in zip(out, ok))
    assert all(o < n - 0.15 for o, n in zip(out[1:], next_only[1:]))  # not just the next game
    def season(shift, confirmed=None):
        sea = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, dates, date(2026, 10, 22),
                             confirmed=confirmed, season_share=sea, last_start=(1, shift) if shift else None)
        return sea
    assert season(hurt)[1] == pytest.approx(rating.NO_DECISION_SEASON * season(None)[1])  # Season share too (findings 167)
    # the share he loses goes to his partner, so the team's shares still add up (findings 217)
    assert season(hurt)[2] == pytest.approx(season(None)[2] + (1 - rating.NO_DECISION_SEASON) * season(None)[1])
    assert season(hurt, {"2026-10-24": 1})[1] == season(None)[1]
    back = per_game(hurt, confirmed={"2026-10-24": 1})
    assert back[1] == 1.0 and all(b > o + 0.1 for b, o in zip(back[2:], out[2:]))  # confirmed back in net: the cut ends


def test_goalie_who_came_on_in_relief_starts_more(monkeypatch):
    """A goalie who relieved in the team's last game gets +1.2 next game, +0.9 later, +0.5 more with 0-3 of the last 20
    starts (findings 170); a confirmation still overrides; with no roster he is still a candidate."""
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 21, 2))]
    dates = ["2026-10-22", "2026-10-24", "2026-10-26"]

    def run(goalies, relief=(), confirmed=None):
        p = {}
        e = rating.goalie_starts(model, "TOR", goalies, {"TOR": hist}, {1: 50, 2: 32, 3: 2}, dates, date(2026, 10, 22),
                                 confirmed=confirmed, today_p=p, relief=relief)
        return p, e
    (p0, e0), (p1, e1) = run([1, 2]), run([1, 2], relief={2})
    assert p1[2] > p0[2] + 0.1 and e1[2] > e0[2] + 0.3 and e1[1] + e1[2] == pytest.approx(e0[1] + e0[2])
    (q0, f0), (q1, f1) = run([1, 2, 3]), run([1, 2, 3], relief={3})  # a third goalie with no starts relieved
    assert q1[3] > 3 * q0[3] and f1[3] > 2.5 * f0[3]
    monkeypatch.setattr(rating, "RELIEF_RARE_LOGIT", 0.0)
    assert run([1, 2, 3], relief={3})[0][3] < q1[3]
    assert run([1, 2, 3], relief={3}, confirmed={"2026-10-22": 1})[0][3] == 0.0
    assert 3 in run([], relief={3})[1]  # no roster: the reliever is still a candidate


def test_later_games_are_softened(monkeypatch):
    """Start chances two or more games ahead are pulled toward an even split; the next game is not (findings 134)."""
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 6 else 2) for i, d in enumerate(range(1, 21, 2))]
    dates = ["2026-10-22", "2026-10-24", "2026-10-26", "2026-10-28"]

    def run(n):
        return rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 60, 2: 22}, dates[:n], date(2026, 10, 22))

    soft1, soft4 = run(1), run(4)
    monkeypatch.setattr(rating, "LATER_GAME_TEMP", 1.0)
    hard1, hard4 = run(1), run(4)
    assert soft1[1] == pytest.approx(hard1[1])  # the next game is unchanged
    assert hard4[1] > 2.5 and soft4[1] < hard4[1]  # a clear no. 1 gets fewer of the later starts
    assert soft4[1] + soft4[2] == pytest.approx(4)  # still one starter per game


def test_healthy_scratch_comes_back_more_often(monkeypatch, tmp_path):
    """A skater on the last game's healthy-scratch list gets +0.5 on his dress logit; one who missed it but isn't on
    the list doesn't; if the list can't be read, missing the game with no ESPN injury counts (findings 153)."""
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "BOOST_WEEK_W", 0.0)  # the shift on its own, without the weekly boosted model over it
    monkeypatch.setattr(rating, "SCRATCH_LAST_RESULT_BY_GAME", {"win": (0.0,), "other": (0.0,)})  # ... and without the last-result lift (handoff 354)
    monkeypatch.setattr(rating, "load_news", lambda: {})
    def mcavoy(**kw):
        fake = FakeSession(**kw)
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        players = [espn(104, "Charlie McAvoy", "BOS", "D"), espn(101, "Auston Matthews", "TOR", "C")]
        rating.add_ratings(players, 2027, TODAY, MONDAY)
        return players[0]
    listed, unlisted, no_list = mcavoy(), mcavoy(scratched=()), mcavoy(rail_ok=False)
    # half the old logit since it moved after the boosted Week model (handoff 516)
    assert listed["sat_last"] and listed["cr_dress_next"] > unlisted["cr_dress_next"] + 0.02
    assert no_list["cr_dress_next"] == listed["cr_dress_next"]
    assert listed["cr_games"] > unlisted["cr_games"]
    base = rating.dress_by_game(0.25, 3, True, 1.0)
    up = rating.dress_by_game(0.25, 3, True, 1.0, rating.SCRATCH_LOGIT["F"])
    assert all(b < u for b, u in zip(base, up)) and max(rating.dress_by_game(0.9, 3, True, 0.3, rating.SCRATCH_LOGIT["F"])) <= 0.3


def test_skater_who_left_last_game_early_rarely_dresses(monkeypatch, tmp_path):
    """Dressed for the last game but played under 40% of his average ice time: big dress cut for the next games, until
    ESPN flags him; 40-55% a small one for two games (findings 165)."""
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    def matthews(toi=None, injury=None):
        fake = FakeSession(short_last={1: toi} if toi is not None else None)
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        players = [espn(101, "Auston Matthews", "TOR", "C", injury=injury), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY)
        return players[0]
    full, hurt, short = matthews(), matthews(toi=300), matthews(toi=560)  # usual ~18.5 minutes; 5 and 9.3 last game
    assert not hurt["sat_last"] and hurt["cr_dress_next"] < 0.45 * full["cr_dress_next"]
    assert hurt["cr_dress_next"] < short["cr_dress_next"] < full["cr_dress_next"]
    assert hurt["cr_games"] < full["cr_games"] - 0.5
    monkeypatch.setattr(rating, "LEFT_EARLY_SEASON_LOGIT", 0.0)
    assert hurt["cr_season"] < matthews(toi=300)["cr_season"] - 0.02  # Season dress share is trimmed too (findings 166)
    assert full["cr_season"] == matthews()["cr_season"]
    monkeypatch.setattr(rating, "LEFT_EARLY_SEASON_LOGIT", -0.4)
    # ESPN's DAY_TO_DAY flag takes over from the left-early cut, capped by last game's share of his ice time (findings 176)
    assert matthews(toi=300, injury="DAY_TO_DAY")["cr_dress_next"] == pytest.approx(0.15, abs=0.01)
    assert matthews(toi=800, injury="DAY_TO_DAY")["cr_dress_next"] == pytest.approx(0.5, abs=0.01)
    assert 0.5 < matthews(injury="DAY_TO_DAY")["cr_dress_next"] <= 0.75
    assert matthews(injury="DAY_TO_DAY")["cr_dress"] <= 0.75
    # OUT after leaving early: 0.15, OUT with full minutes keeps 0.35 (handoff 75)
    assert matthews(toi=300, injury="OUT")["cr_dress_next"] == pytest.approx(rating.OUT_LEFT_EARLY_CAP, abs=0.01)
    assert matthews(injury="OUT")["cr_dress_next"] == pytest.approx(rating.SKATER_INJURY_CAP["OUT"], abs=0.01)
    assert rating.left_early_shifts(5, 18) == rating.LEFT_EARLY_LOGITS[0][1] and rating.left_early_shifts(9, 18)[0] < 0
    assert rating.left_early_shifts(15, 18) == () and rating.left_early_shifts(5, None) == ()
    by = rating.dress_by_game(0.9, 5, False, 1.0, 0.0, (-2.5, -1.8, -1.1))
    assert by[0] < by[1] < by[2] and by[3] == pytest.approx(by[2], abs=0.08)


def test_just_back_in_the_lineup(monkeypatch, tmp_path):
    """Back 1-3 games from a short healthy-scratch spell: sits again more (-0.4); back 1-2 games from a 5+ game absence
    that wasn't mostly scratches: plays more (+0.4) (findings 168)."""
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "BOOST_WEEK_W", 0.0)  # the shift on its own, without the weekly boosted model over it
    monkeypatch.setattr(rating, "load_news", lambda: {})
    def matthews(**kw):
        fake = FakeSession(missed={1: {2}}, **kw)
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY)
        return players[0]
    rotation, hurt = matthews(scratched=(1, 4)), matthews(scratched=(4,))  # sat game 2 as a scratch / not on the list
    assert not rotation["sat_last"] and rotation["cr_dress_next"] < hurt["cr_dress_next"]
    tg = list(range(10))
    lists = {x: set() for x in tg}
    look = lists.get
    assert rating.comeback_logit(tg, {0, 1, 2, 8, 9}, look, 7) == rating.BACK_INJURY_LOGIT  # back 2 after missing 5
    assert rating.comeback_logit(tg, {0, 1, 2, 7, 8, 9}, look, 7) == 0.0  # back 3: no longer
    assert rating.comeback_logit(tg, {0, 9}, lambda x: {7} if x in (3, 4, 5, 6, 7) else set(), 7) == 0.0  # mostly scratched
    assert rating.comeback_logit(tg, {0, 1, 2, 3, 4, 5, 8, 9}, lambda x: {7}, 7) == rating.BACK_SCRATCH_LOGIT
    assert rating.comeback_logit(tg, {0, 1, 2, 3, 4, 5, 8, 9}, look, 7) == 0.0  # short gap not on the list
    assert rating.comeback_logit(tg, {0, 1, 2, 3, 4, 5, 8, 9}, lambda x: None, 7) == 0.0  # lists unreadable
    assert rating.comeback_logit(tg, {0, 1, 2, 8, 9}, lambda x: None, 7) == rating.BACK_INJURY_LOGIT
    assert rating.comeback_logit(tg, {8, 9}, look, 7) == 0.0 and rating.comeback_logit(tg, set(tg), look, 7) == 0.0


def test_just_traded_skater_dresses_more(monkeypatch, tmp_path):
    """A skater who dressed last game in his first 1-3 games for a new team gets +0.45 on his dress logit; from his
    4th game with the team, nothing (findings 155)."""
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "BOOST_WEEK_W", 0.0)  # the shift on its own, without the weekly boosted model over it
    monkeypatch.setattr(rating, "load_news", lambda: {})
    def matthews(moved, logit):
        monkeypatch.setattr(rating, "NEW_TEAM_LOGIT", logit)
        fake = FakeSession(moved={1: moved})
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY)
        return players[0]["cr_dress_next"]
    assert matthews(2, 0.45) > matthews(2, 0.0)  # 2 games with TOR after 2 with BOS
    def season(logit):
        monkeypatch.setattr(rating, "NEW_TEAM_SEASON_LOGIT", logit)
        fake = FakeSession(moved={1: 2})
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY)
        return players[0]["cr_season"]
    assert season(0.3) > season(0.0)  # Season rating: 2 games for the new team is within the first 10 (findings 158)
    assert matthews(0, 0.45) == matthews(0, 0.0)  # never moved


def test_traded_before_first_game_for_new_team(monkeypatch, tmp_path):
    """ESPN lists him on a new team but he hasn't played for it: his old team's later games aren't misses and the
    new-team shift applies (findings 159)."""
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    def mcavoy(espn_team):
        fake = FakeSession(scratched=())
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        players = [espn(104, "Charlie McAvoy", espn_team, "D"), espn(101, "Auston Matthews", "TOR", "C")]
        rating.add_ratings(players, 2027, TODAY, MONDAY)
        return players[0]
    stayed, moved = mcavoy("BOS"), mcavoy("TOR")  # BOS played once after his last game
    assert stayed["sat_last"] and not moved["sat_last"]
    assert moved["cr_dress_next"] > 0.85 > stayed["cr_dress_next"]
    assert moved["cr_dress"] > stayed["cr_dress"]


def test_dress_chance_by_game():
    """A skater who missed his team's last game is less likely to play the next one than later ones; a regular the
    reverse; caps still apply (findings 147)."""
    out = rating.dress_by_game(0.18, 4, True, 1.0)
    reg = rating.dress_by_game(0.91, 4, False, 1.0)
    assert out[0] == pytest.approx(0.12, abs=0.01) and out == sorted(out)
    assert reg[0] == pytest.approx(0.945, abs=0.01) and reg == sorted(reg, reverse=True)
    assert max(rating.dress_by_game(0.9, 3, False, 0.5)) <= 0.5
    assert rating.dress_by_game(0.5, 0, False, 1.0) == []


def test_confirmed_starter_matches_daily_faceoff_spelling(monkeypatch, tmp_path):
    # Daily Faceoff's "Matthew Murray" is the NHL's "Matt Murray": same team, last name, first two letters (findings 175)
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    out = {}
    for spelling in ("Joseph Woll", "Joey Woll", "Jake Woll"):
        players = [espn(111, "Joseph Woll", "TOR", "G"), espn(112, "Anthony Stolarz", "TOR", "G")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [{"date": "2026-10-09", "games": [
            {"team": "TOR", "goalie": spelling, "status": "Confirmed"}]}])
        out[spelling] = {p["name"]: p for p in players}
    assert out["Joey Woll"]["Joseph Woll"]["cr_why"]["tonight"] == {"starting": True, "status": "Confirmed"}
    assert out["Joey Woll"]["Joseph Woll"]["cr_games"] == out["Joseph Woll"]["Joseph Woll"]["cr_games"]
    # a confirmed name that matches nobody on the team (a new call-up): the team's goalies sit that game (findings 217)
    for who in ("Joseph Woll", "Anthony Stolarz"):
        assert out["Jake Woll"][who]["cr_why"]["tonight"] == {"starting": False, "status": "Confirmed"}
        assert out["Jake Woll"][who]["cr_why"]["p_today"] == 0
    assert out["Jake Woll"]["Joseph Woll"]["cr_games"] < out["Joseph Woll"]["Joseph Woll"]["cr_games"] - 0.5


def test_status_cleared_before_he_is_back(monkeypatch, tmp_path):
    # ESPN dropped a status this week and he hasn't played since: a suspension served or an injury healed (findings 172-173)
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "load_news", lambda: {})
    out = {}
    for name, flagged in (("none", ()), ("cleared", (110, 102)), ("old", ())):
        log = tmp_path / f"{name}.jsonl"
        day = (TODAY - timedelta(days=10 if name == "old" else 2)).isoformat()
        log.write_text("".join(json.dumps({"date": day, "espn_id": i, "status": "OUT"}) + "\n" for i in flagged or (110, 102)))
        if name == "none":
            log.write_text("")
        monkeypatch.setattr(rating, "INJURY_LOG", log)
        players = [espn(110, "Ryan Spooner", "BOS", "C"), espn(102, "Morgan Rielly", "TOR", "D")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        out[name] = {p["name"]: p for p in players}
    spooner, rielly = out["cleared"]["Ryan Spooner"], out["cleared"]["Morgan Rielly"]
    # yet to play: 0.75 next game, 0.8 later ones, Season 0.72 of his points a game
    assert spooner["cr_dress_next"] == rating.CLEARED_NEW_NEXT
    assert spooner["cr_dress"] == pytest.approx((0.75 + 0.8 * 2) / 3, abs=0.01)
    assert spooner["cr_season"] == pytest.approx(spooner["cr_why"]["season_fpg"] * rating.CLEARED_NEW_SEASON, abs=0.01)
    assert out["none"]["Ryan Spooner"]["cr_dress"] < 0.2
    assert out["none"]["Ryan Spooner"]["cr_season"] < spooner["cr_season"]
    # in season and still out: more likely back this week and for the rest of the season
    assert rielly["cr_dress"] > out["none"]["Morgan Rielly"]["cr_dress"]
    assert rielly["cr_season"] > out["none"]["Morgan Rielly"]["cr_season"]
    # a status older than a week changes nothing
    for who in ("Ryan Spooner", "Morgan Rielly"):
        assert out["old"][who]["cr"] == out["none"][who]["cr"] and out["old"][who]["cr_season"] == out["none"][who]["cr_season"]


def test_sat_last_game_but_in_daily_faceoffs_lines(monkeypatch, tmp_path):
    # no ESPN status, sat the last game, Daily Faceoff's lines have him: 14 of 14 dressed next game (findings 174)
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    out = {}
    for dfo in (None, True, False):
        players = [dict(espn(104, "Charlie McAvoy", "BOS", "D"), dfo_out=dfo)]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        out[dfo] = players[0]
    assert out[None]["sat_last"] and out[None]["cr_dress_next"] < rating.DFO_LINEUP_NEXT
    assert out[True]["cr"] == out[None]["cr"]
    assert out[False]["cr_dress_next"] >= rating.DFO_LINEUP_NEXT and out[False]["cr"] > out[None]["cr"]
    assert out[False]["sat_last"]  # still true; the page's Mid-week swaps card reads dfo_out to leave him out
    # no game left this week: Daily Faceoff's lines don't make one up (findings 182)
    players = [dict(espn(104, "Charlie McAvoy", "BOS", "D"), dfo_out=False, games_left=0)]
    rating.add_ratings(players, 2027, TODAY, MONDAY, [])
    assert players[0]["cr_games"] == 0 and players[0]["cr"] == 0


def test_opening_games_cut_short(monkeypatch, tmp_path):
    # two games this season, the latest a few minutes: points a game averaged with leaving it out (findings 171)
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    out = {}
    for name, games in (("off", 0), ("on", 3)):
        monkeypatch.setattr(rating, "SHORT_OPENING_GAMES", games)
        fake = FakeSession(missed={1: {0, 1}}, short_last={1: 200})
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        players = [espn(101, "Auston Matthews", "TOR", "C")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        out[name] = players[0]
    assert out["on"]["cr_fpg"] > out["off"]["cr_fpg"]
    assert out["on"]["cr_season"] > out["off"]["cr_season"]


def test_short_opening_rule():
    prev = {"toi": 22.0}
    assert rating.short_opening([{"toi": 4.5}], prev) == []
    assert rating.short_opening([{"toi": 21.0}, {"toi": 4.5}], prev) == [{"toi": 21.0}]
    assert rating.short_opening([{"toi": 4.5}, {"toi": 21.0}], prev) is None  # the latest was full length
    assert rating.short_opening([{"toi": 21.0}] * 3 + [{"toi": 4.5}], prev) is None  # 4th game on: no change
    assert rating.short_opening([{"toi": 4.5}], None) is None


def test_last_starter_after_a_long_team_break(monkeypatch):
    """Team's next game 4-6 days after its last: +0.4 for whoever started that last game, 7+ days +1.5, next game only
    (findings 196)."""
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 21, 2))]  # goalie 1 started Oct 19

    def p1(day, breaks):
        monkeypatch.setattr(rating, "BREAK_LOGITS", breaks)
        p = {}
        nxt = date.fromisoformat(day)
        out = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32},
                                   [day, (nxt + timedelta(days=2)).isoformat()], nxt, today_p=p)
        return p[1], out
    on = ((7, 1.5), (4, 0.4))
    for day in ("2026-10-22", "2026-10-23", "2026-10-27"):
        (with_, out), (without, base) = p1(day, on), p1(day, ())
        if day == "2026-10-22":  # 3 days: no break
            assert with_ == pytest.approx(without)
        else:
            assert with_ > without + 0.01
            assert out[1] - with_ == pytest.approx(base[1] - without, abs=0.02)  # the game after moves only via the chain
    assert p1("2026-10-27", on)[0] - p1("2026-10-27", ())[0] > p1("2026-10-23", on)[0] - p1("2026-10-23", ())[0]
    # no history at all: nothing to shift, nothing breaks
    assert rating.goalie_starts(model, "TOR", [1, 2], {"TOR": []}, {1: 50, 2: 32}, ["2026-10-27"], date(2026, 10, 27))


def test_second_night_of_a_back_to_back(monkeypatch):
    """Night 2: the no. 1 who played night 1 rests more, a backup who played night 1 goes again more (findings 200)."""
    model = rating.Model()

    def p(last_gid, shifts):
        monkeypatch.setattr(rating, "B2B_NIGHT2_NO1", shifts[0])
        monkeypatch.setattr(rating, "B2B_NIGHT2_OTHER", shifts[1])
        hist = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 19, 2))] + [("2026-10-19", last_gid)]
        out = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, ["2026-10-20"], date(2026, 10, 20), today_p=out)
        return out[last_gid]
    on, off = (rating.B2B_NIGHT2_NO1, rating.B2B_NIGHT2_OTHER), (0.0, 0.0)
    assert p(1, on) < p(1, off) - 0.01  # goalie 1 is the no. 1
    assert p(2, on) > p(2, off) + 0.01  # goalie 2 is the backup


def test_starter_five_to_seven_straight_is_due_a_rest(monkeypatch):
    """The last starter on a 5-7 start run gets -1.0 next game; 4 or 8+ straight, nothing (findings 202)."""
    model = rating.Model()

    def p1(run, logit):
        monkeypatch.setattr(rating, "STREAK_LOGIT", logit)
        seq = [2] + [1, 2] * 4 + [1] * run
        hist = [((date(2026, 10, 1) + timedelta(days=2 * i)).isoformat(), g) for i, g in enumerate(seq)]
        nxt = date.fromisoformat(hist[-1][0]) + timedelta(days=2)
        out = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, [nxt.isoformat()], nxt, today_p=out)
        return out[1]
    for run in (5, 6, 7):
        assert p1(run, -1.0) < p1(run, 0.0) - 0.05
    for run in (4, 8):
        assert p1(run, -1.0) == pytest.approx(p1(run, 0.0))


def test_strict_rotation_keeps_going(monkeypatch):
    """The last 6+ starts strictly alternate: the last starter gets -1.2 next game only; 4-5 alternating, nothing (findings 227)."""
    model = rating.Model()
    assert rating.strict_rotation([1, 1, 2, 1, 2, 1, 2], [1, 2]) and not rating.strict_rotation([1, 1, 2, 1, 2, 1], [1, 2])
    assert not rating.strict_rotation([1, 2, 1, 2, 1, 2], [1, 3])  # one of the pair is gone

    def p(alternating, logit):
        monkeypatch.setattr(rating, "ROTATION_LOGIT", logit)
        seq = [1] * 6 + [2, 1] * (alternating // 2)  # ends on goalie 1
        hist = [((date(2026, 10, 1) + timedelta(days=2 * i)).isoformat(), g) for i, g in enumerate(seq)]
        nxt = date.fromisoformat(hist[-1][0]) + timedelta(days=2)
        later = nxt + timedelta(days=2)
        out = {}
        exp = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, [nxt.isoformat(), later.isoformat()], nxt, today_p=out)
        return out[1], exp
    for n in (6, 8):
        assert p(n, -1.2)[0] < p(n, 0.0)[0] - 0.1
    assert p(4, -1.2)[0] == pytest.approx(p(4, 0.0)[0])
    # the game after next goes back to the last starter less than the chain alone says (findings 228): with the later cut,
    # goalie 2 (due next) has fewer expected starts over the two games
    monkeypatch.setattr(rating, "ROTATION_LATER_LOGIT", 0.0)
    plain = p(6, -1.2)[1]
    monkeypatch.setattr(rating, "ROTATION_LATER_LOGIT", -0.6)
    assert p(6, -1.2)[1][2] < plain[2] - 0.02


def test_repeating_goalie_pattern(monkeypatch):
    """A A B and A A B B patterns over 2+ cycles shift the last starter's next game (findings 243)."""
    model = rating.Model()
    assert rating.repeating_pattern([1, 1, 2, 1, 1, 2], [1, 2]) == (3, False)
    assert rating.repeating_pattern([1, 2, 1, 1, 2, 1], [1, 2]) == (3, True)
    assert rating.repeating_pattern([1, 1, 2, 2, 1, 1, 2, 2], [1, 2]) == (4, False)
    assert rating.repeating_pattern([2, 1, 1, 2, 2, 1, 1, 2], [1, 2]) == (4, True)
    assert rating.repeating_pattern([1, 1, 2, 1, 1, 2], [1, 3]) is None  # one of the pair is gone
    assert rating.repeating_pattern([1, 1, 2, 1, 2], [1, 2]) is None  # under two cycles

    def today(seq):
        hist = [((date(2026, 10, 1) + timedelta(days=2 * i)).isoformat(), g) for i, g in enumerate(seq)]
        nxt = date.fromisoformat(hist[-1][0]) + timedelta(days=2)
        out = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, [nxt.isoformat()], nxt, today_p=out)
        return out[1]
    for seq, sign in (([1, 2, 1, 1, 2, 1], 1), ([2, 1, 1, 2, 1, 1], -1), ([2, 2, 1, 1, 2, 2, 1, 1], -1)):
        monkeypatch.setattr(rating, "PATTERN_LOGITS", {})
        plain = today(seq)
        monkeypatch.setattr(rating, "PATTERN_LOGITS", {(3, True): 0.8, (3, False): -0.4, (4, False): -0.8})
        assert sign * (today(seq) - plain) > 0.03


def test_last_six_start_strings_cut_the_other_goalie(monkeypatch):
    """Two goalies in the last 10, by the last starter's last 6: ends 1 1 1 0 1 -1.0, ends 0 0 1 1 1 -1.2, exactly
    1 1 0 1 0 1 -0.6 on the other goalie's next game, not on night 2 of a back-to-back (findings 246)."""
    model = rating.Model()
    assert rating.last_six_cut([1, 1, 2, 1, 1, 1, 1, 1, 2, 1]) == -1.0
    assert rating.last_six_cut([1, 2, 1, 1, 2, 1, 2, 2, 1, 1, 1]) == -1.2
    assert rating.last_six_cut([2, 2, 2, 2, 1, 1, 2, 1, 2, 1]) == -0.6
    assert rating.last_six_cut([1, 1, 3, 1, 1, 1, 1, 1, 2, 1]) == 0.0  # three goalies in the last 10
    assert rating.last_six_cut([1, 1, 1, 2, 1]) == 0.0  # under 10 games

    def p1(seq, logits, gap=2):
        monkeypatch.setattr(rating, "LAST6_OTHER_LOGITS", logits)
        hist = [((date(2026, 10, 1) + timedelta(days=2 * i)).isoformat(), g) for i, g in enumerate(seq)]
        nxt = date.fromisoformat(hist[-1][0]) + timedelta(days=gap)
        out = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, [nxt.isoformat()], nxt, today_p=out)
        return out[1]
    on = {"110101": -0.6, "11101": -1.0, "00111": -1.2}
    for seq in ([1, 1, 2, 1, 1, 1, 1, 1, 2, 1], [1, 2, 1, 1, 2, 1, 2, 2, 1, 1, 1]):
        assert p1(seq, on) > p1(seq, {}) + 0.05
        assert p1(seq, on, gap=1) == pytest.approx(p1(seq, {}, gap=1))  # night 2 of a back-to-back: the b2b rules decide


def test_three_goalie_teams_cut_the_idle_goalie(monkeypatch):
    """Three goalies in the last 10: each one whose latest start was 3+ team games ago gets -1.0 next game (findings 247)."""
    assert rating.three_goalie_idle([1, 1, 3, 1, 2, 1, 2, 1, 1, 1]) == {2, 3}
    assert rating.three_goalie_idle([1, 1, 3, 1, 1, 1, 1, 2, 3, 1]) == set()  # all three in the last 3
    assert rating.three_goalie_idle([1, 2, 1, 2, 1, 2, 1, 2, 1, 2]) == set()  # two goalies
    model = rating.Model()

    def p(cut):
        monkeypatch.setattr(rating, "THREE_GOALIE_IDLE_LOGIT", cut)
        seq = [1, 1, 3, 1, 2, 1, 2, 1, 1, 1]
        hist = [((date(2026, 10, 1) + timedelta(days=2 * i)).isoformat(), g) for i, g in enumerate(seq)]
        nxt = date.fromisoformat(hist[-1][0]) + timedelta(days=2)
        out = {}
        rating.goalie_starts(model, "TOR", [1, 2, 3], {"TOR": hist}, {1: 50, 2: 20, 3: 12}, [nxt.isoformat()], nxt, today_p=out)
        return out
    on, off = p(-1.0), p(0.0)
    assert on[2] < off[2] and on[3] < off[3] and on[1] > off[1]


def test_later_game_shifts_on_the_last_starter(monkeypatch):
    """A strict rotation's third game -0.9, and the game after next -0.45 when no new next-game rule fires, both on the
    last starter; the next game is unchanged (findings 249, handoff 290)."""
    model = rating.Model()

    def by_date(seq, n, rot, plain):
        monkeypatch.setattr(rating, "ROTATION_THIRD_LOGIT", rot)
        monkeypatch.setattr(rating, "PLAIN_SECOND_LOGIT", plain)
        hist = [((date(2026, 10, 1) + timedelta(days=2 * i)).isoformat(), g) for i, g in enumerate(seq)]
        first = date.fromisoformat(hist[-1][0]) + timedelta(days=2)
        days = [(first + timedelta(days=2 * i)).isoformat() for i in range(n)]
        out = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, days, first, by_date=out)
        return [out[seq[-1]][d] for d in days]
    rot = [1, 2, 1, 2, 1, 2, 1]
    on, off = by_date(rot, 3, -0.9, -0.45), by_date(rot, 3, 0.0, -0.45)
    assert on[:2] == pytest.approx(off[:2]) and on[2] < off[2] - 0.03
    plain = [2, 1, 1, 2, 1, 1, 1, 2, 1, 1]
    on, off = by_date(plain, 2, -0.9, -0.45), by_date(plain, 2, -0.9, 0.0)
    assert on[0] == pytest.approx(off[0]) and on[1] < off[1] - 0.01
    assert by_date(rot, 2, -0.9, -0.45) == pytest.approx(by_date(rot, 2, -0.9, 0.0))  # rotating: not the plain shift


def test_goalie_game_that_already_started(monkeypatch, tmp_path):
    # tonight's game started before the run: its start is no longer ahead and the stream card's chance is 0 (findings 178)
    from datetime import datetime, timezone
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 9, 23, 30, tzinfo=timezone.utc))
    starters = [{"date": "2026-10-09", "games": [{"team": "TOR", "goalie": "Anthony Stolarz", "status": "Confirmed"}]}]
    out = {}
    for start in (None, "2026-10-10T00:00:00Z", "2026-10-09T23:00:00Z"):
        players = [dict(espn(112, "Anthony Stolarz", "TOR", "G"), today_start=start), dict(espn(111, "Joseph Woll", "TOR", "G"), today_start=start)]
        rating.add_ratings(players, 2027, TODAY, MONDAY, starters)
        out[start] = {p["name"]: p for p in players}
    assert out["2026-10-10T00:00:00Z"]["Anthony Stolarz"]["cr_games"] == out[None]["Anthony Stolarz"]["cr_games"]
    gone = out["2026-10-09T23:00:00Z"]
    assert gone["Anthony Stolarz"]["cr_games"] == pytest.approx(out[None]["Anthony Stolarz"]["cr_games"] - 1, abs=0.01)
    assert gone["Anthony Stolarz"]["cr_why"]["p_today"] == 0
    assert gone["Anthony Stolarz"]["cr_why"]["p_today_pre"] == out[None]["Anthony Stolarz"]["cr_why"]["p_today"] > 0.5
    assert gone["Joseph Woll"]["cr_games"] == pytest.approx(out[None]["Joseph Woll"]["cr_games"], abs=0.01)
    assert rating.game_started({"today_start": "not a date"}) is False and rating.game_started({}) is False


def test_goalie_espn_columns_use_nhl_starts(monkeypatch, tmp_path):
    # ESPN's start count can run a game behind its points (findings 182): This yr/G and Proj/G use NHL starts
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    players = [dict(espn(pid, name, team, "G"), gp=1, cur_ppg=99.0, ppg=50.0, proj_ppg=3.0, last_ppg=2.5) for pid, name, team in
               [(111, "Joseph Woll", "TOR"), (112, "Anthony Stolarz", "TOR"), (113, "Jeremy Swayman", "BOS")]]
    rating.add_ratings(players, 2027, TODAY, MONDAY, [])
    started = [p for p in players if p["cr_why"]["gs"]]
    assert started
    for p in started:
        assert p["gp"] == p["cr_why"]["gs"] and p["cur_ppg"] < 99
        assert min(p["cur_ppg"], 3.0) - 0.01 <= p["ppg"] <= max(p["cur_ppg"], 3.0) + 0.01


def test_cleared_boost_skips_a_player_who_played_since_the_flag(monkeypatch, tmp_path):
    # McAvoy played Oct 5 and sat Oct 7: a flag from Oct 3 was played through, one from Oct 6 is fresh (findings 183)
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "load_news", lambda: {})
    out = {}
    for day in (None, "2026-10-03", "2026-10-06"):
        log = tmp_path / f"{day}.jsonl"
        log.write_text(json.dumps({"date": day, "espn_id": 104, "status": "DAY_TO_DAY"}) + "\n" if day else "")
        monkeypatch.setattr(rating, "INJURY_LOG", log)
        players = [espn(104, "Charlie McAvoy", "BOS", "D")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        out[day] = players[0]
    assert out[None]["sat_last"]
    assert out["2026-10-03"]["cr_dress"] == out[None]["cr_dress"]
    assert out["2026-10-06"]["cr_dress"] > out[None]["cr_dress"]
    # flagged Oct 5, the day he played: played through it, unless he left that game early (findings 213)
    for short, boosted in ((None, False), (200, True)):
        monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession(short_last={4: short} if short else None, short_date="2026-10-05"))
        res = {}
        for day in (None, "2026-10-05"):
            log = tmp_path / f"same{short}{day}.jsonl"
            log.write_text(json.dumps({"date": day, "espn_id": 104, "status": "DAY_TO_DAY"}) + "\n" if day else "")
            monkeypatch.setattr(rating, "INJURY_LOG", log)
            players = [espn(104, "Charlie McAvoy", "BOS", "D")]
            rating.add_ratings(players, 2027, TODAY, MONDAY, [])
            res[day] = players[0]["cr_dress"]
        assert (res["2026-10-05"] > res[None]) is boosted


def test_history_runs_to_last_night_in_an_overnight_run(monkeypatch):
    # 1 AM Sunday: ESPN is still on Saturday, but Saturday night's games are over (findings 183)
    sat = date(2026, 10, 10)
    monkeypatch.setattr(rating, "et_date", lambda: date(2026, 10, 11))
    assert rating.history_end(sat) == sat
    monkeypatch.setattr(rating, "et_date", lambda: sat)
    assert rating.history_end(sat) == date(2026, 10, 9)
    monkeypatch.setattr(rating, "et_date", lambda: date(2027, 1, 1))  # far-off clock: unchanged
    assert rating.history_end(sat) == date(2026, 10, 9)


def test_back_to_back_no1_early_in_the_season(monkeypatch):
    """Through a team's 10th game the no. 1 on night 2 gets an extra shift after the split; from the 11th, none (findings 213)."""
    model = rating.Model()

    def p(n_games, early):
        monkeypatch.setattr(rating, "B2B_NIGHT2_NO1_EARLY", early)
        hist = [((date(2026, 10, 1) + timedelta(days=2 * i)).isoformat(), 2 if i % 4 == 3 else 1) for i in range(n_games - 1)]
        last = date.fromisoformat(hist[-1][0]) + timedelta(days=2)
        hist.append((last.isoformat(), 1))
        nxt = last + timedelta(days=1)
        out = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, [nxt.isoformat()], nxt, today_p=out)
        return out
    shift = rating.B2B_NIGHT2_NO1_EARLY  # read before p() patches it
    early, off = p(6, shift), p(6, 0.0)
    assert early[1] < off[1] - 0.02 and early[1] + early[2] == pytest.approx(1)
    assert p(12, shift)[1] == pytest.approx(p(12, 0.0)[1])  # game 12: past EARLY_GAMES (10), whatever the skater depth rules use


def test_unsigned_player_counts_as_not_playing(monkeypatch, tmp_path):
    # ESPN team "FA" and no NHL games this season: 0.1 to dress, week and Season alike (Kane, Tarasenko; findings 216)
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession(missed={1: {0, 1, 2, 3}}))
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    players = [espn(101, "Auston Matthews", "FA", "C")]
    rating.add_ratings(players, 2027, TODAY, MONDAY, [])
    p = players[0]
    assert p["cr_dress"] == pytest.approx(rating.NOT_PLAYING_DRESS) and p["cr_season"] == pytest.approx(p["cr_why"]["season_fpg"] * rating.NOT_PLAYING_DRESS, abs=0.01)


def test_player_who_missed_last_season_uses_the_one_before(monkeypatch, tmp_path):
    # no games last season (Barkov): the season before, not a newcomer's prior (findings 216)
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    out, fakes = {}, {"plain": FakeSession(), "away": FakeSession(away_last={1})}
    for name, fake in fakes.items():
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        players = [espn(101, "Auston Matthews", "TOR", "C")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        out[name] = players[0]
    assert out["away"]["cr_fpg"] == pytest.approx(out["plain"]["cr_fpg"], abs=0.01)
    # the details say which season that line is from (Barkov, findings 234)
    assert out["away"]["cr_why"]["last_season"] == "2024-25" and "last_season" not in out["plain"]["cr_why"]
    # his MoneyPuck inputs come from that same season, so the luck terms don't mix two seasons
    older = lambda f: any("moneypuck" in u and "/2024/" in u for u in f.calls)
    assert older(fakes["away"]) and not older(fakes["plain"])


def test_goalie_out_for_the_season(monkeypatch):
    """A goalie out for the season has no Season share and his partner takes it (Shesterkin and Garand, findings 216)."""
    note = {"name": "Igor Shesterkin", "short": "Shesterkin (lower body) was placed on LTIR.", "date": "2026-10-10",
            "long": "Jonathan Quick will back up Garand. Shesterkin has been ruled out for the season after surgery."}
    assert rating.injury_note(note, date(2026, 10, 11))["season"]
    other = dict(note, name="Jonathan Quick", short="Quick is day-to-day.")
    assert not rating.injury_note(other, date(2026, 10, 11))["season"]  # the season-ending sentence is about a teammate
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 15, 2))]
    def sea(out):
        s = {}
        rating.goalie_starts(model, "NYR", [1, 2], {"NYR": hist}, {1: 50, 2: 32}, ["2026-10-22"], date(2026, 10, 22),
                             season_share=s, injury={1: 0.0} if out else None, season_out={1} if out else ())
        return s
    base, gone = sea(False), sea(True)
    assert gone[1] == 0 and gone[2] == pytest.approx(base[1] + base[2])


def test_early_prior_when_last_seasons_partner_left(monkeypatch):
    """The top goalie's share of last season's starts is capped at 0.7 and the excess goes to the others, so a no. 1 whose
    partner left isn't given a prior near 1, and the team's shares still add up (findings 217, 234)."""
    model = rating.Model()
    def share(prev, cap, goalies=(1, 2)):
        monkeypatch.setattr(rating, "PRIOR_TOP_CAP", cap)
        s = {}
        rating.goalie_starts(model, "BOS", list(goalies), {"BOS": [("2026-10-08", 1)]}, prev, ["2026-10-10"], date(2026, 10, 10), season_share=s)
        return s
    alone, before = share({1: 60, 2: 0}, 0.7), share({1: 60, 2: 0}, 1.0)
    assert alone[1] < before[1] - 0.05 and alone[2] > before[2] + 0.05
    assert sum(alone.values()) == pytest.approx(sum(before.values()))  # no share lost (the old floor lost up to 0.27)
    assert share({1: 50, 2: 32}, 0.7) == share({1: 50, 2: 32}, 1.0)  # 0.61: no change
    # the excess goes to the others in proportion to their own starts
    three = share({1: 80, 2: 15, 3: 5}, 0.7, (1, 2, 3))
    plain = share({1: 80, 2: 15, 3: 5}, 1.0, (1, 2, 3))
    assert three[2] - plain[2] > 2 * (three[3] - plain[3]) > 0
    assert share({1: 60}, 0.7, (1,)) == share({1: 60}, 1.0, (1,))  # one goalie: nothing to cap


def rate(monkeypatch, tmp_path, players, fake=None, starters=(), injuries=None, **kw):
    """add_ratings on these players with a fresh injury log each time (so one call's statuses don't clear the next's)."""
    monkeypatch.setattr(rating.requests, "Session", lambda: fake or FakeSession())
    log = tmp_path / "injury_log.jsonl"
    log.unlink(missing_ok=True)
    monkeypatch.setattr(rating, "INJURY_LOG", log)
    monkeypatch.setattr(rating, "load_news", lambda: {})
    rating.add_ratings(players, 2027, TODAY, MONDAY, list(starters), injuries=injuries, **kw)
    return {p["name"]: p for p in players}


def test_out_on_game_day_counts_about_zero(monkeypatch, tmp_path):
    """ESPN OUT on the day of his team's game, before it starts: that game counts 0.03; later games keep the OUT cap
    (0 of 20 dressed, findings 235)."""
    from datetime import datetime, timezone
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 9, 16, tzinfo=timezone.utc))

    def matthews(start, injury="OUT"):
        return rate(monkeypatch, tmp_path, [dict(espn(101, "Auston Matthews", "TOR", "C", injury=injury), today_start=start)])["Auston Matthews"]
    no_game, tonight, started = matthews(None), matthews("2026-10-09T23:00:00Z"), matthews("2026-10-09T15:00:00Z")
    assert no_game["cr_dress_next"] == pytest.approx(rating.SKATER_INJURY_CAP["OUT"], abs=0.01)
    assert tonight["cr_dress_next"] == rating.OUT_TODAY_CAP
    assert tonight["cr_games"] == pytest.approx(no_game["cr_games"] - rating.SKATER_INJURY_CAP["OUT"] + rating.OUT_TODAY_CAP, abs=0.02)
    assert started["cr_games"] == no_game["cr_games"]  # his game already started: games left start tomorrow
    assert matthews("2026-10-09T23:00:00Z", "DAY_TO_DAY")["cr_games"] == matthews(None, "DAY_TO_DAY")["cr_games"]


def test_bare_status_word_keeps_the_last_real_note(monkeypatch, tmp_path):
    """ESPN replaces the real note with 'IR': the last real one is kept in injury_notes.json and still read, if his status
    is as severe, it wasn't a day-to-day note, and he hasn't played since (findings 236)."""
    notes_file = rating.INJURY_NOTES
    real = {"athlete_id": 103, "name": "David Pastrnak", "short": "Pastrnak (upper body) will undergo surgery Tuesday.", "long": "",
            "date": "2026-10-08T12:00Z"}
    bare = dict(real, short="IR", date="2026-10-09T10:00Z")

    def pasta(note, status="INJURY_RESERVE"):
        return rate(monkeypatch, tmp_path, [espn(103, "David Pastrnak", "BOS", "RW", injury=status)], injuries=[note])["David Pastrnak"]
    assert not notes_file.exists()
    assert pasta(bare)["cr_dress"] == pytest.approx(rating.SKATER_INJURY_CAP["INJURY_RESERVE"], abs=0.01)  # nothing kept yet
    assert pasta(real)["cr"] == 0
    kept = json.loads(notes_file.read_text())
    assert kept["103"]["short"] == real["short"] and kept["103"]["status"] == "INJURY_RESERVE" and kept["103"]["date"] == real["date"]
    assert pasta(bare)["cr"] == 0 and pasta(bare)["cr_why"]["back"] == {"games": "16"}
    assert json.loads(notes_file.read_text())["103"]["short"] == real["short"]  # a bare word never overwrites it
    assert pasta(bare, "DAY_TO_DAY")["cr_dress"] > 0.1  # a lesser status now: today's note
    assert pasta(dict(bare, short="out"), "OUT")["cr_dress"] > 0  # OUT is less severe than the IR it was written under
    # played since the note: it's old news
    notes_file.write_text(json.dumps({"103": {**kept["103"], "date": "2026-10-05T12:00Z"}}))
    assert pasta(bare)["cr_dress"] > 0
    # written under a day-to-day status: not carried
    notes_file.write_text(json.dumps({"103": {**kept["103"], "status": "DAY_TO_DAY"}}))
    assert pasta(bare)["cr_dress"] > 0
    # notes not seen in 60 days are dropped
    notes_file.write_text(json.dumps({"999": {"date": "2026-07-01T12:00Z", "short": "x", "seen": "2026-07-01"},
                                      "998": {"date": "2026-07-01T12:00Z", "short": "y", "seen": "2026-10-01"}}))
    pasta(bare)
    assert set(json.loads(notes_file.read_text())) == {"998"}
    # an unreadable file is treated as empty
    notes_file.write_text("not json")
    assert pasta(bare)["cr_dress"] > 0


def test_bare_status_word_keeps_a_goalie_out_for_the_season(monkeypatch, tmp_path):
    """Shesterkin's 'out for the remainder of the season' became 'ir': he stays out of the Season split (findings 236)."""
    note = {"athlete_id": 111, "name": "Joseph Woll", "short": "Woll will miss the remainder of the season.", "long": "",
            "date": "2026-10-08T12:00Z"}

    def toronto(n):
        return rate(monkeypatch, tmp_path, [espn(111, "Joseph Woll", "TOR", "G", injury="INJURY_RESERVE"),
                                            espn(112, "Anthony Stolarz", "TOR", "G")], injuries=[n])
    first = toronto(note)
    assert first["Joseph Woll"]["cr_season"] == 0
    later = toronto(dict(note, short="IR"))
    assert later["Joseph Woll"]["cr_season"] == 0 and later["Anthony Stolarz"]["cr_season"] == first["Anthony Stolarz"]["cr_season"]
    rating.INJURY_NOTES.unlink()
    assert toronto(dict(note, short="IR"))["Joseph Woll"]["cr_season"] > 0  # nothing kept: today's bare word only


def test_injury_note_wording_for_long_absences():
    """Long-absence phrases and return months the old parser missed, and stat lines it read as return dates (findings 236)."""
    day = date(2026, 10, 9)

    def note(short, long_=""):
        return rating.injury_note({"short": short, "long": long_, "name": "John Smith", "date": "2026-10-08T12:00Z"}, day)
    assert note("Smith (lower body) is out without a timetable.")["long"]
    assert note("Smith (upper body) is not expected back until November.")["days_left"] == 23
    assert note("Smith (upper body) is not expected back until November.")["long"]
    assert note("Smith (knee) is a few weeks away.")["long"]
    assert note("Smith (knee) will be out until at least mid-December.")["long"]
    assert note("Smith (knee) could be back before Christmas.")["long"]
    assert note("Smith (knee) is day-to-day.", "Smith is considered week-to-week by the team.")["long"]
    assert not note("Smith (knee) is day-to-day.", "Jones (knee) is week-to-week.")["long"]  # a teammate's sentence
    assert note("Smith (knee) is day-to-day.", "The team says it is unclear when he will return.")["long"]
    assert not note("Smith (knee) is day-to-day.", "He had surgery last summer.")["long"]  # surgery in the long comment is a recap
    assert not note("Smith (knee) met with hockey operations staff.")["long"]
    assert note("Smith (knee) requires an operation.")["long"]
    assert not note("Smith (illness) won't play Thursday.")["long"]
    # stat lines and old absences aren't return dates
    assert note("Smith (illness) is out; he has four points over five games.")["days_left"] is None
    assert note("Smith (hip) is out after he missed the final 26 games of 2025-26.")["days_left"] is None
    assert note("Smith (hip) will be out for the first three weeks of the season.")["days_left"] == pytest.approx(20)
    assert note("Smith (knee) is expected to miss 4-6 weeks.")["days_left"] == pytest.approx(34)


def test_one_game_out_note_expires_once_that_game_is_played(monkeypatch, tmp_path):
    """OUT for one named game: once his team has played after the note, the day-to-day cap (findings 236)."""
    def pasta(when, short="Pastrnak won't play Tuesday against Toronto."):
        inj = [{"athlete_id": 103, "name": "David Pastrnak", "short": short, "long": "", "date": when}]
        return rate(monkeypatch, tmp_path, [espn(103, "David Pastrnak", "BOS", "RW", injury="OUT")], injuries=inj)["David Pastrnak"]
    assert pasta("2026-10-08T14:00Z")["cr_dress_next"] == pytest.approx(rating.SKATER_INJURY_CAP["OUT"], abs=0.01)  # no game since
    assert pasta("2026-10-06T14:00Z")["cr_dress_next"] == pytest.approx(rating.SKATER_INJURY_CAP["DAY_TO_DAY"], abs=0.01)
    assert pasta("2026-10-06T14:00Z", "Pastrnak will miss two games.")["cr_dress_next"] == pytest.approx(rating.SKATER_INJURY_CAP["OUT"], abs=0.01)
    over = rating.one_game_out_over
    assert over({"short": "Smith won't play Saturday.", "date": "2026-10-07T14:00Z"}, {"2026-10-07"})  # 10 AM ET, game that night
    assert not over({"short": "Smith won't play Saturday.", "date": "2026-10-07T23:30Z"}, {"2026-10-07"})  # posted at 7:30 PM ET
    assert not over({"short": "Smith will miss 2-3 weeks.", "date": "2026-10-01T14:00Z"}, {"2026-10-07"})
    assert not over({"short": "Smith (knee) is out.", "date": "2026-10-01T14:00Z"}, {"2026-10-07"})


def test_season_out_share_also_goes_to_hurt_partners():
    """A goalie out for the season: his Season share goes to every other goalie, an injured one at half weight (findings 234)."""
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", [1, 1, 2, 1, 3][i % 5]) for i, d in enumerate(range(1, 21, 2))]

    def sea(injury, season_out):
        s = {}
        rating.goalie_starts(model, "NYR", [1, 2, 3], {"NYR": hist}, {1: 50, 2: 28, 3: 4}, ["2026-10-22"], date(2026, 10, 22),
                             season_share=s, injury=injury, season_out=season_out)
        return s
    base, gone = sea({}, ()), sea({1: 0.0, 2: 0.0}, {1})
    w2, w3 = rating.INJURED_TAKER_WEIGHT * base[2], base[3]
    assert gone[1] == 0
    assert gone[2] == pytest.approx(base[2] + base[1] * w2 / (w2 + w3))
    assert gone[3] == pytest.approx(base[3] + base[1] * w3 / (w2 + w3))


def test_back_to_back_no1_counts_only_goalies_still_here(monkeypatch):
    """Night 2: the no. 1 is the most-used of the goalies still on the team, not a departed one (findings 234)."""
    model = rating.Model()
    seq = [9] * 12 + [2, 1, 2, 2]  # 9 was traded away; 2 is the no. 1 now and started night 1
    hist = [((date(2026, 10, 1) + timedelta(days=2 * i)).isoformat(), g) for i, g in enumerate(seq)]
    nxt = date.fromisoformat(hist[-1][0]) + timedelta(days=1)

    def p2(no1, other):
        monkeypatch.setattr(rating, "B2B_NIGHT2_NO1", no1)
        monkeypatch.setattr(rating, "B2B_NIGHT2_OTHER", other)
        out = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 30, 2: 30, 9: 50}, [nxt.isoformat()], nxt, today_p=out)
        return out[2]
    assert p2(-0.8, 0.0) < p2(0.0, 0.0) - 0.01
    assert p2(0.0, 0.9) == pytest.approx(p2(0.0, 0.0))


def test_team_with_no_game_this_week_keeps_its_goalies_season(monkeypatch, tmp_path):
    """BOS has no game this week, or the rosters can't be read: its goalies keep their Season rating (findings 230)."""
    def goalies(fake, bos_games=3):
        return rate(monkeypatch, tmp_path, [espn(113, "Jeremy Swayman", "BOS", "G", games_left=bos_games), espn(111, "Joseph Woll", "TOR", "G"),
                                            espn(112, "Anthony Stolarz", "TOR", "G")], fake=fake)
    normal, bye, down = goalies(FakeSession()), goalies(FakeSession(bye=True), 0), goalies(FakeSession(roster_down={"TOR", "BOS"}))
    assert bye["Jeremy Swayman"]["cr_season"] == normal["Jeremy Swayman"]["cr_season"] > 0
    assert bye["Jeremy Swayman"]["cr_games"] == 0 and bye["Jeremy Swayman"]["cr_g_nights"] == {}
    for name in normal:  # rosters unreadable: the goalies who started for each team stand in
        assert down[name]["cr_season"] == normal[name]["cr_season"] > 0
        assert down[name]["cr_games"] == normal[name]["cr_games"]


def test_espn_free_agent_on_an_nhl_roster_gets_his_games(monkeypatch, tmp_path):
    """ESPN lists him as 'FA' but he plays for TOR: fetch.py's reschedule gives him its games (Bunting, findings 230)."""
    calls = []

    def reschedule(p, team):
        calls.append((p["name"], team))
        p.update(team="TOR", games=4, games_left=2, nights=[7, 9, 11, 13])
    p = rate(monkeypatch, tmp_path, [espn(101, "Auston Matthews", "FA", "C", games_left=0)], reschedule=reschedule)["Auston Matthews"]
    assert calls == [("Auston Matthews", "TOR")] and p["cr_games"] > 1.5 and p["cr"] > 0
    calls.clear()  # unsigned and yet to play: stays a free agent with no games (findings 216)
    rate(monkeypatch, tmp_path, [espn(101, "Auston Matthews", "FA", "C", games_left=0)], FakeSession(missed={1: {0, 1, 2, 3}}),
         reschedule=reschedule)
    assert calls == []


def test_offseason_mover_rated_a_little_lower(monkeypatch, tmp_path):
    """On a new team since last season and with one team this season: points a game x 0.965 (findings 230)."""
    def matthews(mult, **kw):
        monkeypatch.setattr(rating, "OFFSEASON_MOVER_MULT", mult)
        return rate(monkeypatch, tmp_path, [espn(101, "Auston Matthews", "TOR", "C")], FakeSession(**kw))["Auston Matthews"]
    moved, plain = matthews(0.965, last_team={1: "BOS"}), matthews(1.0, last_team={1: "BOS"})
    assert moved["cr_fpg"] == pytest.approx(0.965 * plain["cr_fpg"], abs=0.01)  # both rounded to 2 places
    assert moved["cr_season"] < plain["cr_season"]
    assert matthews(0.965)["cr_fpg"] == matthews(1.0)["cr_fpg"]  # same team as last season
    # traded this season (2 games for BOS, then TOR): the in-season mover rules apply, not this
    assert matthews(0.965, last_team={1: "BOS"}, moved={1: 2})["cr_fpg"] == matthews(1.0, last_team={1: "BOS"}, moved={1: 2})["cr_fpg"]


def test_details_fields(run):
    """The why panel's numbers: a goalie's own points per start and team games counted, the share a player yet to play is
    given (findings 232), and the start chance by night (findings 236)."""
    by_name, _, _ = run
    for name in ("Joseph Woll", "Anthony Stolarz", "Jeremy Swayman"):
        p = by_name[name]
        assert p["cr_why"]["n10"] == 4 and p["cr_why"]["own_pps"] > 0
        assert sum(p["cr_g_nights"].values()) == pytest.approx(p["cr_why"]["exp_starts"], abs=0.02)
        assert all(0 <= q <= 1 for q in p["cr_g_nights"].values())
    sto = by_name["Anthony Stolarz"]
    assert set(sto["cr_g_nights"]) == {"2026-10-09", "2026-10-11"} and sto["cr_g_nights"]["2026-10-09"] == 1.0  # confirmed
    assert by_name["Brad Marchand"]["cr_why"]["season_share"] == rating.PRESEASON_INJURED["short"]
    assert by_name["Ryan Spooner"]["cr_why"]["season_share"] == round(rating.unplayed_share(80, 4), 2)  # BOS has played 4
    assert "season_share" not in by_name["Auston Matthews"]["cr_why"]
    assert all("cr_g_nights" not in p for p in by_name.values() if p["pos"] != "G")


def test_one_game_out_rule_uses_real_start_times(monkeypatch, tmp_path):
    """A pregame note posted after 5 PM ET (Bogosian 5:37 PM): with the game's start time, the game that night is the one
    he missed, so he's back to the day-to-day cap; a note posted after the puck dropped still waits (findings 241 (2))."""
    note = {"athlete_id": 103, "name": "David Pastrnak", "short": "Pastrnak won't play Wednesday.", "long": "",
            "date": "2026-10-07T21:37Z"}  # 5:37 PM ET on Oct 7, BOS's last game

    def pasta(starts=None):
        return rate(monkeypatch, tmp_path, [espn(103, "David Pastrnak", "BOS", "RW", injury="OUT")], FakeSession(starts=starts),
                    injuries=[note])["David Pastrnak"]["cr_dress_next"]
    assert pasta() == pytest.approx(rating.SKATER_INJURY_CAP["OUT"], abs=0.01)  # no start time: the 5 PM rule
    assert pasta({"2026-10-07": "2026-10-07T23:00:00Z"}) == pytest.approx(rating.SKATER_INJURY_CAP["DAY_TO_DAY"], abs=0.01)
    assert pasta({"2026-10-07": "2026-10-07T21:00:00Z"}) == pytest.approx(rating.SKATER_INJURY_CAP["OUT"], abs=0.01)  # in progress
    over = rating.one_game_out_over
    late = {"short": "Smith won't play Saturday.", "date": "2026-10-07T22:53Z"}  # 6:53 PM ET
    assert over(late, {"2026-10-07"}, {"2026-10-07": "2026-10-07T23:00:00Z"})
    assert not over(late, {"2026-10-07"}, {"2026-10-07": "2026-10-07T22:30:00Z"})
    assert over(dict(late, date="2026-10-07T14:00Z"), {"2026-10-07"}, {})  # no start: before 5 PM counts as before


def test_unsigned_free_agent_with_a_rostered_players_name_isnt_matched(monkeypatch, tmp_path):
    """ESPN's unsigned 'Matt Murray' (FA) shares a name with a rostered NHL goalie: only the rostered one gets his stats
    (findings 241 (4))."""
    players = [espn(111, "Joseph Woll", "TOR", "G"), espn(999, "Joseph Woll", "FA", "G", games_left=0)]
    rate(monkeypatch, tmp_path, players)
    real, ghost = players
    assert real["nhl_id"] == 11 and real["cr_season"] > 0
    assert ghost["nhl_id"] is None and not ghost["cr_matched"] and ghost["cr_season"] == 0
    alone = rate(monkeypatch, tmp_path, [espn(999, "Joseph Woll", "FA", "G", games_left=0)])["Joseph Woll"]
    assert alone["nhl_id"] == 11  # nobody on ESPN's rosters has the name: matched as before


def test_goalie_dropped_after_the_first_puck_counts_only_tonight(monkeypatch, tmp_path):
    """A pending drop keeps only today's game for his fantasy team (findings 241 (3)): at most tonight's start chance, and
    no start chances after today."""
    def sway(games_left, **kw):
        return rate(monkeypatch, tmp_path, [dict(espn(113, "Jeremy Swayman", "BOS", "G", games_left=games_left), **kw)])["Jeremy Swayman"]
    kept, dropped = sway(2), sway(1, pending_drop=True)
    assert kept["cr_games"] > 1.5
    assert dropped["cr_games"] <= 1 and dropped["cr_games"] == pytest.approx(dropped["cr_why"]["p_today"], abs=0.01)
    assert set(dropped["cr_g_nights"]) <= {TODAY.isoformat()}
    gone = sway(0, pending_drop=True)
    assert gone["cr_games"] == 0 and gone["cr"] == 0


def test_kept_note_edge_cases(monkeypatch, tmp_path):
    """Three ways the kept injury note could be lost (findings 241 (5))."""
    notes_file = rating.INJURY_NOTES
    real = {"athlete_id": 103, "name": "David Pastrnak", "short": "Pastrnak (upper body) will undergo surgery Tuesday.", "long": "",
            "date": "2026-10-07T23:30Z"}  # 7:30 PM ET during BOS's Oct 7 game, which he played
    bare = dict(real, short="IR", date="2026-10-09T10:00Z")

    def pasta(note, fake=None):
        return rate(monkeypatch, tmp_path, [espn(103, "David Pastrnak", "BOS", "RW", injury="INJURY_RESERVE")], fake,
                    injuries=[note])["David Pastrnak"]
    # (2) a note posted during a game he played isn't old news
    assert pasta(real)["cr"] == 0
    assert pasta(bare)["cr"] == 0
    assert pasta(bare, FakeSession(starts={"2026-10-07": "2026-10-07T23:00:00Z"}))["cr"] == 0  # started before the note
    assert pasta(bare, FakeSession(starts={"2026-10-07": "2026-10-08T00:00:00Z"}))["cr"] > 0  # he played after it
    # (1) a note in use is seen today, so a season-out note shown as 'ir' for 60+ days isn't pruned
    old = {"date": "2026-08-01T12:00Z", "short": "Marchand will miss the remainder of the season.", "long": "",
           "name": "Brad Marchand", "status": "INJURY_RESERVE", "seen": "2026-08-01"}
    notes_file.write_text(json.dumps({"109": old}))
    m = rate(monkeypatch, tmp_path, [espn(109, "Brad Marchand", "BOS", "LW", injury="INJURY_RESERVE")],
             injuries=[{"athlete_id": 109, "name": "Brad Marchand", "short": "ir", "long": "", "date": "2026-10-09T10:00Z"}])
    assert m["Brad Marchand"]["cr"] == 0
    assert json.loads(notes_file.read_text())["109"]["seen"] == TODAY.isoformat()
    # (3) a note on a player outside the data keeps the feed's own status, so it can be carried later
    notes_file.unlink()
    rate(monkeypatch, tmp_path, [espn(101, "Auston Matthews", "TOR", "C")],
         injuries=[dict(real, athlete_id=555, name="Someone Else", status="Injured Reserve")])
    assert json.loads(notes_file.read_text())["555"]["status"] == "INJURY_RESERVE"


def test_next_up_power_play_defenseman():
    """The top PP D (1+ PP min/G) missed the last game or is OUT: the next healthy D who played it gets the boost (findings 310 (5))."""
    tg = {"TOR": [1, 2, 3, 4, 5, 6]}
    game = lambda pid, name, g, pp, pos="D": {"game": g, "team": "TOR", "pos": pos, "pptoi": pp, "name": name}
    logs = {
        1: [game(1, "Top D", g, 3.0) for g in (1, 2, 3, 4, 5)],  # missed game 6
        2: [game(2, "Second D", g, 1.5) for g in (1, 2, 3, 4, 5, 6)],
        3: [game(3, "Third D", g, 0.5) for g in (1, 2, 3, 4, 5, 6)],
        4: [game(4, "PP forward", g, 4.0, "C") for g in (1, 2, 3, 4, 5, 6)],
    }
    assert rating.next_up_pp_d(logs, tg, set()) == {2: "Top D"}
    logs[1].append(game(1, "Top D", 6, 3.0))
    assert rating.next_up_pp_d(logs, tg, set()) == {}  # he played: nothing
    assert rating.next_up_pp_d(logs, tg, {1}) == {2: "Top D"}  # ESPN has him out
    assert rating.next_up_pp_d(logs, tg, {1, 2}) == {3: "Top D"}  # the second is out too: the third steps up
    for g in logs[1]:
        g["pptoi"] = 0.8
    assert rating.next_up_pp_d(logs, tg, {1}) == {}  # under 1 PP min/G: no PP role to hand on


def test_season_age_adjustment():
    """Season FP/G + 0.01 a year under 24, - 0.009 a year over 29; nothing without a birth date (findings 310 (3))."""
    today = date(2026, 10, 12)
    assert rating.season_age_adj(None, today) == 0.0
    assert rating.season_age_adj(date(2000, 10, 12), today) == pytest.approx(0.0, abs=1e-3)  # 26
    assert rating.season_age_adj(date(2006, 10, 12), today) == pytest.approx(0.04, abs=1e-3)  # 20
    assert rating.season_age_adj(date(1991, 10, 12), today) == pytest.approx(-0.054, abs=1e-3)  # 35


def test_season_rating_carries_age(run):
    by_name, _, _ = run
    old, young = by_name["Elias N. Pettersson"], by_name["Zack Bolduc"]
    for p, sign in ((old, -1), (young, 1)):
        w = p["cr_why"]
        assert w.get("age_adj") is None or w["age_adj"] * sign > 0
    assert young["cr_why"]["age_adj"] == pytest.approx(0.03, abs=0.005)
    assert old["cr_why"]["age_adj"] == pytest.approx(-0.08, abs=0.005)
    assert "age_adj" not in by_name["Auston Matthews"]["cr_why"]  # 29: no adjustment


def test_season_games_left_for_a_traded_player_yet_to_play():
    """His old team's schedule is cut at his last game, but games left counts the team's games so far (review item 2)."""
    model = rating.Model()
    games = [{"game": g, "team": "TOR"} for g in (1, 2, 3)]
    f = {"is_D": False}
    cut = rating.season_dress_features(games, "TOR", [1, 2, 3], f, {}, None, 2.0, model.season_lr["markov"], played=8)
    full = rating.season_dress_features(games, "TOR", [1, 2, 3], f, {}, None, 2.0, model.season_lr["markov"])
    assert cut["games_left"] == rating.SEASON_GAMES - 8 and full["games_left"] == rating.SEASON_GAMES - 3


def test_season_range_brackets_the_season_rating(run):
    """Every matched skater gets an 80% Season range around his Season rating, never below 0 (findings 345)."""
    by_name, _, _ = run
    rated = [p for p in by_name.values() if p["pos"] != "G" and p["cr_matched"]]
    assert rated
    for p in rated:
        lo, hi = p["cr_why"]["season_range"]
        assert 0 <= lo <= p["cr_season"] + 0.01 and hi >= p["cr_season"] - 0.01, (p["name"], lo, hi, p["cr_season"])
        assert p["cr_why"].get("exp") in (None, "rookie", "year 2")


def test_season_range_leans_up_for_rookies():
    """Rookies and second-year forwards finish further above their Season rating than veterans (findings 345)."""
    m = rating.Model()
    vet, exp = m.season_range(1.0, False, 80, 80)
    assert exp == "veteran"
    rookie, exp = m.season_range(1.0, False, 10, 0)
    assert exp == "rookie" and rookie[1] > vet[1]
    assert m.season_range(1.0, False, 70, 10)[1] == "year 2"
    assert m.season_range(0.1, True, 0, 0)[0][0] == 0  # the low end never goes below 0
    assert m.season_range(1.0, False, 20, 0, age=33)[1] == "veteran"  # a veteran back from injury isn't a rookie


def test_season_share_follows_the_save_pct_gap(monkeypatch):
    """The goalie stopping more pucks than his partners gets more of the Season share; nightly starts don't change
    (findings 330 (1), handoff 270)."""
    prev = {1: (50, 0.0, 1500, 1650), 2: (30, 0.0, 800, 900)}
    games = [{"id": 1, "sv": 280, "sa": 300}, {"id": 2, "sv": 250, "sa": 290}]
    sv, lg = rating.shrunk_save_pcts(games, prev)
    assert sv[1] > sv[2] and 0.85 < lg < 0.95
    model = rating.Model()
    hist = [((date(2026, 10, 1) + timedelta(days=2 * i)).isoformat(), 1 if i % 3 else 2) for i in range(9)]
    def run(sp):
        sea, today = {}, {}
        e = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 30}, ["2026-10-20"], date(2026, 10, 20),
                                 today_p=today, season_share=sea, save_pcts=sp)
        return sea, e
    (on, e_on), (off, e_off) = run((sv, lg)), run(None)
    gap1 = (sv[1] - sv[2]) * 100
    assert on[1] == pytest.approx(off[1] + rating.SAVE_GAP_SEASON[0] + rating.SAVE_GAP_SEASON[1] * gap1)
    assert on[2] == pytest.approx(off[2] + rating.SAVE_GAP_SEASON[0] - rating.SAVE_GAP_SEASON[1] * gap1)
    assert e_on == e_off  # Season only


def test_goalie_season_is_on_the_skaters_scale(run):
    """A goalie with any share of his team's starts gets intercept + slope x (share x pts/start), so goalie Season values
    sit on the skaters' scale (findings 330 (2), handoff 271)."""
    players, _, _ = run
    a, b = rating.GOALIE_SEASON_SCALE
    for name in ("Joseph Woll", "Anthony Stolarz", "Jeremy Swayman"):
        s = players[name]["cr_season"]
        assert s == 0 or s >= a - 0.01, (name, s)
    assert 0.2 < a < 0.5 and 0.5 < b < 1


def test_finished_weeks_of_nhl_stats_are_cached(monkeypatch, tmp_path):
    """With NHL_CACHE set, a week that ended 3+ days ago is read from disk after the first call; recent weeks and season
    totals always ask the API (speed audit plan 3)."""
    monkeypatch.setattr(rating, "NHL_CACHE", str(tmp_path))
    monkeypatch.setattr(rating, "et_date", lambda: date(2026, 10, 20))
    calls = []

    class S:
        def get(self, url, params=None, headers=None, timeout=None):
            calls.append(params.get("cayenneExp"))
            return Resp({"data": [{"playerId": len(calls)}]})
    first = rating.stats_report(S(), "skater/summary", 20262027, "2026-10-06", "2026-10-12")
    assert rating.stats_report(S(), "skater/summary", 20262027, "2026-10-06", "2026-10-12") == first and len(calls) == 1
    rating.stats_report(S(), "skater/summary", 20262027, "2026-10-13", "2026-10-19")
    rating.stats_report(S(), "skater/summary", 20262027, "2026-10-13", "2026-10-19")  # ended yesterday: not cached
    rating.stats_report(S(), "skater/summary", 20262027)
    rating.stats_report(S(), "skater/summary", 20262027)  # season totals: not cached
    assert len(calls) == 5 and len(list(tmp_path.iterdir())) == 1


def test_pulled_goalie_loses_a_little_season_share():
    """A goalie pulled from his latest start loses PULLED_SEASON of his Season share; nightly starts don't change, and a
    goalie the no-decision cut already covers loses nothing more (findings 330 (3), handoff 272)."""
    model = rating.Model()
    hist = [((date(2026, 10, 1) + timedelta(days=2 * i)).isoformat(), 1 if i % 3 else 2) for i in range(9)]
    def run(pulled, last_start=None):
        sea = {}
        e = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 30}, ["2026-10-20"], date(2026, 10, 20),
                                 today_p={}, season_share=sea, last_start=last_start, pulled=pulled)
        return sea, e
    (on, e_on), (off, e_off) = run({1}), run(())
    assert on[1] == pytest.approx(off[1] - rating.PULLED_SEASON) and on[2] == pytest.approx(off[2])
    assert e_on == e_off  # Season only
    nd = (1, rating.last_start_shift({"ga": 1, "sv": 20, "sa": 21, "toi": 2300, "wins": 0, "losses": 0, "otl": 0}))
    assert nd[1][0] == rating.NO_DECISION_LOGIT
    assert run({1}, nd)[0] == run((), nd)[0]


def test_durable_skater_out_long_comes_back_sooner():
    """Out 6+ team games: last season's dress rate raises the Season dress share, FP/G lowers it a little; players out
    fewer games are unchanged (findings 330 (4), handoff 273)."""
    model = rating.Model()
    team = list(range(1, 21))
    def share(played, gp):
        gl = [{"game": g, "team": "TOR", "toi": 17.0, "date": f"2026-11-{g:02d}"} for g in played]
        f = {"dressed3": sum(g in played for g in team[-3:]) / 3, "dressed10": sum(g in played for g in team[-10:]) / 10,
             "std_toi": 17.0, "l5_toi": 17.0, "std_pptoi": 2.0, "is_D": 0.0}
        df = rating.dress_features(gl, "TOR", team, f, date(2026, 11, 21))
        x = rating.season_dress_features(gl, "TOR", team, f, df, {"gp": gp}, 2.0, model.season_lr["markov"])
        return model.season_dress(x), x
    (on, x), off = share(team[:10], 82), None
    a, b = rating.DURABLE_OUT
    rating.DURABLE_OUT = (0.0, 0.0)
    try:
        off = share(team[:10], 82)[0]
        healthy_off = share(team, 82)[0]
    finally:
        rating.DURABLE_OUT = (a, b)
    z = lambda p: math.log(p / (1 - p))
    assert x["os_long"] == 1.0 and z(on) - z(off) == pytest.approx(a * 1.0 + b * 2.0)
    assert share(team, 82)[0] == pytest.approx(healthy_off)


def test_offseason_goalie_mover_carries_his_old_rate_adjusted_for_the_team_change(monkeypatch, tmp_path):
    """Swayman played last season for TOR (points% 0.65) and now for BOS (0.45): his Season pts/start drops by
    3 x 0.20 x 15 / (his starts + 15); the Week rating is unchanged (findings 330 (5), handoff 274)."""
    def sway(**kw):
        return rate(monkeypatch, tmp_path, [espn(113, "Jeremy Swayman", "BOS", "G")], fake=FakeSession(**kw))["Jeremy Swayman"]
    moved, stay = sway(last_team={13: "TOR"}), sway()
    assert moved["cr"] == stay["cr"] and moved["cr_season"] < stay["cr_season"]
    assert "mover_pps" in moved["cr_why"] and "mover_pps" not in stay["cr_why"]
    assert moved["cr_why"]["mover_pps"] < moved["cr_why"]["own_pps"]


def test_no1_next_game_sunday_after_a_day_off_and_on_the_road():
    """The no. 1's next-game chance drops on a Sunday or Monday after one day off (findings 254, handoff 291), and
    on the road otherwise (findings 257, handoff 292); later games and home games keep their rules."""
    model = rating.Model()
    # Fri Oct 16 2026 is the last game played; goalie 1 started most of the last 20
    hist = [((date(2026, 10, 16) - timedelta(days=2 * (8 - i))).isoformat(), 2 if i % 4 == 0 else 1) for i in range(9)]
    def p1(day, road=()):
        out = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 30}, [day], date(2026, 10, 17), by_date=out, road=road)
        return out[1][day]
    def without(fn):
        old = rating.SUNMON_REST1_NO1_LOGIT, rating.ROAD_NO1_LOGIT
        rating.SUNMON_REST1_NO1_LOGIT = rating.ROAD_NO1_LOGIT = 0.0
        try:
            return fn()
        finally:
            rating.SUNMON_REST1_NO1_LOGIT, rating.ROAD_NO1_LOGIT = old
    sun, tue = "2026-10-18", "2026-10-20"
    assert p1(sun) < without(lambda: p1(sun))  # Sunday after one day off
    assert p1(sun, {sun}) == p1(sun)  # the road cut doesn't stack on it
    assert p1(tue) == without(lambda: p1(tue))  # Tuesday at home: unchanged
    assert p1(tue, {tue}) < p1(tue)  # Tuesday on the road


def test_no1_by_opponent_form_and_rest():
    """Handoff 333 (findings 262-263): the no. 1's logit gets +0.2 per 0.10 of opponent points% above .550, +0.2 - 0.25 per
    straight start without a win (cap 3, half weight 2+ games ahead), and +0.4 after 3+ days without a game."""
    model = rating.Model()
    hist = [((date(2026, 10, 16) - timedelta(days=2 * (8 - i))).isoformat(), 2 if i % 4 == 0 else 1) for i in range(9)]
    tue, wed, sat = "2026-10-20", "2026-10-21", "2026-10-24"
    def p1(days, **kw):
        out = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 30}, days, date(2026, 10, 17), by_date=out, **kw)
        return out[1]
    base = p1([tue, wed, sat])
    hot = p1([tue, wed, sat], opp={}, nonwins={})  # rested 4 days, last start a win: +0.6 on Tuesday
    cold = p1([tue, wed, sat], opp={}, nonwins={1: 5})  # 3 non-wins (capped): -0.55 next game, half later
    strong = p1([tue, wed, sat], opp={tue: 2.0, sat: -1.0}, nonwins={1: 1})
    assert hot[tue] > base[tue] and cold[tue] < base[tue] and strong[tue] > p1([tue, wed, sat], opp={}, nonwins={1: 1})[tue]
    assert cold[sat] > p1([tue, wed, sat], opp={}, nonwins={1: 9})[sat] - 1e-9 and cold[sat] < hot[sat]
    assert p1([tue], opp={tue: 2.0}) == p1([tue])  # without nonwins nothing changes


def test_no1_after_split_back_to_back_and_backup_results():
    """Handoffs 334-335 (findings 264-267), next game only: +0.8 when the no. 1 started one night of a back-to-back and the
    next game is 2 days after night 2; +0.8 when a backup started and didn't win the last game; -0.6 when the latest backup
    start was a win 2-3 team games ago."""
    model = rating.Model()
    def p1(hist, wins, days, **kw):
        out = {}
        rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 30}, days, date(2026, 10, 20), by_date=out,
                             start_wins=wins, **kw)
        return out[1]
    day = lambda n: (date(2026, 10, 1) + timedelta(days=n)).isoformat()
    base = [(day(2 * i), 1) for i in range(8)]  # goalie 1 the clear no. 1, a game every other day to Oct 15
    # back-to-back Oct 17-18 split 1 then 2 (2 won), next game Oct 20
    split = base + [(day(16), 1), (day(17), 2)]
    on, off = p1(split, [True] * 10, [day(19)]), p1(split, None, [day(19)])
    assert on[day(19)] > off[day(19)]
    # backup lost the last game (Oct 17, two days off): +0.8; backup won it 2 games ago: -0.6
    lost = base + [(day(16), 2)]
    assert p1(lost, [True] * 8 + [False], [day(19)])[day(19)] > p1(lost, None, [day(19)])[day(19)]
    won = base + [(day(15), 2), (day(17), 1)]
    assert p1(won, [True] * 10, [day(19)])[day(19)] < p1(won, None, [day(19)])[day(19)]
    # later games are untouched
    two = p1(won, [True] * 10, [day(19), day(21)])
    assert abs(two[day(21)] - p1(won, None, [day(19), day(21)])[day(21)]) < 0.05


def test_opponent_strength_and_nonwin_streaks():
    g = lambda gid, team, game, day, w=0, otl=0, started=1: {"id": gid, "team": team, "game": game, "date": day, "wins": w, "otl": otl, "started": started}
    games = [g(1, "TOR", 1, "2026-10-08", w=1), g(1, "TOR", 2, "2026-10-10"), g(1, "TOR", 3, "2026-10-12", otl=1),
             g(2, "TOR", 3, "2026-10-12", started=0), g(3, "BOS", 1, "2026-10-08")]
    st = rating.opponent_strength(games, {"TOR": 0.6, "MTL": 0.45})
    assert abs(st["TOR"] - ((3 + 12) / 26 - 0.55) / 0.1) < 1e-9  # 2 W + 1 OTL in 3 games, shrunk 10 games toward .600
    assert abs(st["BOS"] - ((0 + 11) / 22 - 0.55) / 0.1) < 1e-9 and abs(st["MTL"] - (-1.0)) < 1e-9
    assert rating.nonwin_streaks(games) == {1: 2, 3: 1}


def test_unplayed_share_falls_as_his_team_plays_without_him():
    """Yet to play: the share depends on last season's games and his team's games so far (findings 350 (1), handoff 310)."""
    s = rating.unplayed_share
    assert s(0, 0) is None and s(19, 0) == 0.119 and s(5, 40) == 0.043 and s(20, 0) == 0.275 and s(59, 8) == 0.156 and s(82, 0) == 0.717 and s(82, 50) == 0.081
    for gp in (5, 25, 45, 70):
        row = [s(gp, n) for n in (0, 3, 4, 7, 8, 12, 13, 20, 21, 35, 36, 80)]
        assert row == sorted(row, reverse=True)
    assert [rating.UNPLAYED_NOTED_MULT[rating.unplayed_cell(n)] for n in (0, 7, 8, 13, 21, 36)] == [1.0, 1.0, 0.78, 0.56, 0.37, 0.23]


def test_new_and_part_time_skaters_who_stick_keep_their_spot():
    """Season dress logit: no games last season, +0.41 x min(GP, 40)/40 and -0.26 with 5 or fewer games (handoff 311);
    1-40 games last season, +0.38 x min(GP, 40)/40 (handoff 312); regulars unchanged (findings 350 (2)-(3))."""
    model = rating.Model()
    base = {k: 0.0 for k in model.season_lr["coef"]}
    z = lambda p: math.log(p / (1 - p))
    def shift(**x):
        return z(model.season_dress({**base, **x})) - z(model.season_dress(base))
    assert shift(no_prev=1.0, gp_now=3) == pytest.approx(0.41 * 3 / 40 - 0.26 + model.season_lr["coef"]["no_prev"])
    assert shift(no_prev=1.0, gp_now=60) == pytest.approx(0.41 + model.season_lr["coef"]["no_prev"])
    c = model.season_lr["coef"]["prev_dress_rate"]
    assert shift(prev_dress_rate=0.3, gp_now=20) == pytest.approx(0.3 * c + 0.38 * 0.5)
    assert shift(prev_dress_rate=0.9, gp_now=20) == pytest.approx(0.9 * c)


def test_proj_gp_adds_the_season_share_of_games_left(run):
    """Proj GP = NHL games so far + the Season rating's share of his team's games still to play."""
    by_name, _, _ = run
    p = by_name["Ryan Spooner"]
    assert p["cr_why"]["gp"] == 0 and 0 < p["proj_gp"] < rating.SEASON_GAMES
    assert p["proj_gp"] == round(p["cr_why"]["season_share"] * (rating.SEASON_GAMES - 4))


def test_daily_faceoff_news_moves_the_season_dress_logit():
    """Findings 360 (1), handoff 330: sent down while out, called up while in the lineup, season-ending and surgery items
    since his last game; older items and stale copies add nothing."""
    from datetime import datetime, timezone
    now = datetime(2026, 11, 20, 15, tzinfo=timezone.utc)
    news = {"injuries": {"1": {"ts": "2026-11-18T20:00:00", "season": True, "surgery": True},
                         "2": {"ts": "2026-11-10T20:00:00", "season": True, "surgery": False}},
            "roster-moves": {"3": {"ts": "2026-11-19T18:00:00", "down": True, "up": False},
                             "4": {"ts": "2026-11-01T18:00:00", "down": False, "up": True},
                             "5": {"ts": "2026-08-01T18:00:00", "down": False, "up": True}}}
    f = rating.news_season_logit
    assert f(news, 1, "2026-11-18", 1, 20, now) == (pytest.approx(-1.5 - 0.33), ["season-ending", "surgery"])
    assert f(news, 2, "2026-11-15", 2, 20, now) == (0.0, [])  # the item came before his last game
    assert f(news, 3, "2026-11-17", 3, 10, now) == (pytest.approx(-0.82 + 0.18 * math.log(4)), ["sent down"])
    assert f(news, 3, "2026-11-19", 0, 10, now) == (0.0, [])  # back in the lineup: no send-down term
    assert f(news, 4, "2026-11-19", 0, 20, now) == (pytest.approx(-0.18 + 0.18 * 0.5), ["called up"])
    assert f(news, 4, "2026-11-12", 2, 20, now) == (0.0, [])  # out now: the call-up term is for players in the lineup
    assert f(news, 5, "2026-11-19", 0, 20, now) == (0.0, [])  # called up more than 60 days ago
    assert f(None, 1, "2026-11-18", 1, 20, now) == (0.0, [])
    # findings 371: an earlier season-level or "indefinitely" item since his last game still counts under a milder latest one
    news["injuries"]["6"] = {"ts": "2026-11-19T20:00:00", "season": False, "surgery": False, "season_ts": "2026-11-16T20:00:00"}
    news["injuries"]["7"] = {"ts": "2026-11-19T20:00:00", "season": False, "surgery": False, "indefinite_ts": "2026-11-16T20:00:00"}
    assert f(news, 6, "2026-11-15", 3, 20, now) == (pytest.approx(-1.5), ["season-ending"])
    assert f(news, 6, "2026-11-17", 1, 20, now) == (0.0, [])  # he played after it
    assert f(news, 7, "2026-11-15", 3, 20, now) == (pytest.approx(-0.3), ["indefinite"])


def test_daily_faceoff_news_reaches_the_season_rating(monkeypatch, tmp_path):
    """A season-ending item after Rielly's last game lowers his Season rating and is named in the details; a stale copy
    of the news (the fetch has failed for days) changes nothing."""
    def ratings(news):
        fake = FakeSession()
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
        monkeypatch.setattr(rating, "load_news", lambda: {})
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(102, "Morgan Rielly", "TOR", "D", injury="OUT")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [], dfo_news=news)
        return {p["name"]: p for p in players}
    fresh = rating.utcnow().strftime("%Y-%m-%dT%H:%MZ")
    item = {"injuries": {"2": {"ts": "2026-10-08T20:00:00", "season": True, "surgery": False}}, "roster-moves": {}}
    base, hit = ratings(None), ratings({**item, "fetched": fresh})
    assert hit["Morgan Rielly"]["cr_season"] < base["Morgan Rielly"]["cr_season"]
    assert hit["Morgan Rielly"]["cr_why"]["season_news"] == ["season-ending"]
    assert hit["Auston Matthews"]["cr_season"] == base["Auston Matthews"]["cr_season"]
    stale = ratings({**item, "fetched": "2020-01-01T00:00Z"})
    assert stale["Morgan Rielly"]["cr_season"] == base["Morgan Rielly"]["cr_season"]


def test_espn_preseason_projection_pulls_the_season_fpg(monkeypatch, tmp_path):
    """Findings 360 (2), handoff 331: Season FP/G += (ESPN projected FP/G - FP/G) x (0.29 - 0.57 g + 0.35 g^2), g = min(GP, 40)/40;
    skaters without a projection are unchanged."""
    def ratings(proj):
        fake = FakeSession()
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
        monkeypatch.setattr(rating, "load_news", lambda: {})
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(104, "Charlie McAvoy", "BOS", "D")]
        players[0]["proj_ppg"] = proj
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        return {p["name"]: p for p in players}
    base, hi = ratings(None), ratings(9.0)
    m0, m1 = base["Auston Matthews"], hi["Auston Matthews"]
    g = min(m1["cr_why"]["gp"], 40) / 40
    w = 0.29 - 0.57 * g + 0.35 * g * g
    assert m1["cr_why"]["espn_blend"] == round(w, 2) and "espn_blend" not in m0["cr_why"]
    assert m1["cr_season"] > m0["cr_season"]
    assert hi["Charlie McAvoy"]["cr_season"] == base["Charlie McAvoy"]["cr_season"]


def test_boosted_model_walks_its_trees_like_sklearn():
    """season_boost.predict: left when x <= threshold (null = +inf), missing inputs to the node's missing side,
    baseline + leaf values (handoff 332)."""
    import season_boost
    model = {"features": ["a", "b"], "baseline": 1.0,
             "trees": [[[0, -1, 1, -1, -1], [2.0, 0.5, None, -0.25, 0.75], [1, 0, 3, 0, 0], [2, 0, 4, 0, 0], [0, 0, 0, 0, 0]],
                       [[0, -1, -1], [None, 0.0, 0.125], [1, 0, 0], [2, 0, 0], [0, 0, 0]]]}
    assert season_boost.predict(model, {"a": 2.0}) == 1.5  # a <= 2 -> leaf 0.5, then null threshold -> left 0.0
    assert season_boost.predict(model, {"a": 3.0, "b": 1e9}) == 0.75  # null threshold: always left (-0.25)
    assert season_boost.predict(model, {"a": 3.0, "b": None}) == 1.75  # missing b goes right at node 2 (m = 0)
    assert season_boost.predict(model, {}) == 1.875  # a missing: right in both trees (m = 0)
    for name, lo, hi in (("dress", 0.0, 1.2), ("fpg", 0.0, 6.0)):
        m = season_boost.load(name)
        assert m and len(m["trees"]) == 400
        assert lo < season_boost.predict(m, {}) < hi
    week = season_boost.load("week")  # sklearn's early stopping: fewer than 400 trees (handoff 411)
    assert week and 50 <= len(week["trees"]) <= 400 and 0.0 < season_boost.predict(week, {}) < 1.2


def test_weekly_boosted_dress_model_moves_the_week_toward_its_share(monkeypatch, tmp_path):
    """Handoff 411: each remaining game's dress logit moves 0.8 x (logit(b) - logit(his mean)); injury caps still hold."""
    import season_boost
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    real = season_boost.load

    def run(b, w=0.8, injury=None):
        monkeypatch.setattr(rating, "BOOST_WEEK_W", w)
        monkeypatch.setattr(rating.season_boost, "load", lambda n: {"features": [], "baseline": b, "trees": []} if n == "week" else real(n))
        monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
        players = [espn(101, "Auston Matthews", "TOR", "C", injury=injury), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY)
        return players[0]
    off, low, high = run(0.5, w=0.0), run(0.05), run(0.99)
    assert low["cr_dress"] < off["cr_dress"] < high["cr_dress"] <= 1.0
    assert low["cr_why"]["boost_week"] < 0 < high["cr_why"]["boost_week"]
    # day-to-day after a full game: capped at 0.75 (DTD_PLAYED_CAPS) however high the boosted share
    assert run(0.99, injury="DAY_TO_DAY")["cr_dress"] == pytest.approx(rating.DTD_PLAYED_CAPS[-1][1])


def test_week_news_lands_on_top_of_the_weekly_boosted_model(monkeypatch, tmp_path):
    """Handoff 434: the boost is measured against his chances before the news, so the news keeps its full logit shift;
    435: week_news records the news's real effect on his chance."""
    import season_boost
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    real = season_boost.load
    monkeypatch.setattr(rating.season_boost, "load", lambda n: {"features": [], "baseline": 0.5, "trees": []} if n == "week" else real(n))
    monkeypatch.setattr(rating, "HEALTHY_REG_LOGIT", 0.0)  # news players don't get it; keep both runs alike (handoff 355)

    def run(z):
        monkeypatch.setattr(rating, "week_news_logit", lambda news, pid, last, sat, st, dates, *a: ([z] * len(dates), 0.0, False, ["day-to-day"] if z else []))
        monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY)
        return players[0]
    off, on = run(0.0), run(-1.0)
    assert "week_news" not in off["cr_why"] and off["cr_why"]["boost_week"] == on["cr_why"]["boost_week"]
    assert rating.logit(off["cr_dress"]) - rating.logit(on["cr_dress"]) == pytest.approx(1.0, abs=0.05)
    assert on["cr_why"]["week_news"]["move"] == pytest.approx(on["cr_dress"] - off["cr_dress"], abs=0.002)


def test_boosted_inputs_follow_the_research_panel():
    """compact.py's inputs rebuilt from this season's games: games out, last 3/10, absences, lineup crunch."""
    import season_boost
    tg = {"TOR": ["g1", "g2", "g3", "g4", "g5", "g6"]}
    def game(gid, toi, fp=1.0, pos="C"):
        return {"game": gid, "date": f"2026-10-0{gid[1]}", "team": "TOR", "pos": pos, "toi": toi, "pptoi": 1.0, "fp": fp}
    by = {1: [game(g, 20) for g in ("g1", "g2", "g3", "g4", "g5", "g6")],  # every game
          2: [game(g, 22) for g in ("g1", "g2", "g3")],  # regular, out the last 3
          3: [game(g, 12) for g in ("g1", "g4", "g5", "g6")],  # missed 2 then back
          4: [game("g6", 15, pos="D")]}
    x = season_boost.features(by, tg, {1: {"gp": 80, "fp": 2.0}}, {}, {1: (date(2000, 1, 1), 5, 20172018)}, {}, 20262027,
                              date(2026, 10, 9), 84)
    assert x[1]["out"] == 0 and x[2]["out"] == 3 and x[3]["d3"] == 3 and x[2]["d3"] == 0 and x[2]["d10"] == 0.5
    assert (x[3]["n_abs"], x[3]["n_long"], x[3]["max_abs"]) == (1, 0, 2) and x[3]["dress_rate"] == 4 / 6
    assert x[1]["out_above"] == 1 and x[3]["out_above"] == 1 and x[1]["toi_rank"] == 1 and x[3]["toi_rank"] == 2
    assert x[2]["toi_rank"] is None and x[1]["n_reg"] == 3 and x[4]["isD"] == 1.0 and x[4]["n_reg"] == 0
    assert x[1]["prev_fpg"] == 2.0 and x[1]["nhl_yrs"] == 9 and x[1]["exp"] == 5 and x[1]["draft"] == 5 and x[2]["draft"] == 250


def test_boosted_models_reach_the_season_rating(run):
    """Players with games this season get the boosted dress share and FP/G in their details (handoff 332)."""
    by_name, _, _ = run
    m = by_name["Auston Matthews"]["cr_why"]
    assert 0.01 <= m["boost_dress"] <= 0.99 and m["boost_fpg"] >= 0
    assert "boost_dress" not in by_name["Ryan Spooner"]["cr_why"]  # yet to play: no panel row


def test_open_lineup_spots_lift_depth_players():
    """Findings 370, handoff 350: per team and position group, slack = spots - summed Season shares; logit(p) moves by
    1.34 x max(slack, 0)/3 x (1 - p) + 2.84 x min(slack, 0)/3 x (1 - p); a group with fewer players than spots is left alone."""
    model = rating.Model()
    def entry(pid, team, share, is_d=False):
        return {"p": {"cr_why": {}, "pos": "D" if is_d else "C"}, "pid": pid, "team": team, "isD": is_d, "share": share, "pts": 2.0,
                "gp": 5, "left": 70, "range": (is_d, 80, 80, 27.0)}
    thin = [entry(i, "TOR", 0.6) for i in range(12)]  # 7.2 of 12 forward spots filled: slack 4.8
    full = [entry(100 + i, "BOS", 0.8) for i in range(18)]  # 14.4 of 12: slack -2.4
    few = [entry(200 + i, "MTL", 0.5, True) for i in range(4)]  # 4 D listed: missing logs, unchanged
    rating.lineup_slack(thin + full + few, {}, None, model)
    z = math.log(0.6 / 0.4) + 1.34 * 4.8 / 3 * 0.4
    assert thin[0]["p"]["cr_season"] == round(2.0 / (1 + math.exp(-z)), 2) and thin[0]["p"]["cr_why"]["lineup_slack"] == 4.8
    z = math.log(0.8 / 0.2) + 2.84 * -2.4 / 3 * 0.2
    assert full[0]["p"]["cr_season"] == round(2.0 / (1 + math.exp(-z)), 2) < 1.6
    assert "cr_season" not in few[0]["p"] and thin[0]["p"]["proj_gp"] == round(5 + 1 / (1 + math.exp(-math.log(1.5) - 1.34 * 1.6 * 0.4)) * 70)


def test_team_form_streaks_and_margin():
    """Straight non-wins and wins, latest first, and the last game's margin from both teams' goalie rows (findings 281-283)."""
    row = lambda team, game, date, win, ga: {"team": team, "game": game, "date": date, "wins": win, "ga": ga, "started": 1}
    games = [row("TOR", 1, "2026-10-08", 1, 2), row("BOS", 1, "2026-10-08", 0, 3),
             row("TOR", 2, "2026-10-10", 0, 6), row("BOS", 2, "2026-10-10", 1, 1),
             row("TOR", 3, "2026-10-12", 0, 5), row("TOR", 3, "2026-10-12", 0, 1), row("BOS", 3, "2026-10-12", 1, 1)]
    form = rating.team_form(games)
    assert form["TOR"] == (2, 0, -5)  # two losses since the win; lost the last 1-6 (two TOR goalies)
    assert form["BOS"] == (0, 2, 5)


def test_form_shift_stacks_as_handoff_344_says():
    # slump and blowout don't stack: the larger drop; low minutes add their own; win run only for skaters out 1-4 games
    assert rating.form_shift("F", 9.0, 0, (4, 0, -1)) == (rating.SLUMP_DEPTH["F"], ["slump"])  # 8-10 min: no minutes shift
    assert rating.form_shift("F", 9.0, 0, (5, 0, -4)) == (rating.BLOWOUT_DEPTH, ["blowout"])
    assert rating.form_shift("F", 11.0, 0, None) == (-0.03, ["depth_f"])
    assert rating.form_shift("F", 7.0, 0, None) == (-0.02, ["depth_f"])
    shift, why = rating.form_shift("D", 13.0, 0, (4, 0, -1))
    assert why == ["slump", "depth_d"] and shift == pytest.approx(rating.SLUMP_DEPTH["D"] - 0.055)
    assert rating.form_shift("D", 11.0, 0, None) == (-0.025, ["depth_d"])
    assert rating.form_shift("D", 15.0, 0, None) == (-0.025, ["depth_d"])
    assert rating.form_shift("F", 15.0, 0, (6, 0, -6)) == (0.0, [])  # a regular: no depth shift
    assert rating.form_shift("D", 17.0, 0, (6, 0, -6)) == (0.0, [])
    assert rating.form_shift("F", None, 2, (0, 3, 2)) == (rating.WIN_RUN_OUT, ["win_run"])
    assert rating.form_shift("F", None, 5, (0, 3, 2)) == (0.0, [])  # out 5+: not a lineup choice
    assert rating.form_shift("F", None, 2, (0, 2, 2)) == (0.0, [])
    # the team's first 15 games: depth skaters (under 12 F / 16 D) a further -0.03 (handoff 349)
    shift, why = rating.form_shift("F", 9.0, 0, None, team_played=6)
    assert why == ["early"] and shift == pytest.approx(rating.EARLY_DEPTH)
    assert rating.form_shift("F", 12.5, 0, None, team_played=6) == (0.0, [])
    assert rating.form_shift("D", 15.0, 0, None, team_played=15)[1] == ["depth_d"]


def test_left_early_teammates():
    """Skaters with 5+ games who played the team's last game under 40% of their average minutes (handoff 352)."""
    g = lambda pid, game, toi, pos="D": {"id": pid, "game": game, "team": "TOR", "toi": toi, "pos": pos}
    by_player = {1: [g(1, k, 20.0) for k in range(5)] + [g(1, 5, 5.0)],  # left early in the last game
                 2: [g(2, k, 20.0) for k in range(3)] + [g(2, 5, 5.0)],  # too few games
                 3: [g(3, k, 15.0, "C") for k in range(6)]}  # a normal night, and not the last game
    by_player[4] = [g(4, k, 20.0, "C") for k in range(5)] + [g(4, 5, 9.0, "C")]  # 45% of his minutes: second band (353)
    out = rating.left_early_teammates(by_player, {"TOR": list(range(6))})
    assert out == {("TOR", True): {1: 0}, ("TOR", False): {4: 1}}


def test_week_news_shifts_from_daily_faceoff_items_since_his_last_game():
    """Handoffs 406-408: out-now terms, sent down, and hurt after a game he dressed for with no ESPN status yet."""
    item = lambda ts, cat, *f, tl=None: {"ts": ts, "cat": cat, "f": sorted(f), **({"tl": tl} if tl is not None else {})}
    news = {"recent": {"1": [item("2026-10-08T20:00:00", "inj", "out"),  # before his last game: ignored
                             item("2026-10-11T15:00:00", "inj", "skate"), item("2026-10-12T15:00:00", "inj", "full", "ret")],
                       "2": [item("2026-10-11T15:00:00", "inj", "wtw", tl=21.0)],
                       "3": [item("2026-10-11T15:00:00", "move", "down")],
                       "4": [item("2026-10-11T15:00:00", "inj", "out")]}}
    dates = ["2026-10-13", "2026-10-15", "2026-10-25"]
    w = rating.WEEK_NEWS
    z, _, practising, why = rating.week_news_logit(news, 1, "2026-10-10", True, "OUT", dates)
    assert practising and z == [pytest.approx(w["practice"] + w["ret"])] * 3 and why == ["practising", "set to return"]
    z, _, practising, _ = rating.week_news_logit(news, 2, "2026-10-10", True, "OUT", dates)
    # 21 days from Oct 11: 19 and 17 days left at the first two games (more than 7), 7 at Oct 25 (neither), run out by Nov 5
    assert not practising and z == [pytest.approx(w["wtw"] + w["timeline"])] * 2 + [pytest.approx(w["wtw"])]
    assert rating.week_news_logit(news, 2, "2026-10-10", True, "OUT", dates)[3] == ["week-to-week", "timeline"]  # handoff 425
    z, _, _, why = rating.week_news_logit(news, 2, "2026-10-10", True, "OUT", ["2026-11-05"])  # handoff 428
    assert z == [pytest.approx(w["wtw"] + w["timeline_up"])] and why == ["week-to-week", "timeline up"]
    assert rating.week_news_logit(news, 3, "2026-10-10", True, None, dates)[:2] == ([0.0] * 3, w["down_out"])
    assert rating.week_news_logit(news, 3, "2026-10-10", False, None, dates)[:2] == ([0.0] * 3, w["down_in"])
    assert rating.week_news_logit(news, 4, "2026-10-10", False, None, dates)[0] == [w["hurt_out"]] * 3
    assert rating.week_news_logit(news, 4, "2026-10-10", False, "DAY_TO_DAY", dates)[0] == [0.0] * 3  # ESPN's cap covers it
    assert rating.week_news_logit(news, 4, "2026-10-12", False, None, dates)[0] == [0.0] * 3  # news older than his last game
    assert rating.week_news_logit(None, 4, "2026-10-10", False, None, dates)[0] == [0.0] * 3
    # handoff 438: a game-day item counts only after puck drop (23:00 UTC when the schedule has no start time)
    morning = {"recent": {"5": [item("2026-10-10T17:00:00", "inj", "out")]}}
    assert rating.week_news_logit(morning, 5, "2026-10-10", False, None, dates)[0] == [0.0] * 3
    assert rating.week_news_logit(morning, 5, "2026-10-10", False, None, dates, "2026-10-10T16:00:00Z")[0] == [w["hurt_out"]] * 3


def test_week_model_news_inputs_from_the_latest_injury_item():
    """Handoff 413: flags count only for an item posted after his last game; age in days and the timeline left."""
    import season_boost
    from datetime import datetime, timezone
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    item = {"ts": "2026-10-12T12:00:00", "nf": ["out", "wtw"], "tl": 14.0}
    x = season_boost.news_inputs(item, "2026-10-11", now)
    assert x["inj_after"] == 1 and x["inj_out"] == 1 and x["inj_wtw"] == 1 and x["inj_dtd"] == 0
    assert x["inj_age"] == pytest.approx(2.0) and x["inj_rem"] == pytest.approx(12.0)
    old = season_boost.news_inputs(item, "2026-10-13", now)  # he has played since
    assert old["inj_after"] == 0 and old["inj_out"] == 0 and old["inj_age"] is None and old["inj_rem"] is None
    assert season_boost.news_inputs(None, "2026-10-11", now)["inj_after"] == 0
    assert set(season_boost.load("week")["features"]) >= set(x)


def test_season_drift_moves_a_group_whose_scoring_runs_ahead_of_its_season_rating():
    """Handoff 405: forwards outscoring their Season FP/G more than everyone else get half the gap, defensemen the rest."""
    class M:
        def season_range(self, season, *a):
            return [round(season * 0.8, 2), round(season * 1.2, 2)], "veteran"
    p = lambda: {"cr_why": {}}
    rows = [{"p": p(), "D": False, "pts": 2.0, "share": 0.9, "fp": [3.0] * 12, "range": (False, 80, 80, 25), "lineup": None},
            {"p": p(), "D": True, "pts": 1.0, "share": 0.9, "fp": [1.0] * 12, "range": (True, 80, 80, 25), "lineup": {"pts": 1.0}},
            {"p": p(), "D": False, "pts": 1.5, "share": 0.5, "fp": [1.0] * 4, "range": (False, 0, 0, 20), "lineup": None}]  # under 10 GP
    rating.season_drift(rows, M())
    # e_std = e_l10: F +1.0 (12 GP), D 0.0 (12 GP), all +0.5; F drift +0.5, D -0.5; half of each
    assert rows[0]["p"]["cr_why"]["season_pts"] == 2.25 and rows[0]["p"]["cr_season"] == round(0.9 * 2.25, 2)
    assert rows[1]["p"]["cr_why"]["drift"] == -0.25 and rows[1]["lineup"]["pts"] == 0.75
    assert rows[2]["p"]["cr_why"]["season_pts"] == 1.75  # every forward moves, whatever his games
    few = [dict(r, fp=r["fp"][:5], p=p()) for r in rows]
    rating.season_drift(few, M())
    assert all("drift" not in r["p"]["cr_why"] for r in few)  # nobody with 10 games yet


def test_sent_down_skater_with_no_games_left_still_rates(monkeypatch, tmp_path):
    """A send-down with no games left this week moves nothing and must not break the run (handoffs 407, 412)."""
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    news = {"fetched": "2099-01-01T00:00Z", "recent": {str(i): [{"ts": "2099-01-01T00:00:00", "cat": "move", "f": ["down"]}] for i in range(1, 10)}}
    for gl in (0, 3):
        players = [espn(101, "Auston Matthews", "TOR", "C", games_left=gl), espn(103, "David Pastrnak", "BOS", "RW", games_left=gl)]
        rating.add_ratings(players, 2027, TODAY, MONDAY, dfo_news=news)
        assert all(p.get("cr") is not None for p in players)


def test_predicted_winners_cover_the_week(monkeypatch, tmp_path):
    """The Predicted winners tab (findings 450): one row per game this week, home side first, a win chance for each
    game, and the stronger team (TOR outscored BOS last season and this) favoured at home."""
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    players = [espn(101, "Auston Matthews", "TOR", "C"), espn(104, "Charlie McAvoy", "BOS", "D"),
               espn(111, "Joseph Woll", "TOR", "G"), espn(113, "Jeremy Swayman", "BOS", "G")]
    winners = {}
    rating.add_ratings(players, 2027, TODAY, MONDAY, [], winners=winners)
    games = winners["games"]
    assert [g["date"] for g in games] == WEEK
    assert all(g["home"] == "TOR" and g["away"] == "BOS" for g in games)
    assert all(0.5 < g["p_home"] < 0.95 for g in games), [g["p_home"] for g in games]
    assert all(g["inputs"]["pts"][0] > 0 and g["inputs"]["pts"][1] > 0 for g in games)
    # team xG (handoff 449): TOR (14 - 10 + 15 x 0.5 x 0.5) / (4 + 15), BOS even
    assert all(g["inputs"]["xgd"] == [round(7.75 / 19, 2), 0.0] for g in games)


def test_predicted_winners_without_team_xg(monkeypatch, tmp_path):
    """MoneyPuck's team file missing: the picks fall back to the goal-differential coefficients (handoff 449)."""
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    out = {}
    for miss in (False, True):
        monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession(no_team_xg=miss))
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(104, "Charlie McAvoy", "BOS", "D"),
                   espn(111, "Joseph Woll", "TOR", "G"), espn(113, "Jeremy Swayman", "BOS", "G")]
        winners = {}
        rating.add_ratings(players, 2027, TODAY, MONDAY, [], winners=winners)
        out[miss] = winners["games"]
    assert all("xgd" not in g["inputs"] and 0.5 < g["p_home"] < 0.95 for g in out[True])
    assert [g["p_home"] for g in out[True]] != [g["p_home"] for g in out[False]]


def test_week_inputs_for_a_skater_with_no_game_lean_on_two_seasons():
    """Handoff 429: with no game yet, the Week's windows are the GP-weighted mean of last season and the one before."""
    prev = {r: 1.0 for r in rating.GAME_RATES} | {"gp": 60, "toi": 18.0, "pptoi": 2.0}
    p2 = {r: 2.0 for r in rating.TWO_SEASON_RATES} | {"gp": 20, "toi": 14.0, "pptoi": 1.0}
    one = rating.skater_features([], prev, None, None, ["g1"], p2=p2)
    two = rating.skater_features([], prev, None, None, ["g1"], p2=p2, two_season=True)
    assert one["l5_fp"] == 1.0 and one["last_toi"] == 18.0
    assert two["l5_fp"] == two["std_fp"] == pytest.approx(1.25) and two["last_toi"] == two["toi_l3"] == pytest.approx(17.0)
    assert two["pp_l1"] == pytest.approx(1.75) and two["prev_fp"] == 1.0  # last season's own inputs stay
    assert rating.skater_features([], prev, None, None, ["g1"], p2=None, two_season=True)["l5_fp"] == 1.0
    assert [w for _, w in rating.EARLY_WEEK_ESPN] == [0.25, 0.19]


def test_early_no_start_goalie_cut(monkeypatch):
    """Early season, a roster goalie with no start for the team yet gets a lower start chance (handoff 450)."""
    model = rating.Model()
    dates = ["2026-10-09", "2026-10-11", "2026-10-13", "2026-10-15"]
    hist = {"TOR": [("2026-10-05", 1), ("2026-10-07", 1)]}
    on = rating.goalie_starts(model, "TOR", [1, 2], hist, {1: 50, 2: 32}, dates, TODAY)
    monkeypatch.setattr(rating, "EARLY_NO_START_LOGIT", 0.0)
    off = rating.goalie_starts(model, "TOR", [1, 2], hist, {1: 50, 2: 32}, dates, TODAY)
    assert on[2] < off[2] and on[2] > 0


def test_even_tandem_halves_b2b_shift(monkeypatch):
    """When the last 20 starts are split evenly, the back-to-back shifts are half size (handoff 451)."""
    model = rating.Model()
    pair = ["2026-10-23", "2026-10-24"]
    def gap(hist):
        def one(on):
            if not on:
                monkeypatch.setattr(rating, "B2B_AHEAD_NIGHT1", 0.0)
                monkeypatch.setattr(rating, "B2B_AHEAD_NIGHT2_NEXT", 0.0)
            out = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, pair, date(2026, 10, 22))
            monkeypatch.undo()
            return out[1]
        return abs(one(True) - one(False))
    even = [(f"2026-10-{d:02d}", 1 if i % 2 else 2) for i, d in enumerate(range(1, 21))]
    lopsided = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 21))]
    assert 0 < gap(even) < gap(lopsided)


def test_traded_goalie_keeps_his_confirmed_start(monkeypatch, tmp_path):
    """Handoff 454: a goalie on his new team's roster with no game for it yet still matches its confirmed starter."""
    from datetime import datetime, timezone
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 9, 16, tzinfo=timezone.utc))
    real = rating.roster
    def moved(s, team, goalie_games=()):  # Swayman claimed by TOR, all his games so far for BOS
        r = [x for x in real(s, team, goalie_games) if x[0] != 13]
        return r + [(13, "Jeremy Swayman", "G")] if team == "TOR" else r
    monkeypatch.setattr(rating, "roster", moved)
    starters = [{"date": "2026-10-09", "games": [{"team": "TOR", "goalie": "Jeremy Swayman", "status": "Confirmed"}]}]
    players = [espn(111, "Joseph Woll", "TOR", "G"), espn(112, "Anthony Stolarz", "TOR", "G"), espn(113, "Jeremy Swayman", "TOR", "G")]
    rating.add_ratings(players, 2027, TODAY, MONDAY, starters)
    woll, stolarz, sway = players
    assert sway["cr_why"]["tonight"]["starting"] is True and sway["cr_why"]["p_today"] == 1.0
    assert woll["cr_why"]["p_today"] == 0 and stolarz["cr_why"]["p_today"] == 0


def test_prev_goalie_points_leave_out_relief_appearances():
    """Handoff 486: ESPN scores relief appearances 0 here, so last season's points per start count started games only."""
    prev = rating.load_prev_goalie_season(FakeSession(), 20252026)
    woll, stolarz = prev[11], prev[12]
    relief = 2 * 1 - 1 + 0.2 * 12  # the relief win: W 2, GA -1, 12 saves
    assert stolarz[1] - woll[1] == pytest.approx(relief)
    assert woll[0] == stolarz[0] == 40 and woll[2] == stolarz[2]  # starts and save% inputs unchanged


def test_week_slack_fits_each_game_to_the_lineup_spots():
    """Per team game, the Week's per-game chances move toward 12 forwards dressed (handoff 489): an overfull game pulls
    every chance down, a game with room lifts them, a 0 stays 0, caps hold, and the Week rating follows its games."""
    def row(pid, chances, cap=1.0):
        p = {"cr_fpg": 2.0, "cr_why": {}}
        return {"p": p, "pid": pid, "team": "TOR", "isD": False, "dates": ["2026-10-09", "2026-10-11"],
                "per_game": list(chances), "caps": [cap, cap]}
    # 15 forwards: 13.3 chances summed on Oct 9 (1.3 too many), 9.1 on Oct 11 (2.9 open)
    rows = [row(k, (0.95, 0.7)) for k in range(12)] + [row(12, (0.0, 0.7)), row(13, (0.95, 0.0), cap=0.95), row(14, (0.95, 0.0))]
    rating.week_slack(rows, {}, None)
    first = rows[0]["p"]
    assert first["cr_dress_next"] < 0.93 and first["cr_why"]["week_slack"] == -1.3  # overfull: down
    assert rows[0]["per_game"] == [0.95, 0.7]  # the rows keep the chances from before the step
    assert first["cr"] == round(2.0 * first["cr_games"], 2)
    assert rows[12]["p"]["cr_dress_next"] == 0.0 and rows[12]["p"]["cr_games"] > 0.75  # a 0 stays 0; room on Oct 11: up
    assert rows[13]["p"]["cr_dress_next"] <= 0.95  # under his cap
    few = [row(k, (0.5, 0.5)) for k in range(5)]  # fewer listed than spots: left alone
    rating.week_slack(few, {}, None)
    assert "cr_games" not in few[0]["p"]


def test_a_fresh_call_up_with_no_game_yet_plays_one_game_in_four(monkeypatch, tmp_path):
    """Yet to play this season, no ESPN status, and Daily Faceoff's latest move a call-up this week: 0.25 a game, not the
    0.10 for a player who isn't playing (handoff 502). A call-up two weeks old, or with an ESPN status, keeps the old cap."""
    def rate(ts, injury=None):
        fake = FakeSession(missed={1: {0, 1, 2, 3}})
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
        monkeypatch.setattr(rating, "load_news", lambda: {})
        p = espn(101, "Auston Matthews", "TOR", "C", games_left=2, injury=injury)
        news = {"injuries": {}, "roster-moves": {"1": {"ts": ts, "up": True}}, "fetched": rating.utcnow().strftime("%Y-%m-%dT%H:%MZ")}
        rating.add_ratings([p], 2027, TODAY, MONDAY, [], dfo_news=news)
        return p
    fresh = (rating.utcnow() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    up = rate(fresh)
    assert up["cr_dress"] == pytest.approx(rating.RECALL_NEW_DRESS, abs=0.03) and up["cr_why"]["called_up"]
    old = rate((rating.utcnow() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M"))
    assert old["cr_dress"] <= 0.11 and "called_up" not in old["cr_why"]
    hurt = rate(fresh, injury="OUT")
    assert "called_up" not in hurt["cr_why"] and hurt["cr_dress"] <= 0.11


def test_home_after_a_road_trip_and_long_absences_move_week_chances():
    """The first home game after 2+ road games brings back skaters who missed games and sits regulars a little; a
    skater out 3+ games comes back a bit faster later in the week (handoff 500)."""
    venue = {"2026-10-03": False, "2026-10-05": False, "2026-10-07": True, "2026-10-09": False, "2026-10-11": True}
    assert rating.after_trip(venue, ["2026-10-07", "2026-10-09", "2026-10-11"]) == [True, False, False]
    def row(pid, chances, missed, trip):
        return {"p": {"cr_fpg": 2.0, "cr_why": {}}, "pid": pid, "team": "TOR", "isD": False, "dates": ["2026-10-07", "2026-10-09"],
                "per_game": list(chances), "before": list(chances), "caps": [1.0, 1.0], "missed": missed, "after_trip": trip}
    rows = [row(1, (0.9, 0.9), 0, [True, False]), row(2, (0.3, 0.3), 1, [True, False]), row(3, (0.3, 0.3), 4, [False, False])]
    rating.week_slack(rows, {}, None)  # 3 forwards: too few for the slack, so only these shifts move them
    regular, back, long_out = (r["p"] for r in rows)
    assert regular["cr_dress_next"] < 0.9 and back["cr_dress_next"] > 0.3
    assert long_out["cr_dress_next"] == 0.3 and long_out["cr_games"] > 0.6
    assert regular["cr"] == round(2.0 * regular["cr_games"], 2)


def test_early_opponents_who_leaked_goals_last_season_lift_the_week(monkeypatch, tmp_path):
    """Opponents with last season's goals against above the league lift a skater's Week FP/G by at most OPP_LAST_C x their
    excess, applied once on top of the ESPN early blend (handoff 504); unreadable standings change nothing."""
    def rate(opp):
        fake = FakeSession()
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
        monkeypatch.setattr(rating, "load_news", lambda: {})
        monkeypatch.setattr(rating, "opp_last_ga", lambda s, season: opp)
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        return {p["name"]: p["cr_fpg"] for p in players}
    base, leaky = rate({}), rate({"TOR": 0.2, "BOS": 0.2, "MTL": 0.2})
    for name in base:
        assert base[name] < leaky[name] <= round(base[name] * (1 + rating.OPP_LAST_C * 0.2), 2) + 0.01, name
    assert rate({"TOR": 0.0, "BOS": 0.0, "MTL": 0.0}) == base
    fake = FakeSession()
    table = rating.opp_last_ga(fake, 20252026)
    assert table and abs(sum(table.values())) < 0.5  # a fraction above or below the league, around 0


def test_standings_readers_survive_null_fields():
    """A null goalAgainst / gamesPlayed / wins in NHL standings leaves each standings reader empty instead of raising into
    add_ratings, which would publish the site with no ratings (handoff 508)."""
    class Resp:
        def __init__(self, body):
            self.body = body
        def raise_for_status(self):
            pass
        def json(self):
            return self.body

    class NullStandings:
        def get(self, url, **kw):
            if url.endswith("standings-season"):
                return Resp({"seasons": [{"id": 20252026, "standingsEnd": "2026-04-16"}]})
            return Resp({"standings": [{"teamAbbrev": {"default": "TOR"}, "goalAgainst": None, "goalFor": None,
                                        "gamesPlayed": None, "wins": None, "otLosses": None}]})
    for reader in (rating.opp_last_ga, rating.prev_points_pct, rating.team_strength):
        assert reader(NullStandings(), 20252026) == {}, reader.__name__


def test_early_ice_time_change_is_pulled_back(monkeypatch, tmp_path):
    """In a skater's first 10 games, the Season FP/G gives back part of a change in his ice time vs last season (handoff
    507): the details carry the shift, and the Season rating moves the opposite way to the ice-time change."""
    def rate(c):
        monkeypatch.setattr(rating, "SEASON_TOI_CHG_C", c)
        class LessIceLastSeason(FakeSession):  # last season's ice time 3 minutes under this season's
            def get(self, url, params=None, **kw):
                r = super().get(url, params, **kw)
                exp = (params or {}).get("cayenneExp", "")
                if "skater/" in url and "seasonId=20252026" in exp and "gameDate" not in exp:
                    for row in r.payload["data"]:
                        row["timeOnIcePerGame"] -= 180
                return r
        fake = LessIceLastSeason()
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
        monkeypatch.setattr(rating, "load_news", lambda: {})
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        return {p["name"]: p for p in players}
    c = rating.SEASON_TOI_CHG_C
    off, on = rate(0.0), rate(c)
    moved = [n for n, p in on.items() if p["cr_why"].get("toi_chg")]
    assert moved
    for n in moved:
        tc = on[n]["cr_why"]["toi_chg"]
        assert -0.5 < tc < 0 and 1 <= on[n]["cr_why"]["gp"] <= rating.SEASON_TOI_CHG_GAMES
        assert (on[n]["cr_season"] - off[n]["cr_season"]) * tc >= 0, n


def test_healthy_regulars_dress_a_little_more(monkeypatch, tmp_path):
    """A skater with no injury listing who dressed his team's last game at regular minutes gets HEALTHY_REG_LOGIT on his
    Week chances after the slack fit; a skater who sat the last game is untouched (handoff 355)."""
    def rate(z):
        monkeypatch.setattr(rating, "HEALTHY_REG_LOGIT", z)
        fake = FakeSession()
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
        monkeypatch.setattr(rating, "load_news", lambda: {})
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(104, "Charlie McAvoy", "BOS", "D")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        return {p["name"]: p for p in players}
    z = rating.HEALTHY_REG_LOGIT
    off, on = rate(0.0), rate(z)
    assert on["Auston Matthews"]["cr_games"] >= off["Auston Matthews"]["cr_games"]
    assert on["Auston Matthews"]["cr_dress"] > off["Auston Matthews"]["cr_dress"] or off["Auston Matthews"]["cr_dress"] >= 0.99
    assert on["Charlie McAvoy"]["cr_games"] == off["Charlie McAvoy"]["cr_games"]


def test_last_seasons_regulars_yet_to_play_cut_their_teams_season_share(monkeypatch, tmp_path):
    """Last season's 40+ GP regulars on a team's roster with no game yet this season (Spooner, Marchand at BOS) lower his
    teammates' Season share once the team has 3+ games; other teams are untouched (handoff 509)."""
    def rate(c):
        monkeypatch.setattr(rating, "MISSING_REG_C", c)
        fake = FakeSession()
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
        monkeypatch.setattr(rating, "load_news", lambda: {})
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        return {p["name"]: p for p in players}
    c = rating.MISSING_REG_C
    off, on = rate(0.0), rate(c)
    assert on["David Pastrnak"]["cr_why"]["missing_regs"] < 0
    assert on["David Pastrnak"]["cr_season"] <= off["David Pastrnak"]["cr_season"]
    assert "missing_regs" not in on["Auston Matthews"]["cr_why"]
    assert on["Auston Matthews"]["cr_season"] == off["Auston Matthews"]["cr_season"]


def test_week_counts_regulars_due_back(monkeypatch, tmp_path):
    """Last season's regulars with no game yet and a recent Daily Faceoff item count in their current team's Week lineup
    sums, fading with team games; without recent news, on no roster, or before the team's 3rd game they don't (handoff 510)."""
    from datetime import datetime, timezone
    now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    prev = {9: {"gp": 80, "toi": 15.0, "pos": "C"}, 4: {"gp": 80, "toi": 20.0, "pos": "D"}, 8: {"gp": 80, "toi": 15.0, "pos": "L"},
            7: {"gp": 5, "toi": 15.0, "pos": "R"}}
    news = {"injuries": {"9": {"ts": "2026-10-10T12:00"}, "4": {"ts": "2026-10-10T12:00"}, "7": {"ts": "2026-10-10T12:00"}},
            "roster-moves": {"8": {"ts": "2026-08-01T12:00"}}}
    tg = {"BOS": list(range(5)), "TOR": [1, 2]}
    out = rating.week_missing_regulars(prev, {4: [{"game": 1}]}, tg, news, now, {9: "BOS", 4: "BOS", 8: "BOS", 7: "BOS"})
    assert out == {("BOS", False): pytest.approx(1.0 * rating.WEEK_MISSING_A * 20 / 25)}  # McAvoy has played, Marchand's news is old
    assert rating.week_missing_regulars(prev, {}, tg, news, now, {9: "TOR"}) == {}  # TOR has 2 games
    assert rating.week_missing_regulars(prev, {}, tg, news, now, {}) == {}  # on no roster


def test_ice_time_change_skips_a_game_he_left_hurt(monkeypatch, tmp_path):
    """A short last game he left early isn't lost ice time: no early ice-time term (handoff 512)."""
    fake = FakeSession(short_last={1: 240})
    monkeypatch.setattr(rating.requests, "Session", lambda: fake)
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    players = [espn(101, "Auston Matthews", "TOR", "C"), espn(103, "David Pastrnak", "BOS", "RW")]
    rating.add_ratings(players, 2027, TODAY, MONDAY, [])
    assert "toi_chg" not in players[0]["cr_why"]


def test_depth_skaters_who_fought_keep_their_spot(monkeypatch, tmp_path):
    """A healthy depth skater with a fighting major (5+ PIM) in his last game gets FIGHT_LOGIT on his Week chances; a
    regular-minutes skater with the same penalty doesn't (handoff 356)."""
    class Fight(FakeSession):  # Bolduc on depth minutes, a fight in the last game; Pastrnak too, on his usual minutes
        def get(self, url, params=None, **kw):
            r = super().get(url, params, **kw)
            exp = (params or {}).get("cayenneExp", "")
            if "skater/summary" in url and "gameDate" in exp:
                for row in r.payload["data"]:
                    if row["playerId"] == 7:
                        row["timeOnIcePerGame"] = 600
                    if row["playerId"] in (3, 7) and row["gameDate"] == PAST[-1]:
                        row["penaltyMinutes"] = 5
            return r

    def rate(z):
        monkeypatch.setattr(rating, "FIGHT_LOGIT", z)
        fake = Fight()
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
        monkeypatch.setattr(rating, "load_news", lambda: {})
        players = [espn(107, "Zack Bolduc", "BOS", "RW"), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        return {p["name"]: p for p in players}
    z = rating.FIGHT_LOGIT
    off, on = rate(0.0), rate(z)
    assert on["Zack Bolduc"]["cr_why"].get("fought") and on["Zack Bolduc"]["cr_games"] > off["Zack Bolduc"]["cr_games"]
    assert "fought" not in on["David Pastrnak"]["cr_why"] and on["David Pastrnak"]["cr_games"] == off["David Pastrnak"]["cr_games"]


def test_scratch_lifts_keep_their_size_after_the_weekly_model(monkeypatch, tmp_path):
    """A healthy scratch whose same-position teammate left the last game under 40% of his minutes gets that lift after the
    boosted Week model, so it keeps its size instead of the ~10% left when the boost was measured after it (handoff 515)."""
    def rate(left):
        fake = FakeSession()
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
        monkeypatch.setattr(rating, "load_news", lambda: {})
        monkeypatch.setattr(rating, "left_early_teammates", lambda *a: {("BOS", True): {999: 0}} if left else {})
        players = [espn(104, "Charlie McAvoy", "BOS", "D"), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        return players[0]
    off, on = rate(False), rate(True)
    assert on["cr_why"].get("fills_in")
    lift = rating.logit(on["cr_dress_next"]) - rating.logit(off["cr_dress_next"])
    assert lift > 0.5 * rating.TEAMMATE_LEFT_BANDS[0][1]["D"], lift


def test_last_result_scratch_shift_is_by_game(monkeypatch, tmp_path):
    """The healthy scratch's last-result shift is per game from his team's next one (handoff 357): a shift on game 1 only
    moves his next game, not the rest of the week."""
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "BOOST_WEEK_W", 0.0)
    monkeypatch.setattr(rating, "load_news", lambda: {})

    def mcavoy(by_game):
        monkeypatch.setattr(rating, "SCRATCH_LAST_RESULT_BY_GAME", {"win": by_game, "other": by_game})
        fake = FakeSession()
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        players = [espn(104, "Charlie McAvoy", "BOS", "D"), espn(101, "Auston Matthews", "TOR", "C")]
        rating.add_ratings(players, 2027, TODAY, MONDAY)
        return players[0]
    flat, first = mcavoy((0.0,)), mcavoy((1.0, 0.0))
    assert flat["sat_last"] and flat["games_left"] >= 2
    assert first["cr_dress_next"] > flat["cr_dress_next"] + 0.05
    assert first["cr_games"] - flat["cr_games"] == pytest.approx(first["cr_dress_next"] - flat["cr_dress_next"], abs=0.02)


def test_depth_forwards_special_teams_role(monkeypatch, tmp_path):
    """A healthy depth forward who killed penalties for 1:00+ in his last game dresses a little more, one with no PP or PK
    time a little less; a regular-minutes forward is untouched (handoff 358)."""
    def rate(sh, pp, on=True):
        class Role(FakeSession):  # Bolduc on depth minutes; his last game's special-teams time
            def get(self, url, params=None, **kw):
                r = super().get(url, params, **kw)
                exp = (params or {}).get("cayenneExp", "")
                if "skater/summary" in url and "gameDate" in exp:
                    for row in r.payload["data"]:
                        if row["playerId"] == 7:
                            row["timeOnIcePerGame"] = 600
                if "skater/timeonice" in url and "gameDate" in exp:
                    for row in r.payload["data"]:
                        if row["playerId"] in (3, 7) and row["gameDate"] == PAST[-1]:
                            row["shTimeOnIce"], row["ppTimeOnIce"] = sh, pp
                return r
        monkeypatch.setattr(rating, "ST_ROLE_LOGIT", {"pk": 0.2, "none": -0.1} if on else {"pk": 0.0, "none": 0.0})
        fake = Role()
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
        monkeypatch.setattr(rating, "load_news", lambda: {})
        players = [espn(107, "Zack Bolduc", "BOS", "RW"), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY, [])
        return {p["name"]: p for p in players}
    pk, pk_off = rate(90, 0), rate(90, 0, on=False)
    none, none_off = rate(0, 0), rate(0, 0, on=False)
    assert pk["Zack Bolduc"]["cr_why"].get("st_role") == "pk" and pk["Zack Bolduc"]["cr_games"] > pk_off["Zack Bolduc"]["cr_games"]
    assert none["Zack Bolduc"]["cr_why"].get("st_role") == "none" and none["Zack Bolduc"]["cr_games"] < none_off["Zack Bolduc"]["cr_games"]
    assert "st_role" not in pk["David Pastrnak"]["cr_why"] and "st_role" not in none["David Pastrnak"]["cr_why"]
