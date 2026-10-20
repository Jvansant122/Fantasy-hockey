"""Hunt 14 mutate2: goalie start rules of PRs #215 and #231 (handoffs 711, 731, 733) at the edges the research stated
(findings 514, 528). Passes on main af32a42; kills the mutants named in each docstring (hunt14 mutate2 RESULTS.md)."""
from datetime import date, timedelta

import pytest

import rating
from test_add_ratings import espn, FakeSession, TODAY, MONDAY


def _night(hist, dates, today, confirmed=None, **kw):
    nights = {}
    rating.goalie_starts(rating.Model(), "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, dates, today, confirmed=confirmed,
                         by_date=nights, **kw)
    return nights


def _spell(order):
    return [(f"2026-09-{d:02d}" if d <= 30 else f"2026-10-{d - 30:02d}", g) for d, g in zip(range(1, 61), order)]


DATES, DAY = ["2026-10-20", "2026-10-22"], date(2026, 10, 20)


def test_back_lift_at_exactly_half_the_starts(monkeypatch):
    """Findings 514 / handoff 711: the lift is for a goalie with a share of 0.5 or more of the 20 starts before the spell;
    exactly 0.5 qualifies. Kills: C10 (> not >=), C06 (share never reached)."""
    order = [2, 1] * 6 + [2] * 5 + [1]  # 6 of the 12 before the spell, out 5, back last game
    assert rating.back_from_spell(order, 1) == (0, 0.5)
    on = _night(_spell(order), DATES, DAY)
    monkeypatch.setattr(rating, "BACK_NO1_LOGIT", 0.0)
    off = _night(_spell(order), DATES, DAY)
    assert all(on[1][d] > off[1][d] for d in DATES)


def test_back_lift_reads_the_latest_spell(monkeypatch):
    """Handoff 711: his latest 5+ game gap decides; an earlier spell he came back from long ago doesn't hide a fresh one.
    Kills: C13 (earliest spell)."""
    order = [1] * 12 + [2] * 6 + [1] * 4 + [2] * 6 + [1]
    assert rating.back_from_spell(order, 1)[0] == 0
    on = _night(_spell(order), DATES, DAY)
    monkeypatch.setattr(rating, "BACK_NO1_LOGIT", 0.0)
    assert all(on[1][d] > _night(_spell(order), DATES, DAY)[1][d] for d in DATES)


NEXT = ["2026-10-14", "2026-10-16"]


def test_summer_mover_cut_is_next_game_only():
    """Findings 528 (3) / handoff 731: the cut is on the team's next game only. With the next game confirmed for the no. 1,
    the game after is the same for a mover and a stayer. Kills: C20 (cut every game)."""
    hist = [("2026-10-08", 1), ("2026-10-10", 1), ("2026-10-12", 2)]
    conf = {"2026-10-14": 1}
    mover, stayer = _night(hist, NEXT, date(2026, 10, 14), conf, off_movers={2}), _night(hist, NEXT, date(2026, 10, 14), conf)
    assert mover[2]["2026-10-16"] == pytest.approx(stayer[2]["2026-10-16"])


def test_summer_mover_cut_through_the_teams_16th_game():
    """Findings 528 (3): measured while the team has played 16 or fewer: the 16th still counts, the 17th doesn't.
    Kills: C21 (< not <=)."""
    hist = lambda n: [(str(date(2026, 9, 1) + timedelta(days=2 * i)), 1 if i % 3 else 2) for i in range(n)]
    nxt = lambda n, mv: _night(hist(n), ["2026-10-14"], date(2026, 10, 14), off_movers=mv)[2]["2026-10-14"]
    assert nxt(16, {2}) < nxt(16, set())
    assert nxt(17, {2}) == pytest.approx(nxt(17, set()))


class NoStarts(FakeSession):
    """Last season: Stolarz (12) started for BOS, Swayman (13) was with TOR but started no games."""
    def get(self, url, params=None, **kw):
        r = super().get(url, params, **kw)
        exp = (params or {}).get("cayenneExp", "")
        if "goalie/" in url and "seasonId=20252026" in exp and "gameDate" not in exp and "gamesStarted=0" not in exp:
            for row in r.payload["data"]:
                if row["playerId"] == 12:
                    row["teamAbbrevs"] = "BOS"
                if row["playerId"] == 13:
                    row.update(gamesStarted=0, teamAbbrevs="TOR")
        return r


def test_summer_movers_are_goalies_who_started_for_another_team(monkeypatch, tmp_path):
    """Handoff 731: off_movers = goalies with starts last season whose last team then wasn't this one: a goalie who stayed
    isn't one, nor is one with no starts last season. Kills: C22 (stayers), C23 (nobody), C24 (no starts)."""
    seen, real = {}, rating.goalie_starts
    monkeypatch.setattr(rating, "goalie_starts", lambda model, team, *a, **kw: seen.__setitem__(team, set(a[-1])) or real(model, team, *a, **kw))
    monkeypatch.setattr(rating.requests, "Session", lambda: NoStarts())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    players = [espn(111, "Joseph Woll", "TOR", "G"), espn(112, "Anthony Stolarz", "TOR", "G"), espn(113, "Jeremy Swayman", "BOS", "G")]
    rating.add_ratings(players, 2027, TODAY, MONDAY, [])
    assert seen["TOR"] & {11, 12} == {12} and 13 not in seen["BOS"]


def test_opening_starter_lift_next_game_in_the_first_two_only(monkeypatch):
    """Findings 528 (4) / handoff 733: the opening-night starter's lift is on the team's next game while it has played at
    most 2: not after 3 games, and not on the game after a confirmed next game. Kills: C28 (any number of games),
    C30 (every game)."""
    def nights(hist, conf=None, lift=0.3):
        with monkeypatch.context() as m:
            m.setattr(rating, "OPENING_STARTER_LOGIT", lift)
            return _night(hist, NEXT, date(2026, 10, 14), conf)
    two = [("2026-10-10", 1), ("2026-10-12", 2)]
    assert nights(two)[1]["2026-10-14"] > nights(two, lift=0.0)[1]["2026-10-14"]  # the rule is on
    three = [("2026-10-08", 1), ("2026-10-10", 2), ("2026-10-12", 2)]
    assert nights(three)[1] == pytest.approx(nights(three, lift=0.0)[1])
    conf = {"2026-10-14": 2}
    assert nights(two, conf)[1]["2026-10-16"] == pytest.approx(nights(two, conf, lift=0.0)[1]["2026-10-16"])
