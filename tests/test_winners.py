"""The Predicted winners tab's model (scripts/winners.py, findings 450-451)."""
from datetime import datetime, timezone

import winners


def side(pts=10.0, gd=0.0, sv=0.905, b2b=0.0):
    return {"pts": pts, "gd": gd, "sv": sv, "b2b": b2b}


def test_win_prob_moves_the_right_way():
    even = winners.win_prob(side(), side())
    assert 0.52 < even < 0.54  # home ice alone
    assert winners.win_prob(side(pts=12), side()) > even
    assert winners.win_prob(side(gd=0.5), side()) > even
    assert winners.win_prob(side(b2b=1), side()) < even < winners.win_prob(side(), side(b2b=1))
    assert winners.win_prob(side(sv=0.92), side()) > even


def test_win_prob_uses_team_xg_when_both_sides_have_it():
    """Handoff 449: expected-goal differential takes over from goal differential; one side missing it falls back."""
    xg = lambda **k: {**side(**{a: b for a, b in k.items() if a != "xgd"}), "xgd": k.get("xgd", 0.0)}
    even = winners.win_prob(xg(), xg())
    assert 0.53 < even < 0.54  # the xG model's home ice
    assert winners.win_prob(xg(xgd=0.5), xg()) > even
    assert winners.win_prob(xg(gd=0.5), xg()) - even < winners.win_prob(side(gd=0.5), side()) - winners.win_prob(side(), side())
    assert winners.win_prob(xg(xgd=0.5), side()) == winners.win_prob(side(), side())  # only one side has xG: old model


def test_lineup_points_fill_and_scale():
    depth = {"F": 0.2, "D": 0.1}
    stars = [(i, "F") for i in range(14)] + [(100 + i, "D") for i in range(6)]
    rate = {i: 1.0 for i, _ in stars}
    full = winners.lineup_points(stars, rate, {i: 1.0 for i, _ in stars}, depth)
    assert abs(full - (12 * 1.0 + 6 * 1.0)) < 1e-9  # 14 forwards who all dress count as 12
    hurt = winners.lineup_points(stars, rate, {**{i: 1.0 for i, _ in stars}, **{i: 0.0 for i in range(10)}}, depth)
    assert abs(hurt - (4 * 1.0 + 8 * 0.2 + 6 * 1.0)) < 1e-9  # 10 forwards out: depth forwards fill the 8 slots


def test_skater_rates_shrink_toward_last_season():
    prev = {1: {"gp": 80, "pos": "C", "points": 1.0}, 2: {"gp": 3, "pos": "D", "points": 2.0}, 3: {"gp": 70, "pos": "D", "points": 0.3}}
    by_player = {1: [{"pos": "C", "points": 0}] * 10}
    rate, depth = winners.skater_rates(by_player, prev)
    assert abs(rate[1] - 40 / 50) < 1e-9
    assert rate[2] == depth["D"]  # too few games last season: a depth defenseman's rate


def game(home="TOR", day="2026-10-14", started=False, p=0.6, frozen=False):
    return {"home": home, "away": "BOS", "date": day, "started": started, "p_home": p, "inputs": {}, **({"frozen": True} if frozen else {})}


def test_freeze_keeps_the_pre_game_pick():
    prev = [game(p=0.61), game(home="MTL", started=True, p=None)]
    now = winners.freeze([game(started=True, p=0.7), game(home="MTL", started=True, p=0.55)], prev)
    assert now[0]["p_home"] == 0.61 and now[0]["frozen"]
    assert now[1]["p_home"] is None and not now[1]["frozen"]  # started before the tab had a pick for it
    again = winners.freeze([game(started=True, p=0.8)], now)
    assert next(g for g in again if g["home"] == "TOR")["p_home"] == 0.61


def test_freeze_carries_this_weeks_started_games_through_a_failed_run():
    """Handoff 436: a run with no NHL games keeps the week's locked picks; last week's games drop off."""
    prev = [game(started=True, p=0.61, frozen=True), game(home="MTL", day="2026-10-11", started=True, p=0.5, frozen=True),
            game(home="OTT", day="2026-10-16", p=0.4)]
    kept = winners.freeze([], prev, "2026-10-12")
    assert [(g["home"], g["p_home"]) for g in kept] == [("TOR", 0.61)]  # not last week's, not one still to start
    later = winners.freeze([game(home="OTT", day="2026-10-16", started=True, p=0.45)], kept, "2026-10-12")
    assert [(g["home"], g["p_home"]) for g in later] == [("TOR", 0.61), ("OTT", None)]
    assert winners.record([dict(g, home_won=True) for g in later])["games"] == 1


def test_record_counts_right_picks():
    gs = [dict(game(p=0.6), home_won=True), dict(game(p=0.6), home_won=False), dict(game(p=0.4), home_won=False),
          dict(game(p=None), home_won=True)]
    assert winners.record(gs) == {"games": 3, "right": 2, "home_right": 1}


def test_nhl_games_marks_started_finished_and_lines():
    now = datetime(2026, 10, 14, 23, 30, tzinfo=timezone.utc)
    week = {"TOR": [("2026-10-14", "BOS", True), ("2026-10-15", "MTL", False)], "BOS": [("2026-10-14", "TOR", False)],
            "MTL": [("2026-10-15", "TOR", True)]}
    starts = {("TOR", "2026-10-14"): "2026-10-14T23:00:00Z", ("MTL", "2026-10-15"): "2026-10-15T23:00:00Z"}
    results = {("TOR", "2026-10-14"): {"state": "OFF", "home_score": 3, "away_score": 2, "last": "OT"}}
    lineups = {"TOR": 11.0, "BOS": 10.0, "MTL": 9.0}
    out = winners.nhl_games(week, starts, results, {"TOR": "Maple Leafs"}, {}, {"TOR": {"2026-10-14"}}, lineups,
                            {"_league": 0.9}, {"MTL": 0.45, "TOR": 0.55}, now)
    assert [(g["home"], g["date"]) for g in out] == [("TOR", "2026-10-14"), ("MTL", "2026-10-15")]
    first, second = out
    assert first["started"] and first["home_won"] and first["last"] == "OT" and "p_line" not in first
    assert not second["started"] and second["p_line"] == 0.45
    assert second["inputs"]["b2b"] == [0, 1]  # Toronto played the night before


def fa(pid, pos, cr, nights, team_id=1, **kw):
    return {"id": pid, "pos": pos, "cr": cr, "cr_fpg": cr / len(nights) if nights else 0, "cr_dress": 1.0, "nights": nights,
            "team_id": team_id, **kw}


def test_fantasy_matchups_add_points_so_far_and_fill_the_lineup():
    a = [fa(i, "C", 2.0, [16, 17]) for i in range(12)]  # 12 forwards: 9 F + 1 UTL count each night
    a += [fa(50, "G", 3.0, [16, 17], cr_games=1.0, cr_g_nights={"16": 1.0})]
    b = [fa(100 + i, "D", 1.0, [17], team_id=2) for i in range(3)]
    sched = [{"matchupPeriodId": 3, "home": {"teamId": 1, "pointsByScoringPeriod": {"14": 10.0, "15": 5.0}},
              "away": {"teamId": 2, "totalPointsLive": 30.0}},
             {"matchupPeriodId": 2, "home": {"teamId": 1}, "away": {"teamId": 2}}]
    out = winners.fantasy_matchups(sched, 3, a + b, {1: "Mess", 2: "Other"}, 16, list(range(14, 21)), "2026-10-14T20:00:00Z")
    assert len(out) == 1
    m = out[0]
    assert m["home"]["now"] == 15.0 and m["away"]["now"] == 30.0
    assert abs(m["home"]["rest"] - (10 * 1.0 * 2 + 3.0)) < 0.05  # 10 skater slots a night at 1.0 each, plus the start
    assert abs(m["away"]["rest"] - 3.0) < 0.05
    assert 0.5 < m["p_home"] < 1.0
