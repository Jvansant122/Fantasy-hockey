"""Hunt 14 mutate2: Daily Faceoff tonight cap (handoffs 696/710) and next-game floor (721) pinned to the population they were
measured on (findings 509 (5), 521). Each test passes on main af32a42 and kills the mutants named in its docstring
(hunt14 mutate2 RESULTS.md)."""
from datetime import datetime, timezone

import pytest

import rating
from test_add_ratings import espn, rate, FakeSession

TONIGHT = "2026-10-09T23:00:00Z"


def _one(monkeypatch, tmp_path, name, p, fake=None):
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 9, 16, tzinfo=timezone.utc))
    return rate(monkeypatch, tmp_path, [p], fake=fake)[name]


def test_tonight_cap_is_near_the_measured_rate(monkeypatch, tmp_path):
    """Findings 509 (5): regulars left out of tonight's fresh lines dressed 4 of 45 (0.09), so the cap sits near that rate,
    above 0, and a capped regular keeps a small chance tonight. Kills: A01 (cap zeroed)."""
    assert abs(rating.DFO_ABSENT_TODAY_CAP - 4 / 45) < 0.1 and rating.DFO_ABSENT_TODAY_CAP > 0.02
    p = dict(espn(101, "Auston Matthews", "TOR", "C"), today_start=TONIGHT, dfo_out=True)
    assert _one(monkeypatch, tmp_path, "Auston Matthews", p)["cr_dress_next"] > 0.02


@pytest.mark.parametrize("who,kw,fake", [
    # a healthy scratch last game: the rule was measured on players who dressed it. Kills A05
    ("Charlie McAvoy", dict(), None),
    # ESPN lists him day-to-day: the measured group had no ESPN status. Kills A06
    ("Auston Matthews", dict(injury="DAY_TO_DAY"), None),
])
def test_tonight_cap_only_for_regulars_with_no_status(monkeypatch, tmp_path, who, kw, fake):
    """Handoffs 696/710: the tonight cap is for regulars (dressed the team's last game, no ESPN status). Anyone else left out
    of tonight's lines keeps the chance he has with no lines read."""
    team, pid, pos = {"Charlie McAvoy": ("BOS", 104, "D"),
                      "Auston Matthews": ("TOR", 101, "C")}[who]
    out = _one(monkeypatch, tmp_path, who, dict(espn(pid, who, team, pos, **kw), today_start=TONIGHT, dfo_out=True), fake)
    none = _one(monkeypatch, tmp_path, who, dict(espn(pid, who, team, pos, **kw), today_start=TONIGHT, dfo_out=None), fake)
    assert none["cr_dress_next"] > rating.DFO_ABSENT_TODAY_CAP  # the cap would bind
    assert (out["cr_dress_next"], out["cr_games"]) == (none["cr_dress_next"], none["cr_games"])


def test_fresh_lines_floor_not_for_espn_listed(monkeypatch, tmp_path):
    """Handoff 721 / findings 521: the 0.95 next-game floor was measured on regulars with no ESPN status; a day-to-day
    regular in the lines keeps his own chance under the injury cap. Kills: A14."""
    monkeypatch.setattr(rating.Model, "dress_logit", lambda self, f: 0.5)  # a chance under his day-to-day cap
    monkeypatch.setattr(rating, "BOOST_WEEK_W", 0.0)
    p = lambda dfo: dict(espn(101, "Auston Matthews", "TOR", "C", injury="DAY_TO_DAY"), today_start=TONIGHT, dfo_out=dfo)
    inl, none = _one(monkeypatch, tmp_path, "Auston Matthews", p(False)), _one(monkeypatch, tmp_path, "Auston Matthews", p(None))
    assert none["cr_dress_next"] < rating.DTD_PLAYED_CAPS[-1][1] - 0.05  # the floor would bind
    assert (inl["cr_dress_next"], inl["cr_games"]) == (none["cr_dress_next"], none["cr_games"])


def test_free_agent_left_out_tonight_counts_capped_in_tonights_sums(monkeypatch, tmp_path):
    """Handoffs 680/696: after the night's first puck a free agent's game tonight is no longer his, but it still fills his
    team's lineup tonight; left out of tonight's lines it counts at most the tonight cap there, both its chance and the cap
    any later shift is held to. Kills: A07 (chance uncapped), A09 (cap uncapped)."""
    starts = {"2026-10-09": "2026-10-09T23:30:00Z"}  # BOS tonight at 7:30 PM ET; another game started at 7
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 9, 23, 10, tzinfo=timezone.utc))
    seen, real = [], rating.week_slack
    monkeypatch.setattr(rating, "week_slack", lambda rows, *a, **kw: seen.extend(rows) or real(rows, *a, **kw))

    def bolduc(dfo):
        seen.clear()
        p = dict(espn(108, "Zack Bolduc", "BOS", "LW", games_left=1), owner="freeagent", today_start="2026-10-09T23:00:00Z",
                 dfo_out=dfo)
        rate(monkeypatch, tmp_path, [p, espn(103, "David Pastrnak", "BOS", "RW")], fake=FakeSession(starts=starts))
        return next(r for r in seen if r["p"] is p)
    out, none = bolduc(True), bolduc(None)
    tonight = lambda r: [o for o in r["others"] if o[0] == "2026-10-09"]
    assert tonight(none) and tonight(none)[0][1] > rating.DFO_ABSENT_TODAY_CAP
    (_, q, c, _, _), = tonight(out)
    assert q <= rating.DFO_ABSENT_TODAY_CAP + 1e-9 and c <= rating.DFO_ABSENT_TODAY_CAP + 1e-9


def _cut(old_next, dress=0.9, n=3, cut=0.12, floor=None):
    p = {"cr_dress": dress, "cr_dress_next": old_next, "games_left": n, "cr_fpg": 2.0,
         "cr_games": round(n * dress, 2), "cr": 0.0}
    rating.cut_next(p, cut, floor)
    return p


def test_lineup_cut_keeps_the_floor_only_on_the_next_game():
    """Handoff 722: the returning-regular / newcomer cut takes `cut` off every game, but the team's next game keeps Daily
    Faceoff's floor, never above his own chance before the cut; the week's games move by exactly what that game keeps.
    Kills: A24 (floor above his own chance), A25 (games ignore the kept next game)."""
    plain, kept = _cut(0.95), _cut(0.95, floor=rating.DFO_IN_LINES_NEXT)
    assert plain["cr_dress_next"] == pytest.approx(0.95 - 0.12)
    assert kept["cr_dress_next"] == pytest.approx(0.95)
    assert kept["cr_games"] - plain["cr_games"] == pytest.approx(kept["cr_dress_next"] - plain["cr_dress_next"], abs=0.011)
    low = _cut(0.6, dress=0.6, floor=rating.DFO_IN_LINES_NEXT)  # his chance was under the floor: the floor can't lift it
    assert low["cr_dress_next"] <= 0.6


def test_floor_after_cuts_only_for_the_teams_next_game(monkeypatch, tmp_path):
    """Handoffs 721-722: after the night's first puck a free agent's first counted game is a later one (j0 > 0), which the
    fresh-lines floor doesn't cover; a lineup cut takes it down whatever tonight's lines say. Kills: A26."""
    starts = {"2026-10-09": "2026-10-09T23:30:00Z"}
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 9, 23, 10, tzinfo=timezone.utc))
    monkeypatch.setattr(rating, "displaced_skaters", lambda *a, **k: {7: (0.12, "R")})

    def bolduc(dfo):
        p = dict(espn(108, "Zack Bolduc", "BOS", "LW", games_left=1), owner="freeagent", today_start="2026-10-09T23:00:00Z",
                 dfo_out=dfo)
        return rate(monkeypatch, tmp_path, [p, espn(103, "David Pastrnak", "BOS", "RW")], fake=FakeSession(starts=starts))["Zack Bolduc"]
    inl, none = bolduc(False), bolduc(None)
    assert "displaced" in inl["cr_why"] and none["cr_dress_next"] < rating.DFO_IN_LINES_NEXT
    assert inl["cr_dress_next"] == none["cr_dress_next"]
