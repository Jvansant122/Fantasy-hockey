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
    # tonight's line (dated Oct 14) never lands on a later game of the same team (handoff 614)
    later = winners.nhl_games(week, starts, results, {}, {}, {"TOR": {"2026-10-14"}}, lineups, {"_league": 0.9},
                              {"MTL": 0.45, "TOR": 0.55}, now, line_date="2026-10-14")
    assert all("p_line" not in g for g in later if g["date"] != "2026-10-14")


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


def test_matchup_spread_is_widened(monkeypatch):
    """League matchup margins run wider than the summed player spreads: the sd is MATCHUP_SD_SCALE x theirs, so a leader's
    win chance sits a little closer to even (handoff 505)."""
    a = [fa(i, "C", 2.0, [16, 17]) for i in range(12)]
    b = [fa(100 + i, "C", 1.8, [16, 17], team_id=2) for i in range(12)]
    sched = [{"matchupPeriodId": 3, "home": {"teamId": 1, "totalPointsLive": 10.0}, "away": {"teamId": 2, "totalPointsLive": 10.0}}]
    run = lambda: winners.fantasy_matchups(sched, 3, a + b, {1: "Mess", 2: "Other"}, 16, list(range(14, 21)), "2026-10-14T20:00:00Z")[0]
    wide = run()
    monkeypatch.setattr(winners, "MATCHUP_SD_SCALE", 1.0)
    narrow = run()
    assert wide["sd"] == round(1.04 * narrow["sd"] / 1.0, 1) or abs(wide["sd"] - 1.04 * narrow["sd"]) < 0.11
    assert 0.5 < wide["p_home"] < narrow["p_home"]


def test_games_in_progress_count_their_rest_and_keep_their_slots():
    """ESPN's points so far hold only what tonight's started games have produced yet: each started player in a lineup slot
    adds the rest of his game by the share of GAME_MINUTES left (handoff 750), and holds his slot tonight so the night
    never fills more than 9 F / 5 D / 1 UTL / 2 G (handoff 751)."""
    start, now = "2026-10-14T23:00:00Z", "2026-10-15T00:15:00Z"  # 75 minutes in: half the game left
    on = [fa(i, "C", 1.0, [16], today_start=start) for i in range(10)]  # 10 skater slots, all started tonight
    later = [fa(20 + i, "C", 5.0, [16], today_start="2026-10-15T02:00:00Z") for i in range(3)]
    mean, var = winners.team_projection(on + later, 16, [16], now)
    assert abs(mean - 10 * 0.5 * 1.0) < 1e-6 and var > 0  # no free slot left for the later game's forwards
    mean, _ = winners.team_projection(on[:7] + later, 16, [16], now)
    assert abs(mean - (7 * 0.5 + 3 * 5.0)) < 1e-6  # 7 slots held, 3 left for the later game
    bench = [dict(p, bench=True) for p in on]  # a started player on ESPN's bench holds no slot and adds nothing
    assert abs(winners.team_projection(bench + later, 16, [16], now)[0] - 15.0) < 1e-6
    assert winners.team_projection(on, 16, [16], "2026-10-15T01:40:00Z")[0] == 0  # past 150 minutes: nothing left
    g = fa(50, "G", 3.0, [16], cr_games=1.0, cr_g_nights={"16": 0.8}, today_start=start)
    assert abs(winners.team_projection([g], 16, [16], now)[0] - 0.5 * 0.8 * 3.0) < 1e-6


def test_a_last_game_in_progress_still_counts_its_rest():
    """After puck drop cr holds only later games, so it is 0 when tonight is his last game of the matchup (every Sunday
    game); the rest of that game still counts (handoff 754). A player with no rating adds nothing."""
    start, now = "2026-10-14T23:00:00Z", "2026-10-15T00:15:00Z"  # 75 minutes in: half the game left
    sk = fa(1, "C", 0, [16], today_start=start)
    sk["cr_fpg"] = 2.0
    g = fa(50, "G", 0, [16], cr_games=0, cr_g_nights={"16": 1.0}, today_start=start)
    g["cr_fpg"] = 3.0
    assert abs(winners.team_projection([sk, g], 16, [16], now)[0] - (0.5 * 2.0 + 0.5 * 3.0)) < 1e-6
    bare = {"id": 2, "pos": "C", "nights": [16], "team_id": 1, "today_start": start}
    assert winners.team_projection([bare], 16, [16], now)[0] == 0


def _last_week():
    """Last week's games as the Sunday-evening run published them: Saturday's graded, Sunday's still in progress."""
    return [dict(game(day="2026-10-10", started=True, p=0.6, frozen=True), home_score=3, away_score=1, home_won=True),
            dict(game(home="MTL", day="2026-10-11", started=True, p=0.7, frozen=True), state="LIVE"),
            dict(game(home="OTT", day="2026-10-11", started=True, p=None), state="LIVE")]


def test_last_weeks_games_are_finished_with_the_final_scores():
    """Handoff 808: the first run of a new week grades Sunday's games, which the Sunday runs saw only in progress."""
    results = {("MTL", "2026-10-11"): {"state": "OFF", "home_score": 2, "away_score": 4, "last": "REG"},
               ("OTT", "2026-10-11"): {"state": "OFF", "home_score": 5, "away_score": 1}}
    done = winners.finish_last_week(_last_week() + [game(day="2026-10-13")], "2026-10-12", results)
    assert [g["home"] for g in done] == ["TOR", "MTL", "OTT"]  # not this week's game
    assert winners.record(done) == {"games": 2, "right": 1, "home_right": 1}  # OTT had no pick
    assert winners.record(winners.finish_last_week(_last_week(), "2026-10-12", {}))["games"] == 1  # scores unavailable


def test_last_week_record_survives_the_monday_reset(monkeypatch, tmp_path):
    """Handoff 808: the first run of a week publishes last week's whole record, and later runs keep it."""
    import json
    import fetch
    import rating

    def sched(s, monday, games=None, weeks=1, starts=None, results=None, names=None):
        assert monday.isoformat() == "2026-10-05"
        results[("MTL", "2026-10-11")] = {"state": "OFF", "home_score": 6, "away_score": 2}
        return {}
    monkeypatch.setattr(rating, "week_schedule", sched)
    monkeypatch.setattr(fetch, "DATA_PATH", tmp_path / "players.json")
    first = fetch.last_week_record(None, _last_week(), "2026-10-12")
    assert first == {"week": "2026-10-05", "games": 2, "right": 2, "home_right": 2}
    (tmp_path / "players.json").write_text(json.dumps({"winners": {"games": [game(day="2026-10-12")], "record_last": first}}))
    assert fetch.last_week_record(None, fetch.previous_winners(), "2026-10-12") == first
    assert fetch.last_week_record(None, fetch.previous_winners(), "2026-10-19") is None  # a week later it's gone
