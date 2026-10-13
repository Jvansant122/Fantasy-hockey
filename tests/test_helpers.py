"""Small pure helpers in rating.py and fetch.py."""
from datetime import date, datetime

import pytest

import fetch
import rating


def test_norm_matches_espn_and_nhl_spellings():
    assert rating.norm("Tim Stützle") == rating.norm("Tim Stutzle") == "tim stutzle"
    assert rating.norm("Pierre-Luc Dubois") == "pierre luc dubois"
    assert rating.norm("J.T. Miller") == rating.norm("JT Miller")
    assert rating.norm("Mitchell J. Marner") == "mitchell marner"  # middle initial dropped
    assert rating.norm(None) == ""


def test_season_ids():
    assert rating.season_ids(2027) == (20262027, 20252026)


def test_weekly_covers_range_without_gaps():
    weeks = list(rating.weekly(date(2026, 9, 20), date(2026, 10, 8)))
    assert weeks[0][0] == "2026-09-20" and weeks[-1][1] == "2026-10-08"
    for (_, end), (start, _) in zip(weeks, weeks[1:]):
        assert (date.fromisoformat(start) - date.fromisoformat(end)).days == 1
    assert list(rating.weekly(date(2026, 10, 9), date(2026, 10, 8))) == []


def test_mean_skips_none():
    assert rating.mean([1, None, 3]) == 2
    assert rating.mean([None]) is None


def test_skater_features_dressed_share():
    games = [{"game": g, "fp": 2.0, "toi": 15, "pptoi": 2, "shots": 3, "iCF": 5, "hits": 1, "blockedShots": 1,
              "goals": 1, "assists": 0, "ppPoints": 0, "points": 1} for g in (1, 2, 4)]
    f = rating.skater_features(games, None, None, None, team_games=[1, 2, 3, 4])
    assert f["dressed3"] == 2 / 3
    assert f["dressed10"] == 3 / 4
    assert f["std_gp"] == 3 and f["no_prev"] == 1.0
    assert f["l5_fp"] == 2.0


def test_skater_features_without_games_leans_on_last_season():
    prev = {r: 1.0 for r in rating.GAME_RATES} | {"gp": 70}
    f = rating.skater_features([], prev, None, {"ixg": 0.3, "onice_xgf": 0.9}, team_games=[])
    assert f["l5_fp"] == 1.0 and f["std_toi"] == 1.0
    assert f["std_ixg"] == 0.3
    assert f["dressed3"] is None


def test_add_ranks_ties_groups_and_flags():
    def row(pid, pos, toi, pptoi, team="BOS"):
        return {"id": pid, "game": 1, "team": team, "pos": pos, "toi": toi, "pptoi": pptoi}
    rows = [row(1, "C", 20, 3), row(2, "L", 20, 3), row(3, "R", 18, 2.5), row(4, "C", 17, 2), row(5, "D", 24, 2),
            row(6, "D", 22, 0.4), row(7, "D", 22, 0), row(8, "C", 12, 0.3),
            row(9, "D", 10, 0, team="TOR")]  # the other team in the same game is ranked on its own
    rating.add_ranks(rows)
    r = {x["id"]: x for x in rows}
    # PP rank across all the team's skaters; ties share the smaller rank
    assert [r[i]["pp_rank"] for i in range(1, 9)] == [1, 1, 3, 4, 4, 6, 8, 7]
    # TOI rank within forwards (C/L/R together) and within defensemen
    assert [r[i]["toi_rank"] for i in (1, 2, 3, 4, 8)] == [1, 1, 3, 4, 5]
    assert [r[i]["toi_rank"] for i in (5, 6, 7)] == [1, 2, 2]
    assert r[9]["pp_rank"] == 1 and r[9]["toi_rank"] == 1
    # PP1: top 5 in PP time with over half a minute; top line: top 3 F or top 2 D
    assert [r[i]["pp1"] for i in range(1, 10)] == [1, 1, 1, 1, 1, 0, 0, 0, 0]
    assert [r[i]["top_line"] for i in range(1, 9)] == [1, 1, 1, 0, 1, 1, 1, 0]


def test_skater_features_ranks_and_usage_terms():
    games = [{"game": g, "fp": 2.0, "toi": t, "pptoi": 2, "shots": 3, "iCF": 5, "hits": 1, "blockedShots": 1, "goals": 1,
              "assists": 0, "ppPoints": 0, "points": 1, "pp_rank": k, "toi_rank": k, "pp1": 1.0, "top_line": 0.0}
             for g, t, k in ((1, 20, 1), (2, 22, 3))]
    f = rating.skater_features(games, None, None, None, team_games=[1, 2], is_d=True)
    assert f["is_D"] == 1.0 and f["std_pp_rank"] == 2 and f["l5_pp1"] == 1.0 and f["l20_top_line"] == 0.0
    assert "prev_pp_rank" not in f
    assert f["D_x_std_toi"] == 21 and f["std_toi2"] == 441 and f["std_pptoi2"] == 4
    fwd = rating.skater_features(games, None, None, None, team_games=[1, 2])
    assert fwd["is_D"] == 0.0 and fwd["D_x_std_toi"] == 0 and fwd["l5_toi2"] == 441
    prev = {r: 1.0 for r in rating.GAME_RATES} | {"gp": 70}
    assert rating.skater_features([], prev, None, None, team_games=[])["std_toi_rank"] is None  # no last-season window
    names = rating.Model().sk["features"]
    assert all(n in names for n in ("l5_pp_rank", "std_top_line", "D_x_l5_pptoi", "l5_toi2", "p2_fp", "no_p2", "p2_toi_w"))
    # two seasons ago (findings 310 (4)): his rates and games, weighted by games over 82; none -> the "no" flag
    p2 = {r: 2.0 for r in rating.P2_RATES} | {"gp": 41}
    with_p2 = rating.skater_features(games, None, None, None, team_games=[1, 2], p2=p2)
    assert with_p2["p2_fp"] == 2.0 and with_p2["p2_gp"] == 41 and with_p2["no_p2"] == 0.0 and with_p2["p2_toi_w"] == 1.0
    assert fwd["p2_fp"] is None and fwd["p2_gp"] == 0 and fwd["no_p2"] == 1.0 and fwd["p2_fp_w"] == 0.0


def test_stat_split_and_per_game():
    p = {"stats": [{"statSourceId": 0, "statSplitTypeId": 0, "seasonId": 2027, "appliedTotal": 10, "stats": {"34": 4}},
                   {"statSourceId": 1, "statSplitTypeId": 0, "seasonId": 2027, "appliedTotal": 99, "stats": {"34": 1}}]}
    st = fetch.stat_split(p, 0, 0, 2027)
    assert st["appliedTotal"] == 10
    assert fetch.stat_split(p, 0, 0, 2026) is None
    assert fetch.per_game(st, False) == (2.5, 4)
    assert fetch.per_game(None, False) == (None, 0)
    goalie = {"appliedTotal": 9, "stats": {"0": 3, "34": 4}}  # goalies divide by games started
    assert fetch.per_game(goalie, True) == (3.0, 3)


def test_matchup_move_limit_pools_the_daily_limit_over_the_matchup():
    acq = {"matchupAcquisitionLimit": 1.0, "matchupLimitPerScoringPeriod": True}
    assert fetch.matchup_move_limit(acq, 7, [7, 3, None]) == 7
    assert fetch.matchup_move_limit(acq, 6, [6]) == 6
    assert fetch.matchup_move_limit(acq, 6, [7]) is None  # contradicted by the counter
    assert fetch.matchup_move_limit({"matchupAcquisitionLimit": 6}, 7, [2]) == 6
    assert fetch.matchup_move_limit({"matchupAcquisitionLimit": -1}, 7, [2]) is None


def test_displaced_skater_is_least_used_at_the_returning_regulars_position():
    tg = {"BOS": list(range(1, 11))}
    g = lambda pid, game, pos, toi: {"id": pid, "game": game, "team": "BOS", "pos": pos, "toi": toi, "name": f"P{pid}"}
    by_player = {
        1: [g(1, n, "C", 18) for n in range(1, 8)],          # regular, missed games 8-10
        2: [g(2, n, "L", 15) for n in range(1, 11)],
        3: [g(3, n, "R", 8) for n in range(8, 11)],           # fill-in, least ice time
        4: [g(4, n, "D", 6) for n in range(1, 11)],           # a defenseman: not displaced by a forward
    }
    assert rating.displaced_skaters(by_player, tg, healthy={1}) == {3: (0.08, "P1")}
    assert rating.displaced_skaters(by_player, tg, healthy=set()) == {}  # still listed as out


def test_roster_moves_newcomer_sits_the_least_used_and_a_departed_d_brings_one_in():
    """Handoffs 337-338 (findings 269-272)."""
    tg = {"BOS": list(range(1, 11)), "TOR": list(range(101, 111))}
    g = lambda pid, game, pos, toi, team="BOS": {"id": pid, "game": game, "team": team, "pos": pos, "toi": toi, "name": f"P{pid}"}
    by_player = {
        1: [g(1, n, "C", 16, "TOR") for n in range(101, 110)],   # traded TOR -> BOS, no BOS game yet
        2: [g(2, n, "L", 15) for n in range(1, 11)],
        3: [g(3, n, "R", 9) for n in range(1, 11)],              # least-used BOS forward: sits more
        4: [g(4, n, "D", 20) for n in range(1, 11)],             # BOS D now listed on TOR: departed
        5: [g(5, n, "D", 14) for n in range(1, 9)],              # BOS D out the last 2 games: lifted
        6: [g(6, n, "D", 14) for n in range(1, 4)],              # out 7 games: not lifted
        7: [g(7, n, "D", 16) for n in range(1, 11)],
    }
    listed = {1: "BOS", 2: "BOS", 3: "BOS", 4: "TOR", 5: "BOS", 6: "BOS", 7: "BOS"}
    cuts, lifts = rating.roster_moves(by_player, tg, listed, healthy={2, 3, 5, 6, 7})
    assert cuts == {3: (0.08, "P1")}  # P4 joined TOR, which has no D in its last game to sit
    assert lifts == {5: "P4"}
    assert rating.roster_moves(by_player, tg, listed, healthy={2, 3, 6, 7})[1] == {}  # injured: no lift
    # three games for his new team: no longer a newcomer
    by_player[1] += [g(1, n, "C", 16) for n in range(8, 11)]
    assert 3 not in rating.roster_moves(by_player, tg, listed, healthy=set())[0]


def test_two_week_playoff_round_counts_14_days_and_14_moves():
    """Playoff rounds span two Monday-Sunday weeks (findings 109): in week 2 of a round a manager who used 7 still has 7."""
    periods = {str(k): [k] for k in range(1, 24)} | {"24": [24, 25], "25": [26, 27]}
    assert fetch.matchup_days(periods, 2, 11, 4) == list(range(7, 14))  # Friday of the regular week 2
    round1 = fetch.matchup_days(periods, 24, 170, 2)  # Wednesday of the round's second week
    assert round1 == list(range(161, 175))
    acq = {"matchupAcquisitionLimit": 1.0, "matchupLimitPerScoringPeriod": True}
    limit = fetch.matchup_move_limit(acq, len(round1), [7, 3])
    assert limit == 14 and limit - 7 == 7
    assert fetch.is_final_matchup(periods, 25) and not fetch.is_final_matchup(periods, 24)


def test_matchup_days_falls_back_to_this_week():
    assert fetch.matchup_days(None, 2, 11, 4) == list(range(7, 14))
    assert fetch.matchup_days({"2": [5]}, 2, 11, 4) == list(range(7, 14))  # map doesn't contain today
    assert fetch.matchup_days({"2": [2]}, 2, 12, 4) == list(range(8, 15))  # a stretched week: days don't line up with Monday
    assert not fetch.is_final_matchup(None, 3)


def test_week_schedule_reads_every_week_of_the_matchup(monkeypatch):
    seen = []

    class Resp:
        def __init__(self, day):
            self.day = day

        def json(self):
            return {"gameWeek": [{"date": self.day, "games": [{"gameType": 2, "awayTeam": {"abbrev": "BOS"}, "homeTeam": {"abbrev": "TOR"}}]}]}

    monkeypatch.setattr(rating, "nhl_get", lambda s, url: seen.append(url) or Resp(url.rsplit("/", 1)[1]))
    games = {}
    out = rating.week_schedule(None, date(2027, 3, 8), games, weeks=2)
    assert out["TOR"] == ["2027-03-08", "2027-03-15"] and len(games["BOS"]) == 2 and len(seen) == 2


def test_waiver_players_play_from_the_day_after_they_clear():
    """Waivers clear at ESPN's first ~3 AM ET run at least 24 hours after the drop (findings 122)."""
    from datetime import datetime
    et = fetch.ET
    ms = lambda *a: int(datetime(*a, tzinfo=et).timestamp() * 1000)
    tx = [{"status": "EXECUTED", "proposedDate": ms(2026, 10, 9, 20, 17), "items": [{"type": "DROP", "playerId": 1}, {"type": "ADD", "playerId": 9}]},
          {"status": "EXECUTED", "proposedDate": ms(2026, 10, 9, 2, 30), "items": [{"type": "DROP", "playerId": 2}]},
          {"status": "CANCELED", "proposedDate": ms(2026, 10, 9, 1), "items": [{"type": "DROP", "playerId": 3}]}]
    days = fetch.waiver_first_days(tx, date(2026, 10, 9))
    assert days[1] == date(2026, 10, 11)  # Fri 8:17 PM drop: 24 h is Sat evening, clears the 3 AM Sunday run
    assert days[2] == date(2026, 10, 10)  # Fri 2:30 AM drop: 24 h is Sat 2:30 AM, before that night's run
    assert 3 not in days and 9 not in days
    # a drop made by Sunday's 3:45 AM waiver run (a claim) clears at Monday's run (findings 290 (2))
    claim = [{"status": "EXECUTED", "type": "WAIVER", "proposedDate": ms(2026, 10, 10, 21), "processDate": ms(2026, 10, 11, 3, 45),
              "items": [{"type": "ADD", "playerId": 7}, {"type": "DROP", "playerId": 4}]}]
    assert fetch.waiver_first_days(claim, date(2026, 10, 11))[4] == date(2026, 10, 12)


def test_scoreboard_schedule_fails_on_a_lost_day(monkeypatch):
    """A day the scoreboard never returns would show its games as nights off, so the run fails instead (findings 230)."""
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)

    class Resp:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    class Session:
        fail = True

        def get(self, url, params=None, timeout=None):
            if url.endswith("/teams"):
                return Resp({"sports": [{"leagues": [{"teams": [{"team": {"id": "10", "abbreviation": "TOR"}}]}]}]})
            if self.fail and params["dates"].endswith(("1", "3", "5", "7", "9")):
                raise ConnectionError("boom")
            return Resp({"events": [{"date": "2026-10-10T23:00Z", "competitions": [{"competitors": [{"team": {"id": "10", "abbreviation": "TOR"}}]}]}]})

    with pytest.raises(RuntimeError):
        fetch.scoreboard_schedule(Session(), [1, 2], 1)
    ok = Session()
    ok.fail = False
    teams, counts, dates = fetch.scoreboard_schedule(ok, [1, 2], 1)
    assert counts == {1: 1, 2: 1} and teams[10]["periods"] == {1, 2}
    assert set(teams[10]["starts"].values()) == {"2026-10-10T23:00:00Z"}
    # a run between midnight and ESPN's ~3:25 AM rollover: today's period is still yesterday's date
    class Clock(fetch.datetime):
        @classmethod
        def now(cls, tz=None):
            return fetch.datetime(2026, 10, 13, 1, 30, tzinfo=tz)
    monkeypatch.setattr(fetch, "datetime", Clock)
    assert fetch.scoreboard_schedule(ok, [1, 2], 1)[2][1].date().isoformat() == "2026-10-12"


def test_pro_schedule_asks_for_the_right_view_and_dates_empty_nights(monkeypatch):
    """ESPN's view is proTeamSchedules_wl; the misspelled one returned nothing, so every run fell back (findings 230).
    A night with no NHL games has no date in the view and takes one from its neighbours."""
    asked = []
    day = lambda d: int(datetime(2026, 12, d, 19, tzinfo=fetch.ET).timestamp() * 1000)
    view = {"settings": {"proTeams": [
        {"id": 10, "abbrev": "Tor", "proGamesByScoringPeriod": {"85": [{"id": 1, "date": day(22)}], "88": [{"id": 2, "date": day(26)}]}},
        {"id": 1, "abbrev": "Bos", "proGamesByScoringPeriod": {"85": [{"id": 1, "date": day(22)}]}},
    ]}}

    def get(s, url, views):
        asked.append(views)
        return view if views == ["proTeamSchedules_wl"] else {}
    monkeypatch.setattr(fetch, "get", get)
    monkeypatch.setattr(fetch, "scoreboard_schedule", lambda *a: pytest.fail("fell back to the scoreboard"))
    teams, counts, dates = fetch.pro_schedule(None, "base", "league", [85, 86, 87, 88], 85)
    assert asked == [["proTeamSchedules_wl"]]
    assert counts == {85: 1, 88: 1} and teams[10]["periods"] == {85, 88}
    assert [dates[sp].day for sp in (85, 86, 87, 88)] == [22, 23, 25, 26]


def test_utc_iso():
    from datetime import datetime, timezone, timedelta
    assert fetch.utc_iso(datetime(2026, 10, 10, 19, 0, tzinfo=timezone(timedelta(hours=-4)))) == "2026-10-10T23:00:00Z"


def test_espn_today_follows_the_scoring_period():
    """ESPN rolls its day at about 3:30 AM ET: before that, its current period is still yesterday (findings 180)."""
    from datetime import date, datetime
    nights = {9: datetime(2026, 10, 10, 19, tzinfo=fetch.ET), 10: datetime(2026, 10, 11, 17, tzinfo=fetch.ET)}
    assert fetch.espn_today(nights, 9, date(2026, 10, 11)) == date(2026, 10, 10)  # 1 AM Sunday, ESPN still on Saturday
    assert fetch.espn_today(nights, 10, date(2026, 10, 11)) == date(2026, 10, 11)
    assert fetch.espn_today({12: nights[10]}, 10, date(2026, 10, 11)) == date(2026, 10, 9)  # nearest dated period
    assert fetch.espn_today({}, 10, date(2026, 10, 11)) == date(2026, 10, 11)


def test_free_agents_stop_counting_today_at_the_first_puck():
    teams = {1: {"periods": {5, 6}, "starts": {5: "2026-10-11T17:00:00Z"}},
             2: {"periods": {5}, "starts": {5: "2026-10-11T23:00:00Z"}},
             3: {"periods": {6}, "starts": {}}}
    first = fetch.first_puck(teams, 5)
    assert first == "2026-10-11T17:00:00Z" and fetch.first_puck(teams, 7) is None
    late = "2026-10-11T23:00:00Z"
    assert fetch.counts_until(late, "freeagent", first) == first
    assert fetch.counts_until(late, "waivers", first) == first
    assert fetch.counts_until(late, "mine", first) == late and fetch.counts_until(late, "other", first) == late
    assert fetch.counts_until(None, "freeagent", first) is None  # no game today


def test_adds_after_the_first_puck_stay_in_the_data():
    """Today's executed free-agent and waiver adds that ESPN's roster doesn't list yet (findings 236)."""
    tx = [{"type": "FREEAGENT", "status": "EXECUTED", "scoringPeriodId": 12, "teamId": 3, "proposedDate": 2,
           "items": [{"type": "ADD", "playerId": 1, "toTeamId": 3}, {"type": "DROP", "playerId": 2, "fromTeamId": 3}]},
          {"type": "WAIVER", "status": "EXECUTED", "scoringPeriodId": 12, "teamId": 4, "proposedDate": 3,
           "items": [{"type": "ADD", "playerId": 5, "toTeamId": 4}]},
          {"type": "FREEAGENT", "status": "EXECUTED", "scoringPeriodId": 11, "teamId": 4, "proposedDate": 1,
           "items": [{"type": "ADD", "playerId": 6, "toTeamId": 4}]},  # yesterday
          {"type": "FREEAGENT", "status": "CANCELED", "scoringPeriodId": 12, "teamId": 4, "proposedDate": 4,
           "items": [{"type": "ADD", "playerId": 7, "toTeamId": 4}]},
          {"type": "FREEAGENT", "status": "EXECUTED", "scoringPeriodId": 12, "teamId": 5, "proposedDate": 5,
           "items": [{"type": "ADD", "playerId": 8, "toTeamId": 5}]},
          {"type": "FREEAGENT", "status": "EXECUTED", "scoringPeriodId": 12, "teamId": 5, "proposedDate": 6,
           "items": [{"type": "DROP", "playerId": 8, "fromTeamId": 5}]}]  # added and dropped again
    assert fetch.pending_adds(tx, {5: (4, 7)}, 12) == {1: 3}  # 5 is already on ESPN's roster


def test_schedule_fields_and_nights_by_scoring_period():
    team = {"abbrev": "Tor", "periods": {7, 9, 11, 14}, "starts": {9: "2026-10-07T23:00:00Z"}}
    sched = {"matchup_sps": list(range(7, 14)), "remaining": [9, 10, 11, 12, 13], "light": {11}, "next_sps": list(range(14, 28)),
             "today_sp": 9}
    f = fetch.schedule_fields(team, sched, 0, "2026-10-07T23:00:00Z", "2026-10-07T22:00:00Z")
    assert (f["team"], f["games"], f["games_left"], f["light_left"], f["nights"], f["games_next2"]) == ("TOR", 3, 2, 1, [7, 9, 11], 1)
    assert fetch.schedule_fields(team, sched, 0, "2026-10-07T23:00:00Z", "2026-10-08T00:00:00Z")["games_left"] == 1  # started
    assert fetch.schedule_fields(team, sched, 10, None, "2026-10-07T22:00:00Z")["nights"] == [11]  # on waivers until sp 10
    assert fetch.nights_by_sp({"2026-10-07": 0.6, "2026-10-09": 0.4, "bad": 1}, date(2026, 10, 7), 9) == {9: 0.6, 11: 0.4}


def test_skaters_with_no_games_use_last_seasons_usage_ranks():
    """Before his first game a skater's rank inputs come from last season, not the training mean (findings 213)."""
    rates = {i: {"gp": 80 - i, "pos": "C" if i < 14 else "D", "toi": 22 - i, "pptoi": max(0, 4 - i * 0.5)} for i in range(22)}
    rates[99] = {"gp": 5, "pos": "D", "toi": 30, "pptoi": 0}  # a part-timer still gets ranked against the regulars
    rating.prev_ranks(rates, {i: "BOS" for i in rates})
    assert (rates[0]["pp_rank"], rates[0]["toi_rank"], rates[0]["pp1"], rates[0]["top_line"]) == (1, 1, 1.0, 1.0)
    assert rates[13]["toi_rank"] == 13 and rates[13]["pp1"] == 0.0  # 13th forward: outside the regulars, ranked behind them
    assert rates[99]["toi_rank"] == 1 and rates[99]["top_line"] == 1.0 and rates[99]["pp1"] == 0.0
    f = rating.skater_features([], rates[0], None, None, [], is_d=False)
    assert f["std_pp_rank"] == f["l5_toi_rank"] == 1 and f["l10_pp1"] == 1.0
    assert rating.skater_features([], None, None, None, [])["std_pp_rank"] is None  # nothing to go on: training mean


def test_roster_limit_leaves_out_the_ir_slot():
    """ESPN's limit is every lineup and bench slot outside IR: 22 in this league (handoff 128)."""
    counts = {"0": 2, "1": 2, "2": 2, "3": 3, "4": 5, "5": 2, "6": 1, "7": 5, "8": 2}
    assert fetch.roster_limit(counts) == 22
    assert fetch.roster_limit({}) is None and fetch.roster_limit(None) is None


def test_season_last_day_ends_with_the_final():
    """The season ends on the last day of the final's second week (handoff 139): Sun Apr 4, 2027 is day 188."""
    periods = {str(k): [k] for k in range(1, 24)} | {"24": [24, 25], "25": [26, 27]}
    assert fetch.season_last_day(periods) == 188
    assert fetch.season_last_day({"1": [1]}) == 6
    assert fetch.season_last_day(None) is None


def test_season_from_the_date_and_off_season_runs_skip(tmp_path):
    """The ESPN season comes from the date, and once the published data says the season is over, runs change nothing
    (findings 230)."""
    from datetime import date
    assert fetch.default_season(date(2026, 10, 11)) == 2027
    assert fetch.default_season(date(2027, 3, 1)) == 2027
    assert fetch.default_season(date(2027, 8, 1)) == 2028
    p = tmp_path / "players.json"
    assert not fetch.season_over_published(p)  # no data yet
    p.write_text('{"season_over": false}')
    assert not fetch.season_over_published(p)
    p.write_text('{"season_over": true}')
    assert fetch.season_over_published(p)
    p.write_text("not json")
    assert not fetch.season_over_published(p)


def test_season_weeks_cover_every_later_week_the_schedule_has():
    """The Nights week picker's weeks (findings 300): Monday-Sunday, labelled by date, light nights 1-5 games, and only
    weeks the schedule covers."""
    from datetime import timedelta
    today_sp, today_et = 13, date(2026, 10, 11)  # Sunday Oct 11 is period 13, the end of week 2
    dates = {sp: today_et + timedelta(days=sp - today_sp) for sp in range(1, 60)}
    counts = {sp: (3 if sp % 7 == 2 else 9) for sp in range(1, 60)}
    counts[30] = 0  # a night with no NHL games isn't light
    weeks = fetch.season_weeks(counts, dates, today_sp, today_et)
    assert [w["week"] for w in weeks] == list(range(2, 9))  # week 9 (periods 56-62) runs past the schedule
    assert weeks[0]["sps"] == list(range(7, 14)) and weeks[0]["label"] == "Oct 5-11" and weeks[0]["dates"][0] == "Mon 5"
    assert weeks[1]["label"] == "Oct 12-18" and weeks[1]["dates"] == ["Mon 12", "Tue 13", "Wed 14", "Thu 15", "Fri 16", "Sat 17", "Sun 18"]
    oct26 = next(w for w in weeks if w["sps"][0] == 28)
    assert oct26["label"] == "Oct 26-Nov 1" and oct26["games"][30] == 0 and 30 not in oct26["light"]
    assert all(set(w["light"]) == {sp for sp in w["sps"] if 0 < counts[sp] <= 5} for w in weeks)
    # the season's last day ends the list; a scoreboard window only covers its own weeks
    assert fetch.season_weeks(counts, dates, today_sp, today_et, last_sp=27)[-1]["sps"][-1] == 27
    window = {sp: d for sp, d in dates.items() if 6 <= sp <= 33}  # week 5 (28-34) is cut short
    assert [w["week"] for w in fetch.season_weeks(counts, window, today_sp, today_et)] == [2, 3, 4]
    assert fetch.season_weeks(counts, {}, today_sp, today_et) == []
    # the season's first games on a Tuesday (period 8): this week still counts from its Monday
    opener = {sp: d for sp, d in dates.items() if sp >= 8}
    assert fetch.season_weeks(counts, opener, 8, today_et - timedelta(days=5))[0]["sps"] == list(range(7, 14))
    assert fetch.week_sps(1) == [1, 2, 3, 4, 5, 6] and fetch.week_sps(3) == list(range(14, 21))


def test_team_nights_run_from_today_to_the_last_week():
    weeks = [{"sps": list(range(7, 14))}, {"sps": list(range(14, 21))}]
    teams = {1: {"abbrev": "Bos", "periods": {5, 13, 15, 20, 22}}, 2: {"abbrev": "", "periods": {13}}, 3: {"abbrev": "TB", "periods": set()}}
    assert fetch.team_nights(teams, 13, weeks) == {"BOS": [13, 15, 20], "TB": []}
    assert fetch.team_nights(teams, 13, []) == {"BOS": [], "TB": []}


def test_drops_after_the_first_puck_stay_until_the_rollover():
    """Today's executed drops whose player ESPN's roster still lists, for the team that dropped him (findings 241 (3))."""
    tx = [{"type": "FREEAGENT", "status": "EXECUTED", "scoringPeriodId": 12, "teamId": 3, "proposedDate": 2,
           "items": [{"type": "ADD", "playerId": 1, "toTeamId": 3}, {"type": "DROP", "playerId": 2, "fromTeamId": 3}]},
          {"type": "WAIVER", "status": "EXECUTED", "scoringPeriodId": 12, "teamId": 4, "proposedDate": 3,
           "items": [{"type": "DROP", "playerId": 5, "fromTeamId": 4}]},
          {"type": "FREEAGENT", "status": "EXECUTED", "scoringPeriodId": 11, "teamId": 4, "proposedDate": 1,
           "items": [{"type": "DROP", "playerId": 6, "fromTeamId": 4}]},  # yesterday: rolled over
          {"type": "FREEAGENT", "status": "CANCELED", "scoringPeriodId": 12, "teamId": 4, "proposedDate": 4,
           "items": [{"type": "DROP", "playerId": 7, "fromTeamId": 4}]},
          {"type": "FREEAGENT", "status": "EXECUTED", "scoringPeriodId": 12, "teamId": 5, "proposedDate": 5,
           "items": [{"type": "DROP", "playerId": 8, "fromTeamId": 5}]},
          {"type": "FREEAGENT", "status": "EXECUTED", "scoringPeriodId": 12, "teamId": 6, "proposedDate": 6,
           "items": [{"type": "ADD", "playerId": 8, "toTeamId": 6}]},  # dropped, then picked up again
          {"type": "TRADE_ACCEPT", "status": "EXECUTED", "scoringPeriodId": 12, "teamId": 4, "proposedDate": 7,
           "items": [{"type": "DROP", "playerId": 9, "fromTeamId": 4}]}]  # not a free-agent move
    rostered = {2: (3, 7), 5: (4, 1), 6: (4, 7), 7: (4, 7), 8: (5, 7), 9: (4, 7), 10: (3, 7)}
    assert fetch.pending_drops(tx, rostered, 12) == {2: 3, 5: 4}
    assert fetch.pending_drops(tx, {2: (9, 7)}, 12) == {}  # listed on another team: not his old team's
    assert fetch.pending_drops(None, rostered, 12) == {}


def test_schedule_fields_stop_at_the_last_day_he_counts():
    team = {"abbrev": "Tor", "periods": {7, 9, 11, 14}, "starts": {9: "2026-10-07T23:00:00Z"}}
    sched = {"matchup_sps": list(range(7, 14)), "remaining": [9, 10, 11, 12, 13], "light": {11}, "next_sps": list(range(14, 28)),
             "today_sp": 9}
    f = fetch.schedule_fields(team, sched, 0, "2026-10-07T23:00:00Z", "2026-10-07T22:00:00Z", 9)  # dropped after the first puck
    assert (f["games"], f["games_left"], f["light_left"], f["nights"], f["games_next2"]) == (2, 1, 0, [7, 9], 0)
    assert fetch.schedule_fields(team, sched, 0, "2026-10-07T23:00:00Z", "2026-10-08T00:00:00Z", 9)["games_left"] == 0


def test_week_schedule_records_start_times(monkeypatch):
    class Resp:
        def json(self):
            return {"gameWeek": [{"date": "2026-10-07", "games": [
                {"gameType": 2, "awayTeam": {"abbrev": "BOS"}, "homeTeam": {"abbrev": "TOR"}, "startTimeUTC": "2026-10-07T23:00:00Z"},
                {"gameType": 2, "awayTeam": {"abbrev": "MTL"}, "homeTeam": {"abbrev": "OTT"}}]}]}
    monkeypatch.setattr(rating, "nhl_get", lambda s, url: Resp())
    starts = {}
    rating.week_schedule(None, date(2026, 10, 5), starts=starts)
    assert starts == {("BOS", "2026-10-07"): "2026-10-07T23:00:00Z", ("TOR", "2026-10-07"): "2026-10-07T23:00:00Z"}


def test_carry_injury_notes_edge_cases():
    """A swapped note is marked seen; a note on a player outside the data takes the feed's status (findings 241 (5))."""
    today = date(2026, 12, 15)
    stored = {}
    notes = {5: {"short": "Smith will miss the rest of the season.", "date": "2026-10-01T12:00Z", "status": "Injured Reserve"}}
    rating.carry_injury_notes(notes, stored, {}, lambda aid, when: False, today)
    assert stored["5"]["status"] == "INJURY_RESERVE"
    stored["5"]["seen"] = "2026-10-01"
    notes = {5: {"short": "ir", "date": "2026-12-15T10:00Z"}}
    seen = []
    assert rating.carry_injury_notes(notes, stored, {5: "INJURY_RESERVE"}, lambda aid, when: seen.append(when) or False, today) == {5}
    assert notes[5]["short"].startswith("Smith") and stored["5"]["seen"] == "2026-12-15"
    assert seen == ["2026-10-01T12:00Z"]  # played_since gets the note's full time, not its UTC date
    stored["5"]["status"] = None  # no fantasy status and an unknown feed word: never carried
    assert rating.carry_injury_notes({5: {"short": "ir"}}, stored, {5: "INJURY_RESERVE"}, lambda aid, when: False, today) == set()


def test_sp1_date_from_the_season_schedule():
    """ESPN's period 1 from the whole season's game dates, so the publish check's day-shift rule works next season too;
    a short scoreboard window gives none (findings 242)."""
    from datetime import timedelta
    start = date(2027, 10, 5)
    nights = {sp: datetime(2027, 10, 5, 19) + timedelta(days=sp - 1) for sp in range(3, 60)}
    nights[40] += timedelta(days=1)  # one stray late game doesn't move it
    assert fetch.sp1_date(nights) == start.isoformat()
    assert fetch.sp1_date({sp: d for sp, d in nights.items() if sp < 20}) is None


def test_lineup_context_counts_regulars_above_him_who_missed_the_last_game():
    tg = {"BOS": list(range(1, 11))}
    g = lambda pid, game, pos, toi, pp: {"id": pid, "game": game, "team": "BOS", "pos": pos, "toi": toi, "pptoi": pp}
    by_player = {
        1: [g(1, n, "C", 19, 3.0) for n in range(1, 10)],     # top line and PP1, missed game 10
        2: [g(2, n, "L", 15, 1.0) for n in range(1, 11)],
        3: [g(3, n, "R", 8, 0.0) for n in range(8, 11)],       # 3 games: too few for the counts
        4: [g(4, n, "D", 22, 2.0) for n in range(1, 11)],      # more PP time than 2 but not than 1
    }
    c = rating.lineup_context(by_player, tg)
    assert {k: c[1][k] for k in ("missed_last", "out_above_toi", "out_above_pp")} == {"missed_last": 1.0, "out_above_toi": 0.0, "out_above_pp": 0.0}
    assert {k: c[2][k] for k in ("missed_last", "out_above_toi", "out_above_pp")} == {"missed_last": 0.0, "out_above_toi": 1.0, "out_above_pp": 1.0}
    assert {k: c[3][k] for k in ("missed_last", "out_above_toi", "out_above_pp")} == {"missed_last": 0.0, "out_above_toi": 0.0, "out_above_pp": 0.0}
    assert {k: c[4][k] for k in ("missed_last", "out_above_toi", "out_above_pp")} == {"missed_last": 0.0, "out_above_toi": 0.0, "out_above_pp": 1.0}  # a forward: not above him in TOI
    gaps = rating.context_gaps({"missed_last": 1.0, "l10_toi": 19, "last_toi": 12, "out_above_toi": 5.0, "out_above_pp": 0.0,
                                "l10_pptoi": 3.0, "pp_l1": None})
    assert gaps == {"ml_gap": 7, "ml_ppgap": None, "oa_gap": 3 * (12 - 19), "oap_gap": None}


def test_next_matchup_length():
    """Adds after Sunday's first puck use the next matchup's moves (findings 290 (1)): 14 before a two-week round."""
    assert fetch.next_matchup_days({"21": [22, 23], "20": [21]}, 20) == 14
    assert fetch.next_matchup_days({"2": [2], "1": [1]}, 1) == 7
    assert fetch.next_matchup_days(None, 5) == 7
