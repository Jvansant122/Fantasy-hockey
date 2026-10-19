"""Hunt 14 mutate2: return-from-absence rules of PRs #216-#220 (handoffs 712-718) at the edges findings 515 measured:
forwards back 1-3 games from 5+ missed team games, with games before the absence. Passes on main af32a42; kills the mutants
named in each docstring (hunt14 mutate2 RESULTS.md)."""
from datetime import datetime, timezone

import pytest

import rating
from test_add_ratings import espn, rate, FakeSession


def _player(monkeypatch, tmp_path, walk, who=("Auston Matthews", 101, "TOR", "C"), injury=None, fake=None):
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 9, 16, tzinfo=timezone.utc))
    if walk is not None:
        monkeypatch.setattr(rating, "back_gap", lambda tg, played: walk)
    name, pid, team, pos = who
    return rate(monkeypatch, tmp_path, [espn(pid, name, team, pos, injury=injury)], fake=fake)[name]


# (games back, team games missed before them, index where the run starts) -> is he a returner (findings 515)
RETURNERS = [((1, 5, 1), True), ((3, 6, 2), True), ((1, 4, 1), False), ((4, 6, 1), False), ((0, 6, 1), False),
             ((2, 6, 0), False)]  # the last: no games for the team before the absence (a late start, not a return)


@pytest.mark.parametrize("walk,back", RETURNERS)
def test_forward_return_cut_by_games_back_and_gap(monkeypatch, tmp_path, walk, back):
    """Handoff 712: the Week pts/game cut is for forwards 1-3 games back from 5+ missed, after games for the team.
    Kills: D03 (gap 0), D04 (0 games back), D05 (30 games back), D09 (r_k gate), D10 (gap > 5), D11 (back < 3)."""
    plain = _player(monkeypatch, tmp_path, (0, 0, 4))
    p = _player(monkeypatch, tmp_path, walk)
    assert (p["cr_fpg"] < plain["cr_fpg"] - 0.005) == back, walk


def test_return_cut_is_for_forwards_on_their_own_team(monkeypatch, tmp_path):
    """Handoff 712: defensemen were worse with the same cut, so they keep their pts/game; a traded skater's walk isn't his
    team's (715). Kills: D06 (D too), D07 (traded too)."""
    rielly = lambda walk: _player(monkeypatch, tmp_path, walk, who=("Morgan Rielly", 102, "TOR", "D"),
                                  fake=FakeSession(missed={2: set()}))
    assert rielly((2, 6, 1))["cr_fpg"] == rielly((0, 0, 4))["cr_fpg"]
    traded = lambda walk: _player(monkeypatch, tmp_path, walk, who=("Auston Matthews", 101, "BOS", "C"))  # ESPN has him on BOS
    assert traded((2, 6, 1))["cr_fpg"] == traded((0, 0, 4))["cr_fpg"]


@pytest.mark.parametrize("walk,back", RETURNERS)
def test_ir_listed_returner_by_games_back_and_gap(monkeypatch, tmp_path, walk, back):
    """Handoff 714: an IR-listed skater already back 1-3 games from 5+ missed, on full minutes, takes IR_RETURN_CAP; anyone
    else IR-listed keeps the IR caps. Kills: D29 (any games back), D30 (any gap)."""
    p = _player(monkeypatch, tmp_path, walk, injury="INJURY_RESERVE")
    assert (p["cr_dress_next"] > rating.IR_DAY_CAPS[2] + 0.1) == back, walk


def test_ir_returner_needs_full_minutes(monkeypatch, tmp_path):
    """Findings 515: IR-listed returners on full minutes dressed 0.80; the rule asks for IR_RETURN_SHARE (0.8) of his
    average, so a short last game keeps the IR caps. Kills: D25 (share zeroed)."""
    assert 0.5 <= rating.IR_RETURN_SHARE <= 1 and rating.IR_DAY_CAPS[2] < rating.IR_RETURN_CAP <= 0.95
    short = _player(monkeypatch, tmp_path, (2, 6, 1), injury="INJURY_RESERVE", fake=FakeSession(short_last={1: 400}))
    assert short["cr_dress_next"] <= rating.IR_DAY_CAPS[2] + 1e-9


def test_back_gap_needs_games_before_the_absence():
    """Handoffs 712-714: a skater whose first games for the team came late in its schedule has no games before the 'gap':
    back_gap returns k = 0, which every rule reads as no return. Kills: D39."""
    tg = list(range(20))
    assert rating.back_gap(tg, {18, 19}) == (2, 18, 0)
    assert rating.back_gap(tg, {0, 18, 19}) == (2, 17, 1)


def _g(pid, pos, games, team="TOR"):
    return [{"id": pid, "team": team, "pos": pos, "game": x} for x in games]


def _fill_case(before_reg, gap, back):
    """TOR's 20 games. The regular (C) played `before_reg` of the 10 games before a `gap`-game absence and the last `back`;
    the fill-in (L) played all of the absence and 1 of the 10 before."""
    tg = list(range(20))
    start = 20 - back - gap
    before = list(range(start - 10, start))
    reg = before[10 - before_reg:] + list(range(20 - back, 20))
    run = list(range(start, start + gap))
    fill = [before[0]] + run + list(range(20 - back, 20))
    return rating.fillins_back({1: _g(1, "C", reg), 2: _g(2, "L", fill)}, {"TOR": tg})


# (regular's games of the 10 before, team games he missed, games back) -> his fill-in sits more (findings 515)
FILLINS = [((8, 5, 1), True), ((10, 6, 3), True), ((7, 6, 1), False), ((8, 4, 1), False), ((8, 6, 4), False)]


@pytest.mark.parametrize("case,fills", FILLINS)
def test_fill_ins_by_the_regulars_absence(case, fills):
    """Handoff 713: fill-ins of a regular (8+ of the 10 team games before) now 1-3 games back from 5+ straight missed.
    Kills: D14 (regular at 5 of 10), D19 (back 4+), D20 (gap 1+)."""
    assert (_fill_case(*case) == {2}) == fills, case
