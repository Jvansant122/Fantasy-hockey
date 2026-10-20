"""Hunt 14 mutate2: the Season lift for scratched defensemen (handoffs 729/730, findings 527): + 0.8 x min(k, 5)/5 on the
Season logit, fading to 0 at the team's 20th game, defensemen only. Passes on main af32a42; kills the mutants named in each
docstring (hunt14 mutate2 RESULTS.md)."""
import math

import pytest

import rating
from test_add_ratings import espn, FakeSession, TODAY, MONDAY


def _share(monkeypatch, tmp_path, who, **kw):
    """His rest-of-season dress share as the Season rating set it (the row handed to lineup_slack)."""
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    fake = FakeSession(**kw)
    monkeypatch.setattr(rating.requests, "Session", lambda: fake)
    seen, real = {}, rating.lineup_slack
    monkeypatch.setattr(rating, "lineup_slack", lambda rows, *a, **k: seen.update({r["p"]["name"]: r["share"] for r in rows}) or real(rows, *a, **k))
    players = [espn(104, "Charlie McAvoy", "BOS", "D"), espn(101, "Auston Matthews", "TOR", "C")]
    rating.add_ratings(players, 2027, TODAY, MONDAY)
    return seen[who]


def _lift(monkeypatch, tmp_path, who="Charlie McAvoy", **kw):
    """The rule's lift on the Season dress logit: logit(share) with the rule minus without."""
    lg = lambda q: math.log(q / (1 - q))
    on = _share(monkeypatch, tmp_path, who, **kw)
    with monkeypatch.context() as m:
        m.setattr(rating, "SEASON_SCRATCH_D_LOGIT", 0.0)
        off = _share(m, tmp_path, who, **kw)
    return lg(on) - lg(off)


def test_lift_grows_with_the_scratch_streak(monkeypatch, tmp_path):
    """Findings 527: the lift is by streak length, min(k, 5)/5; two straight scratches lift more than one.
    Kills: E11 (full lift at one game)."""
    one = _lift(monkeypatch, tmp_path)  # sat game 3 as a scratch
    two = _lift(monkeypatch, tmp_path, missed={4: {2, 3}})
    assert one > 0.05 and two == pytest.approx(2 * one, rel=0.05)


def test_lift_fades_as_the_team_plays(monkeypatch, tmp_path):
    """Findings 527: the lift fades to 0 at the team's SEASON_SCRATCH_D_GAMES-th game; at 4 team games it is smaller with
    a 5-game fade than with the shipped 20. Kills: E12 (no fade)."""
    full = _lift(monkeypatch, tmp_path)
    monkeypatch.setattr(rating, "SEASON_SCRATCH_D_GAMES", 5)
    assert _lift(monkeypatch, tmp_path) == pytest.approx(full * (1 - 4 / 5) / (1 - 4 / 20), rel=0.05)


def test_no_lift_for_forwards(monkeypatch, tmp_path):
    """Findings 527: forwards in a scratch streak were calibrated; the lift is defensemen only. Kills: E05."""
    assert _lift(monkeypatch, tmp_path, who="Auston Matthews", scratched=(1,), missed={1: {3}}) == pytest.approx(0)
