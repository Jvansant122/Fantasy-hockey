"""Small pure helpers in rating.py and fetch.py."""
from datetime import date

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
