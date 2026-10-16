"""Hunt 14 mutate stream: pins for the Week chance-to-play lifts after the boosted model (copy into tests/).
Each test passes on main 4940e32 and fails on the mutants named in its docstring (hunt14/mutate/RESULTS.md)."""
import pytest

import rating
from test_add_ratings import espn, FakeSession, TODAY, MONDAY, PAST


def _rate(monkeypatch, tmp_path, players, fake=None, **kw):
    fake = fake or FakeSession()
    monkeypatch.setattr(rating.requests, "Session", lambda: fake)
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    rating.add_ratings(players, 2027, TODAY, MONDAY, [], **kw)
    return {p["name"]: p for p in players}


def _row(pid, chances, team="TOR"):
    return {"p": {"cr_fpg": 2.0, "cr_why": {}}, "pid": pid, "team": team, "isD": False, "dates": ["2026-10-09", "2026-10-11"],
            "per_game": list(chances), "before": list(chances), "caps": [1.0, 1.0], "missed": 0, "after_trip": [False, False]}


def test_week_slack_moves_each_side_by_its_own_weight():
    """Handoff 517: an over-full game moves the logit by WEEK_SLACK[0] x s, a game with room by WEEK_SLACK[1] x s, and the
    over-full side is the stronger. Kills: R02 (sides swapped in week_slack), flip:WEEK_SLACK."""
    assert rating.WEEK_SLACK[0] > rating.WEEK_SLACK[1] > 0
    rows = [_row(k, (0.9, 0.6)) for k in range(15)]  # Oct 9: 13.5 chances for 12 spots (s = -0.5); Oct 11: 9 (s = +1)
    rating.week_slack(rows, {}, None)
    p = rows[0]["p"]
    sig = lambda z: 1 / (1 + rating.math.exp(-z))
    assert p["cr_dress_next"] == pytest.approx(sig(rating.logit(0.9) - 0.5 * rating.WEEK_SLACK[0]), abs=0.006)
    assert p["cr_games"] == pytest.approx(p["cr_dress_next"] + sig(rating.logit(0.6) + 1.0 * rating.WEEK_SLACK[1]), abs=0.011)


def test_regulars_due_back_fill_the_week_lineup():
    """Handoff 510: regulars due back count in the team's lineup sums, so a group with room fills up and its depth
    chances come down. Kills: R06 (subtracted instead of added), flip:WEEK_MISSING_A (sign pinned)."""
    assert rating.WEEK_MISSING_A > 0
    plain, back = [_row(k, (0.9, 0.9)) for k in range(12)], [_row(k, (0.9, 0.9)) for k in range(12)]
    rating.week_slack(plain, {}, None)
    rating.week_slack(back, {}, None, {("TOR", False): 2.0})
    assert plain[0]["p"]["cr_dress_next"] > 0.9 > back[0]["p"]["cr_dress_next"]


def test_long_scratch_gets_its_own_lift(monkeypatch, tmp_path):
    """Handoff 516: a healthy scratch out SCRATCH_LONG_GAMES+ straight games gets SCRATCH_LONG_LOGIT (shipped 0.375 > 0)
    in place of the position lift. Kills: R07 (turn inverted), zero:SCRATCH_LONG_LOGIT, flip:SCRATCH_LONG_LOGIT."""
    assert rating.SCRATCH_LONG_LOGIT > 0
    monkeypatch.setattr(rating, "SCRATCH_LONG_GAMES", 3)  # the fake league has 4 past games; McAvoy sat the last 3
    fake = lambda: FakeSession(missed={4: {1, 2, 3}})
    players = lambda: [espn(104, "Charlie McAvoy", "BOS", "D"), espn(103, "David Pastrnak", "BOS", "RW")]
    on = _rate(monkeypatch, tmp_path, players(), fake())["Charlie McAvoy"]
    monkeypatch.setattr(rating, "SCRATCH_LONG_LOGIT", 0.0)
    off = _rate(monkeypatch, tmp_path, players(), fake())["Charlie McAvoy"]
    assert on["sat_last"] and on["cr_games"] > off["cr_games"]


def _fill_in(monkeypatch, tmp_path, bands):
    monkeypatch.setattr(rating, "left_early_teammates", lambda *a: {("BOS", True): bands})
    return _rate(monkeypatch, tmp_path, [espn(104, "Charlie McAvoy", "BOS", "D"), espn(103, "David Pastrnak", "BOS", "RW")])["Charlie McAvoy"]


def test_teammate_who_played_least_sets_the_fill_in_lift_and_never_himself(monkeypatch, tmp_path):
    """Handoffs 352-353: with teammates in both bands the under-40% band's lift applies; his own short game is no
    teammate leaving early. Kills: R08 (max band), R09 (counts himself)."""
    both, low, mid = _fill_in(monkeypatch, tmp_path, {999: 0, 998: 1}), _fill_in(monkeypatch, tmp_path, {999: 0}), \
        _fill_in(monkeypatch, tmp_path, {998: 1})
    assert both["cr_games"] == low["cr_games"] > mid["cr_games"]
    himself = _fill_in(monkeypatch, tmp_path, {4: 0})  # McAvoy's own NHL id
    assert not himself["cr_why"].get("fills_in")


def test_power_play_scratch_rule_only_within_its_window(monkeypatch, tmp_path):
    """Handoff 359: the PP-scratch cut needs his last game within PP_SCRATCH_DAYS. Kills: R12 (window dropped)."""
    class PP(FakeSession):
        def get(self, url, params=None, **kw):
            r = super().get(url, params, **kw)
            if "skater/timeonice" in url and "gameDate" in (params or {}).get("cayenneExp", ""):
                for row in r.payload["data"]:
                    if row["playerId"] == 4:
                        row["ppTimeOnIce"] = 120
            return r
    players = lambda: [espn(104, "Charlie McAvoy", "BOS", "D"), espn(103, "David Pastrnak", "BOS", "RW")]
    assert _rate(monkeypatch, tmp_path, players(), PP())["Charlie McAvoy"]["cr_why"].get("pp_scratch")
    monkeypatch.setattr(rating, "PP_SCRATCH_DAYS", 3)  # his last game, Oct 5, is 4 days before the run
    assert not _rate(monkeypatch, tmp_path, players(), PP())["Charlie McAvoy"]["cr_why"].get("pp_scratch")


def test_scratch_after_a_win_sits_more(monkeypatch, tmp_path):
    """Handoff 357: after his team's win a healthy scratch's next game moves by SCRATCH_LAST_RESULT_BY_GAME['win'][0] < 0
    (BOS won its last game in the fake league). Kills: R14 (win/other swapped), zero/flip:SCRATCH_LAST_RESULT_BY_GAME."""
    assert rating.SCRATCH_LAST_RESULT_BY_GAME["win"][0] < 0 < rating.SCRATCH_LAST_RESULT_BY_GAME["other"][0]
    players = lambda: [espn(104, "Charlie McAvoy", "BOS", "D"), espn(103, "David Pastrnak", "BOS", "RW")]
    on = _rate(monkeypatch, tmp_path, players())["Charlie McAvoy"]
    monkeypatch.setattr(rating, "SCRATCH_LAST_RESULT_BY_GAME", {"win": (0.0,), "other": (0.0,)})
    off = _rate(monkeypatch, tmp_path, players())["Charlie McAvoy"]
    assert on["sat_last"] and on["cr_dress_next"] < off["cr_dress_next"]


def test_healthy_regular_lift_skips_a_player_with_news_since_his_last_game(monkeypatch, tmp_path):
    """Handoff 355: 'no injury listing or Daily Faceoff news since': a regular with a DFO item after his last game gets no
    HEALTHY_REG_LOGIT. Kills: R18."""
    news = {"recent": {"1": [{"ts": "2026-10-08T15:00:00", "cat": "inj", "f": ["out"]}]},
            "fetched": rating.utcnow().strftime("%Y-%m-%dT%H:%MZ")}
    def matthews(z):
        monkeypatch.setattr(rating, "HEALTHY_REG_LOGIT", z)
        return _rate(monkeypatch, tmp_path, [espn(101, "Auston Matthews", "TOR", "C"), espn(103, "David Pastrnak", "BOS", "RW")],
                     dfo_news=news)["Auston Matthews"]
    on, off = matthews(0.5), matthews(0.0)
    assert on["cr_why"].get("week_news") and on["cr_games"] == off["cr_games"]


def _bolduc(monkeypatch, tmp_path, points):
    class F(FakeSession):  # Bolduc on 10 minutes; a point in the last game
        def get(self, url, params=None, **kw):
            r = super().get(url, params, **kw)
            if "skater/summary" in url and "gameDate" in (params or {}).get("cayenneExp", ""):
                for row in r.payload["data"]:
                    if row["playerId"] == 7:
                        row["timeOnIcePerGame"] = 600
                        if row["gameDate"] == PAST[-1]:
                            row["points"] = row["assists"] = points
                            row["goals"] = 0
            return r
    return _rate(monkeypatch, tmp_path, [espn(107, "Zack Bolduc", "BOS", "RW"), espn(103, "David Pastrnak", "BOS", "RW")], F())["Zack Bolduc"]


def test_depth_skater_with_a_point_keeps_his_spot(monkeypatch, tmp_path):
    """Handoff 351: a healthy depth skater with a point in his last game gets DEPTH_POINT_LOGIT (> 0).
    Kills: zero:DEPTH_POINT_LOGIT, flip:DEPTH_POINT_LOGIT."""
    assert rating.DEPTH_POINT_LOGIT > 0
    monkeypatch.setattr(rating, "LOW_SCORER_LOGIT", 0.0)  # handoff 362's own lift for the lowest-minutes scorer: off here
    monkeypatch.setattr(rating, "BOOST_WEEK_W", 0.0)  # the weekly model takes back ~90% of a lift placed before it
    on = _bolduc(monkeypatch, tmp_path, 1)
    monkeypatch.setattr(rating, "DEPTH_POINT_LOGIT", 0.0)
    off = _bolduc(monkeypatch, tmp_path, 1)
    assert on["cr_why"].get("depth_point") and on["cr_games"] > off["cr_games"]


def test_week_lift_constants_keep_their_researched_direction():
    """The tests for 358-360 set their own values, so the shipped ones are pinned here.
    Kills: flip:ST_ROLE_LOGIT, flip:PP_SCRATCH_LOGIT, flip/zero:DEPTH_RANK_LOGIT, flip:SCRATCH_LOGIT, flip:NO_STATUS_OUT_LOGIT."""
    assert rating.ST_ROLE_LOGIT["pk"] > 0 > rating.ST_ROLE_LOGIT["none"]
    assert rating.PP_SCRATCH_LOGIT < 0
    assert rating.DEPTH_RANK_LOGIT["lowest"] < 0 < rating.DEPTH_RANK_LOGIT["other"]
    assert 0 < rating.SCRATCH_LOGIT["F"] < rating.SCRATCH_LOGIT["D"]
    assert rating.NO_STATUS_OUT_LOGIT < 0 and rating.DTD_NEXT_LOGIT < 0 and rating.HEALTHY_REG_LOGIT > 0 < rating.FIGHT_LOGIT
    assert all(b[1]["F"] > 0 and b[1]["D"] > 0 for b in rating.TEAMMATE_LEFT_BANDS)
    assert rating.TEAMMATE_LEFT_BANDS[0][1]["D"] > rating.TEAMMATE_LEFT_BANDS[1][1]["D"]
