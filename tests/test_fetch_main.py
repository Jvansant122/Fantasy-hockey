"""Handoff 662 (hunt 14 audit3): scripts/fetch.py main() end to end on a fake ESPN league (fake_espn.py) and the repo's NHL fake, under
varied clocks, matchup shapes, schedules and feed failures. Each output must pass check_data's rules and winners checks,
keep goalie nights per team at most 1."""
import copy
import json
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

import check_data
import fetch
import leads
import rating
import test_add_ratings as T
import fake_espn as F
from fake_espn import ET, League, NHL, Combined, player, sp_of


def default_rosters(extra=()):
    t1 = [(player(101, "Auston Matthews", 10, "C"), 0), (player(102, "Morgan Rielly", 10, "D", inj="DAY_TO_DAY"), 4),
          (player(106, "Elias Pettersson", 10, "C"), 0), (player(111, "Joseph Woll", 10, "G", goalie=True), 5),
          (player(113, "Jeremy Swayman", 1, "G", goalie=True), 5)]
    t2 = [(player(103, "David Pastrnak", 1, "RW", inj="OUT"), 8), (player(104, "Charlie McAvoy", 1, "D"), 4),
          (player(112, "Anthony Stolarz", 10, "G", goalie=True), 5), (player(108, "Zack Bolduc", 1, "LW"), 7)]
    return {1: t1 + list(extra), 2: t2}


def default_fas():
    return [(player(107, "Elias N. Pettersson", 10, "D"), "FREEAGENT"), (player(110, "Ryan Spooner", 1, "C"), "FREEAGENT"),
            (player(109, "Brad Marchand", 1, "LW", inj="SUSPENSION"), "FREEAGENT"),
            (player(105, "Not A Real Player", 1, "C", gp=0, total=0.0, last=(0, 0.0), proj=(0, 0.0)), "WAIVERS")]


def run_fetch(monkeypatch, tmp_path, now_utc, league=None, nhl=None, fail=(), starters=None, odds=None):
    espn_day = (now_utc.astimezone(ET) - fetch.ESPN_ROLLOVER).date()
    league = league or League(sp_of(espn_day), rosters=default_rosters(), fas=default_fas())
    if league.rosters is None:
        league.rosters = default_rosters()
    if not league.fas:
        league.fas = default_fas()
    nhl = nhl or NHL()
    combined = Combined(league, nhl)
    monkeypatch.setenv("LEAGUE_ID", "1")
    monkeypatch.setenv("SEASON", "2027")
    monkeypatch.setenv("TEAM_ID", "1")
    monkeypatch.setattr(requests, "Session", lambda: combined)
    monkeypatch.setattr(fetch, "datetime", F.frozen(now_utc))
    monkeypatch.setattr(rating, "utcnow", lambda: now_utc)
    monkeypatch.setattr(rating, "et_date", lambda: now_utc.astimezone(ET).date())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(fetch, "DATA_PATH", tmp_path / "players.json")
    monkeypatch.setattr(fetch, "PRESEASON_FILE", tmp_path / "espn_preseason.json")
    monkeypatch.setattr(leads, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(leads, "NEWS_FILE", tmp_path / "dfo_news.json")

    def maybe(name, value):
        def f(*a, **k):
            if name in fail:
                raise requests.ConnectionError(f"{name} down")
            return copy.deepcopy(value(*a)) if callable(value) else copy.deepcopy(value)
        return f
    st = starters if starters is not None else (lambda s, day: {"date": day.isoformat(), "games": [
        {"team": "TOR", "goalie": "Anthony Stolarz", "status": "Confirmed"}, {"team": "BOS", "goalie": "Jeremy Swayman", "status": "Likely"}]})
    monkeypatch.setattr(leads, "fetch_starters", maybe("starters", st))
    monkeypatch.setattr(leads, "fetch_lineups", maybe("lineups", {"TOR": {"updated": "2026-10-01T12:00:00Z", "players": [
        ["Auston Matthews", "f1", None, None], ["Elias Pettersson", "f2", None, None]]}}))
    monkeypatch.setattr(leads, "fetch_injuries", maybe("injuries", [
        {"athlete_id": 103, "short": "Pastrnak (upper body) will undergo surgery Tuesday.", "long": "", "date": "2026-10-08T12:00Z"},
        {"athlete_id": 102, "short": "", "long": None, "date": None}]))
    monkeypatch.setattr(leads, "fetch_news", maybe("news", lambda s, now, stored=None: {"fetched": now.strftime("%Y-%m-%dT%H:%MZ"), "version": leads.NEWS_VERSION}))
    monkeypatch.setattr(leads, "fetch_odds", maybe("odds", odds if odds is not None else {"games": []}))
    for name in fail:
        if name.startswith("espn:"):
            view = name[5:]
            orig = league.get

            def bad(url, params=None, headers=None, timeout=None, orig=orig, view=view):
                if isinstance(params, list) and ("view", view) in params:
                    return T.Resp(status=503)
                return orig(url, params, headers, timeout)
            league.get = bad
        if name.startswith("nhl:"):
            key = name[4:]
            orig_n = nhl.get

            def badn(url, params=None, headers=None, timeout=None, orig=orig_n, key=key):
                if key in url:
                    return T.Resp(status=503)
                return orig(url, params, headers, timeout)
            nhl.get = badn
    fetch.main()
    return json.loads((tmp_path / "players.json").read_text())


def problems(d):
    out = check_data.rules(d) + check_data.winners_problems(d.get("winners") or {})
    out += [e for e in check_data.problems(d) if not e.startswith("only") and "players" not in e.split(":")[0]]
    nights = {}
    for p in d["players"]:
        if p["pos"] == "G" and p.get("cr_g_nights"):
            for sp, q in p["cr_g_nights"].items():
                nights[(p["team"], sp)] = nights.get((p["team"], sp), 0) + q
    out += [f"G-night {k} sums {v:.2f}" for k, v in nights.items() if v > 1.02]
    return out


def et(day, hour, minute=30):
    return datetime(2026, 10, day, hour, minute, tzinfo=ET).astimezone(timezone.utc)


OUTPUTS = {}


def keep(name, d):
    OUTPUTS[name] = d
    return d


@pytest.mark.parametrize("hour", range(24))
def test_every_hour_friday(monkeypatch, tmp_path, hour):
    d = keep(f"fri_{hour:02d}", run_fetch(monkeypatch, tmp_path, et(9, hour)))
    assert d["rated"], "rating failed"
    assert not problems(d)


@pytest.mark.parametrize("hour", [0, 2, 3, 4, 6])
def test_overnight_after_waiver_drop(monkeypatch, tmp_path, hour):
    """Team 2 dropped Bolduc on Thu Oct 8 at 1 PM ET: on waivers until Sat's 3 AM run."""
    drop = {"type": "FREEAGENT", "status": "EXECUTED", "teamId": 2, "scoringPeriodId": sp_of(date(2026, 10, 8)),
            "proposedDate": F.ms(datetime(2026, 10, 8, 13, tzinfo=ET)), "items": [{"type": "DROP", "playerId": 108, "fromTeamId": 2}]}
    lg = League(sp_of((et(9, hour).astimezone(ET) - fetch.ESPN_ROLLOVER).date()), tx=[drop])
    r = default_rosters()
    r[2] = [e for e in r[2] if e[0]["id"] != 108]
    lg.rosters = r
    lg.fas = default_fas() + [(player(108, "Zack Bolduc", 1, "LW"), "WAIVERS")]
    d = keep(f"waiver_{hour}", run_fetch(monkeypatch, tmp_path, et(9, hour), league=lg))
    assert not problems(d)
    b = next(p for p in d["players"] if p["id"] == 108)
    assert b["owner"] == "waivers" and 11 not in [sp for sp in b["nights"]]  # Fri isn't his (clears Sat 3 AM)


def test_sunday_last_night_after_puck(monkeypatch, tmp_path):
    d = keep("sun_2030", run_fetch(monkeypatch, tmp_path, et(11, 20)))
    assert not problems(d)


def test_monday_2am_espn_still_sunday(monkeypatch, tmp_path):
    d = keep("mon_0200", run_fetch(monkeypatch, tmp_path, et(12, 2)))
    assert not problems(d)


def test_two_week_playoff_round_first_week(monkeypatch, tmp_path):
    periods = {"1": [1], "2": [2, 3], "3": [4, 5]}
    lg = League(sp_of(date(2026, 10, 9)), period=2, periods=periods)
    d = keep("playoff_r1", run_fetch(monkeypatch, tmp_path, et(9, 10), league=lg))
    assert not problems(d) and d["matchup_weeks"] == 2 and len(d["nights"]) == 14 and d["move_limit"] == 14


def test_final_matchup(monkeypatch, tmp_path):
    periods = {"1": [1], "2": [2]}
    lg = League(sp_of(date(2026, 10, 9)), period=2, periods=periods)
    d = keep("final", run_fetch(monkeypatch, tmp_path, et(9, 10), league=lg))
    assert not problems(d) and d["final_matchup"] and d["move_limit_next"] is None


def test_final_two_week_last_day(monkeypatch, tmp_path):
    periods = {"1": [1], "2": [2]}  # the final ends Sun Oct 11
    lg = League(sp_of(date(2026, 10, 11)), period=2, periods=periods)
    d = keep("final_lastday", run_fetch(monkeypatch, tmp_path, et(11, 21), league=lg))
    assert not problems(d) and d["final_matchup"]


def test_after_the_season(monkeypatch, tmp_path):
    periods = {"1": [1]}
    lg = League(sp_of(date(2026, 10, 9)), period=1, periods=periods)
    d = keep("over", run_fetch(monkeypatch, tmp_path, et(9, 10), league=lg))
    assert d["season_over"]
    assert not problems(d)


def test_bye_week(monkeypatch, tmp_path):
    g = {10: {3, 5}, 1: set(range(3, 200, 2)), 8: set(range(4, 200, 3))}  # TOR: no games after Oct 3
    lg = League(sp_of(date(2026, 10, 9)), games=g)
    d = keep("bye", run_fetch(monkeypatch, tmp_path, et(9, 10), league=lg, nhl=NHL(bye=True, week_dates=[])))
    assert not problems(d)


def test_espn_drops_a_postponed_game(monkeypatch, tmp_path):
    """ESPN's schedule loses Sunday's BOS-TOR game (postponed); the NHL week schedule still lists it."""
    lg = League(sp_of(date(2026, 10, 9)), drop_from_schedule={(10, 13), (1, 13)})
    d = keep("postponed", run_fetch(monkeypatch, tmp_path, et(9, 10), league=lg, nhl=NHL(ppd={"2026-10-11"})))
    assert not problems(d)


def test_double_header_listed(monkeypatch, tmp_path):
    lg = League(sp_of(date(2026, 10, 9)), double={(10, 11), (1, 11)})
    d = keep("double", run_fetch(monkeypatch, tmp_path, et(9, 10), league=lg))
    assert not problems(d)


FEEDS = ["starters", "lineups", "injuries", "news", "odds", "espn:mTransactions2", "espn:kona_playercard",
         "espn:mMatchupScore", "nhl:right-rail", "nhl:moneypuck", "nhl:standings", "nhl:/roster/"]


@pytest.mark.parametrize("feed", FEEDS)
def test_feed_down(monkeypatch, tmp_path, feed):
    d = keep(f"down_{feed}", run_fetch(monkeypatch, tmp_path, et(9, 10), fail=(feed,)))
    assert d["rated"]
    assert not problems(d)


def test_roster_sizes_and_ir(monkeypatch, tmp_path):
    extra = [(player(200 + i, f"Depth Guy{i}", 8, "C"), 7) for i in range(17)] + [(player(230, "Hurt Guy", 8, "D", inj="INJURY_RESERVE"), 8)]
    lg = League(sp_of(date(2026, 10, 9)), rosters=default_rosters(extra))
    d = keep("roster23", run_fetch(monkeypatch, tmp_path, et(9, 10), league=lg))
    assert not problems(d)


def test_traded_mid_week(monkeypatch, tmp_path):
    lg = League(sp_of(date(2026, 10, 9)))
    lg.rosters = default_rosters()
    lg.rosters[1][0] = (player(101, "Auston Matthews", 1, "C"), 0)  # ESPN still lists BOS; the NHL moved him to TOR after 3 games
    d = keep("traded", run_fetch(monkeypatch, tmp_path, et(9, 10), league=lg, nhl=NHL(moved={1: 3})))
    assert not problems(d)


def test_unicode_and_unmatched(monkeypatch, tmp_path):
    extra = [(player(240, "Ëlias Lindhölm", 1, "C"), 7), (player(241, "Zzz Nobody", 8, "G", goalie=True), 5)]
    lg = League(sp_of(date(2026, 10, 9)), rosters=default_rosters(extra))
    d = keep("unicode", run_fetch(monkeypatch, tmp_path, et(9, 10), league=lg))
    assert not problems(d)


def test_same_player_on_two_rosters_and_in_the_pool(monkeypatch, tmp_path):
    lg = League(sp_of(date(2026, 10, 9)))
    lg.rosters = default_rosters()
    lg.rosters[1].append((player(104, "Charlie McAvoy", 1, "D"), 7))  # also on team 2
    lg.fas = default_fas() + [(player(101, "Auston Matthews", 10, "C"), "FREEAGENT")]  # rostered, but the pool still lists him
    d = keep("dupes", run_fetch(monkeypatch, tmp_path, et(9, 10), league=lg))
    assert not problems(d)
    assert sum(p["id"] == 104 for p in d["players"]) == 1 and next(p for p in d["players"] if p["id"] == 101)["owner"] == "mine"


def test_team_with_no_goalies_and_pending_moves(monkeypatch, tmp_path):
    """Team 1 holds no goalie; team 2 added Spooner after the first puck and dropped McAvoy after it (pending both ways)."""
    lg = League(sp_of(date(2026, 10, 9)))
    lg.rosters = default_rosters()
    lg.rosters[1] = [e for e in lg.rosters[1] if e[0]["defaultPositionId"] != 5]
    sp = sp_of(date(2026, 10, 9))
    t = F.ms(datetime(2026, 10, 9, 20, tzinfo=ET))
    lg.tx = [{"type": "FREEAGENT", "status": "EXECUTED", "teamId": 2, "scoringPeriodId": sp, "proposedDate": t,
              "items": [{"type": "ADD", "playerId": 110, "toTeamId": 2}, {"type": "DROP", "playerId": 104, "fromTeamId": 2}]}]
    d = keep("pending", run_fetch(monkeypatch, tmp_path, et(9, 21), league=lg))
    assert not problems(d)
    sp_ = next(p for p in d["players"] if p["id"] == 110)
    mc = next(p for p in d["players"] if p["id"] == 104)
    assert sp_.get("pending_add") and sp_["team_id"] == 2 and mc.get("pending_drop")


@pytest.mark.parametrize("hour", [1, 3, 10, 20])
def test_scoreboard_fallback(monkeypatch, tmp_path, hour):
    """ESPN's fantasy schedule view has no proTeams: the public scoreboard stands in (TOR-BOS games only)."""
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    lg = League(sp_of((et(9, hour).astimezone(ET) - fetch.ESPN_ROLLOVER).date()),
                games={10: set(range(3, 200, 2)), 1: set(range(3, 200, 2)), 8: set()})
    lg.no_pro = True
    d = keep(f"scoreboard_{hour}", run_fetch(monkeypatch, tmp_path, et(9, hour), league=lg))
    assert d["rated"] and not problems(d)


@pytest.mark.parametrize("day,hour", [(29, 10), (30, 2), (30, 20)])
def test_opening_matchup(monkeypatch, tmp_path, day, hour):
    """The 6-day opener (periods 1-6, Tue Sep 29 - Sun Oct 4), before and on the first NHL nights."""
    now = datetime(2026, 9, day, hour, 30, tzinfo=ET).astimezone(timezone.utc)
    lg = League(sp_of((now.astimezone(ET) - fetch.ESPN_ROLLOVER).date()))
    d = keep(f"opener_{day}_{hour}", run_fetch(monkeypatch, tmp_path, now, league=lg))
    assert not problems(d) and len(d["nights"]) == 6 and d["move_limit"] == 6


@pytest.mark.parametrize("hour", [3, 4, 9])
def test_monday_new_matchup(monkeypatch, tmp_path, hour):
    lg = League(sp_of((et(12, hour).astimezone(ET) - fetch.ESPN_ROLLOVER).date()), moves={1: {"2": 7}, 2: {"2": 3, "3": 1}})
    d = keep(f"mon_{hour}", run_fetch(monkeypatch, tmp_path, et(12, hour), league=lg, nhl=NHL(week_dates=[])))
    assert not problems(d)
