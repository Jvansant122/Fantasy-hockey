"""Early-season pulls toward ESPN's draft market for goalies' Season (handoff 758) and skaters' Week pts/game (759)."""
import math

import pytest

import rating


def goalies(n=12, games=0):
    gs = [{"id": i, "pos": "G", "team": "TOR", "cr_matched": True, "adp": 20.0 * (i + 1), "cr_season": 1.0 + 0.1 * i,
           "cr_why": {}} for i in range(n)]
    gs.append({"id": 99, "pos": "G", "team": "TOR", "cr_matched": True, "adp": 5.0, "cr_season": 0.0, "cr_why": {}})
    return gs, {"TOR": [None] * games}


def test_goalie_season_moves_toward_the_draft_market():
    gs, tg = goalies()
    before = [g["cr_season"] for g in gs]
    rating.adp_pull_goalies(gs, tg)
    assert gs[0]["cr_season"] > before[0] and gs[0]["cr_why"]["adp_pull"] > 1  # drafted first, lowest Season
    assert gs[11]["cr_season"] < before[11] and gs[11]["cr_why"]["adp_pull"] < 1
    assert gs[12]["cr_season"] == 0.0 and "adp_pull" not in gs[12]["cr_why"]  # a 0 stays 0, out of the pool
    q = rating._avg_rank([-g["adp"] for g in gs[:12]])[0] - rating._avg_rank(before[:12])[0]
    assert gs[0]["cr_why"]["adp_pull"] == pytest.approx(math.exp(rating.ADP_PULL_C_G * q), abs=1e-3)


def test_goalie_pull_fades_and_needs_distinct_adps():
    gs, tg = goalies(games=rating.ADP_PULL_K)
    rating.adp_pull_goalies(gs, tg)
    assert all("adp_pull" not in g["cr_why"] for g in gs)
    gs, tg = goalies()
    for g in gs[3:]:
        g["adp"] = 230.0
    rating.adp_pull_goalies(gs, tg)
    assert all("adp_pull" not in g["cr_why"] for g in gs)


def skaters(n=24, games=0):
    ps = [{"id": i, "pos": "C", "team": "TOR", "cr_matched": True, "adp": 10.0 * (i + 1), "cr_fpg": 1.0 + 0.05 * i,
           "cr_games": 3.0, "cr": round((1.0 + 0.05 * i) * 3, 2), "cr_season": 5.0, "cr_why": {"season_pts": 80.0}}
          for i in range(n)]
    return ps, {"TOR": [None] * games}


def test_week_pts_per_game_moves_toward_the_draft_market_and_keeps_week_equal_rate_times_games():
    ps, tg = skaters(games=6)  # half of ADP_WEEK_K: half the day-one exponent
    rating.adp_week_pull(ps, tg)
    assert ps[0]["cr_why"]["adp_week"] > 1 > ps[23]["cr_why"]["adp_week"]
    q = rating._avg_rank([-p["adp"] for p in ps])[0] - rating._avg_rank([1.0 + 0.05 * i for i in range(24)])[0]
    assert ps[0]["cr_why"]["adp_week"] == pytest.approx(math.exp(rating.ADP_WEEK_C * 0.5 * q), abs=1e-3)
    for p in ps:
        assert p["cr"] == pytest.approx(round(p["cr_fpg"] * p["cr_games"], 2))  # P4
        assert p["cr_season"] == 5.0  # the Season's own factor is 757's, not this one


def test_week_pull_is_off_after_the_first_weeks_or_with_flat_adp():
    ps, tg = skaters(games=rating.ADP_WEEK_K)
    rating.adp_week_pull(ps, tg)
    assert all("adp_week" not in p["cr_why"] for p in ps)
    ps, tg = skaters()
    for p in ps[5:]:
        p["adp"] = 230.0
    rating.adp_week_pull(ps, tg)
    assert all("adp_week" not in p["cr_why"] for p in ps)


def test_a_free_agent_without_team_games_takes_the_league_median():
    assert rating.team_games_of({"team": "FA"}, {"TOR": [1] * 4, "BOS": [1] * 6, "MTL": [1] * 8}) == 6
    assert rating.team_games_of({"team": "TOR"}, {"TOR": [1] * 4}) == 4
    assert rating.team_games_of({"team": "LA"}, {"LAK": [1] * 3}) == 3
