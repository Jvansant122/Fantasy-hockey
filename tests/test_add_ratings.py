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
            "saves": 28 * started, "shutouts": 0}


class FakeSession:
    def __init__(self, scratched=(4,), rail_ok=True, moved=None):
        self.calls = []
        self.moved = moved or {}  # player id: his first n games this season were for the other team (a trade)
        self.scratched, self.rail_ok = set(scratched), rail_ok  # healthy scratches on each game page (McAvoy sat the last one)

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
        m = re.search(r"/gamecenter/(\d+)/right-rail$", url)
        if m:
            if not self.rail_ok:
                return Resp(status=503)
            team = TEAMS[int(m.group(1)) % 10]
            i = (int(m.group(1)) - 2026020000) // 10
            def side(t):
                return {"scratches": [{"id": pid} for pid, _, tm, _, missed in SKATERS if tm == t and i in missed and pid in self.scratched]}
            return Resp({"gameInfo": {"awayTeam": side("BOS"), "homeTeam": side(team)}})
        if url.endswith("/standings-season"):
            return Resp({"seasons": [{"id": 20252026, "standingsEnd": "2026-04-17"}]})
        if "/standings/" in url:  # last season's final table, then this season's
            gp = 82 if url.endswith("2026-04-17") else 5
            return Resp({"standings": [{"teamAbbrev": {"default": "TOR"}, "goalFor": 3 * gp, "goalAgainst": 2 * gp, "gamesPlayed": gp},
                                       {"teamAbbrev": {"default": "BOS"}, "goalFor": 2 * gp, "goalAgainst": 3 * gp, "gamesPlayed": gp}]})
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
                return Resp({"data": [{"playerId": pid, "gamesStarted": 40, "wins": 22, "losses": 12, "otLosses": 6, "goalsAgainst": 110,
                                  "saves": 1000, "shutouts": 3} for pid, *_ in GOALIES]})
            return Resp({"data": [{**skater_row(pid, name, team, pos, 0), "gamesPlayed": 80, "goals": 20, "assists": 30,
                                   "ppPoints": 15, "shots": 200, "hits": 60, "blockedShots": 50, "ppTimeOnIce": 12000}
                                  for pid, name, team, pos, _ in SKATERS]})
        lo, hi = dates
        days = [i for i, d in enumerate(PAST) if lo <= d <= hi] if season == 20262027 else []
        if report.startswith("goalie"):
            return Resp({"data": [goalie_row(pid, name, team, i) for pid, name, team in GOALIES for i in days]})
        other = lambda t: TEAMS[1 - TEAMS.index(t)]
        return Resp({"data": [skater_row(pid, name, other(team) if i < self.moved.get(pid, 0) else team, pos, i)
                              for pid, name, team, pos, missed in SKATERS for i in days if i not in missed]})


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
        json.dumps(p)  # players.json must stay serialisable


def test_matching_and_injuries(run):
    players, _, log = run
    assert players["Auston Matthews"]["nhl_id"] == 1 and players["Auston Matthews"]["cr"] > 0
    assert players["David Pastrnak"]["cr"] == 0 and players["David Pastrnak"]["cr_dress"] == 0
    assert players["David Pastrnak"]["cr_why"]["back"] == {"games": "16"}
    assert players["Morgan Rielly"]["cr_dress"] <= 0.5
    ghost = players["Not A Real Player"]
    assert ghost["cr_matched"] is False and ghost["cr_dress"] == rating.NOT_PLAYING_DRESS
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


def test_injured_starter_hands_starts_to_backup():
    """An injured no. 1's games go to his partner (findings section 48)."""
    model = rating.Model()
    hist = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 21, 2))]
    dates = ["2026-10-22", "2026-10-24", "2026-10-26", "2026-10-28"]
    healthy = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, dates, date(2026, 10, 22))
    hurt = rating.goalie_starts(model, "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, dates, date(2026, 10, 22), injury={1: 0.0})
    assert healthy[1] > healthy[2]
    assert hurt[1] == 0 and hurt[2] == pytest.approx(4.0)


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
    """A suspended regular takes the short-injury share and a healthy regular who hasn't played 0.38, not the idle cap (findings 118)."""
    by_name, _, _ = run
    for name, share in (("Brad Marchand", rating.PRESEASON_INJURED["short"]), ("Ryan Spooner", rating.UNPLAYED_REGULAR)):
        p = by_name[name]
        assert p["cr_why"]["gp"] == 0
        assert p["cr_season"] == pytest.approx(p["cr_fpg"] * share, abs=0.01)


def test_last_start_result_shifts_the_next_games():
    """A bad start (5+ against, or pulled after 3+) lowers the starter's chance in the next game and a little in
    the one after; a win raises it and a loss lowers it in the next game only (findings 131-133)."""
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
    loss = rating.last_start_shift({"ga": 3, "toi": 3600, "wins": 0, "losses": 0, "otl": 1})
    assert bad == (rating.BAD_START_LOGIT, rating.BAD_START_LOGIT_2)
    assert win == (rating.WIN_LOGIT, 0.0) and loss == (rating.LOSS_LOGIT, 0.0)
    p_ok, ok = run(None, 1)
    p_bad, b1 = run(bad, 1)
    p_win, _ = run(win, 1)
    p_loss, _ = run(loss, 1)
    assert p_bad < p_loss < p_ok < p_win
    assert b1[1] + b1[2] == pytest.approx(ok[1] + ok[2])  # the team still has one starter per game
    # two games: the bad start also costs a little in the second; a win or loss costs nothing there
    _, ok2 = run(None, 2)
    _, bad2 = run(bad, 2)
    _, win2 = run(win, 2)
    first_game = ok[1] - b1[1]
    assert ok2[1] - bad2[1] > first_game
    assert win2[1] - ok2[1] == pytest.approx(run(win, 1)[1][1] - ok[1], abs=0.05)
    assert rating.bad_start({"ga": 5, "toi": 3600}) and rating.bad_start({"ga": 3, "toi": 2000})
    assert not rating.bad_start({"ga": 3, "toi": 3600}) and not rating.bad_start({"ga": 2, "toi": 1200})


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
    monkeypatch.setattr(rating, "load_news", lambda: {})
    def mcavoy(**kw):
        fake = FakeSession(**kw)
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        players = [espn(104, "Charlie McAvoy", "BOS", "D"), espn(101, "Auston Matthews", "TOR", "C")]
        rating.add_ratings(players, 2027, TODAY, MONDAY)
        return players[0]
    listed, unlisted, no_list = mcavoy(), mcavoy(scratched=()), mcavoy(rail_ok=False)
    assert listed["sat_last"] and listed["cr_dress_next"] > unlisted["cr_dress_next"] + 0.03
    assert no_list["cr_dress_next"] == listed["cr_dress_next"]
    assert listed["cr_games"] > unlisted["cr_games"]
    base = rating.dress_by_game(0.25, 3, True, 1.0)
    up = rating.dress_by_game(0.25, 3, True, 1.0, rating.SCRATCH_LOGIT)
    assert all(b < u for b, u in zip(base, up)) and max(rating.dress_by_game(0.9, 3, True, 0.3, rating.SCRATCH_LOGIT)) <= 0.3


def test_just_traded_skater_dresses_more(monkeypatch, tmp_path):
    """A skater who dressed last game in his first 1-3 games for a new team gets +0.45 on his dress logit; from his
    4th game with the team, nothing (findings 155)."""
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    def matthews(moved, logit):
        monkeypatch.setattr(rating, "NEW_TEAM_LOGIT", logit)
        fake = FakeSession(moved={1: moved})
        monkeypatch.setattr(rating.requests, "Session", lambda: fake)
        players = [espn(101, "Auston Matthews", "TOR", "C"), espn(103, "David Pastrnak", "BOS", "RW")]
        rating.add_ratings(players, 2027, TODAY, MONDAY)
        return players[0]["cr_dress_next"]
    assert matthews(2, 0.45) > matthews(2, 0.0)  # 2 games with TOR after 2 with BOS
    assert matthews(0, 0.45) == matthews(0, 0.0)  # never moved


def test_dress_chance_by_game():
    """A skater who missed his team's last game is less likely to play the next one than later ones; a regular the
    reverse; caps still apply (findings 147)."""
    out = rating.dress_by_game(0.18, 4, True, 1.0)
    reg = rating.dress_by_game(0.91, 4, False, 1.0)
    assert out[0] == pytest.approx(0.12, abs=0.01) and out == sorted(out)
    assert reg[0] == pytest.approx(0.945, abs=0.01) and reg == sorted(reg, reverse=True)
    assert max(rating.dress_by_game(0.9, 3, False, 0.5)) <= 0.5
    assert rating.dress_by_game(0.5, 0, False, 1.0) == []
