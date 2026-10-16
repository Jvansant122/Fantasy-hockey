"""Hunt 14 mutate stream: pins for the Season ice-time pull-back (507/512) and goalie start rules 363, 447, 450, 451
(copy into tests/)."""
from datetime import date

import pytest

import rating
from test_add_ratings import espn, FakeSession, TODAY, MONDAY


class LessIce(FakeSession):  # last season's ice time `less` seconds under this season's
    def __init__(self, less=180, **kw):
        super().__init__(**kw)
        self.less = less

    def get(self, url, params=None, **kw):
        r = super().get(url, params, **kw)
        exp = (params or {}).get("cayenneExp", "")
        if "skater/" in url and "seasonId=20252026" in exp and "gameDate" not in exp:
            for row in r.payload["data"]:
                row["timeOnIcePerGame"] -= self.less
        return r


def _toi_chg(monkeypatch, tmp_path, fake):
    monkeypatch.setattr(rating.requests, "Session", lambda: fake)
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    players = [espn(101, "Auston Matthews", "TOR", "C"), espn(103, "David Pastrnak", "BOS", "RW")]
    rating.add_ratings(players, 2027, TODAY, MONDAY, [])
    return players[0]["cr_why"].get("toi_chg")


def test_ice_time_change_is_capped_at_five_minutes(monkeypatch, tmp_path):
    """Handoff 512: the change vs last season counts at most SEASON_TOI_CHG_CAP minutes. Kills: R41."""
    six, nine = _toi_chg(monkeypatch, tmp_path, LessIce(360)), _toi_chg(monkeypatch, tmp_path, LessIce(540))
    assert six is not None and six == nine  # both past the 5-minute cap
    assert abs(_toi_chg(monkeypatch, tmp_path, LessIce(180))) < abs(six)


def test_ice_time_change_only_in_the_first_games(monkeypatch, tmp_path):
    """Handoff 507: only in a skater's first SEASON_TOI_CHG_GAMES games. Kills: R42."""
    assert _toi_chg(monkeypatch, tmp_path, LessIce()) is not None
    monkeypatch.setattr(rating, "SEASON_TOI_CHG_GAMES", 3)  # the fake league's skaters have 4 games
    assert _toi_chg(monkeypatch, tmp_path, LessIce()) is None


def test_ice_time_change_skips_an_opening_game_cut_short(monkeypatch, tmp_path):
    """Handoff 512: an opening game cut short (under half of last season's TOI, so short_opening keeps the rest) is no lost
    ice time, even when it isn't short enough to read as leaving early. Kills: R40."""
    monkeypatch.setattr(rating, "SHORT_OPENING_GAMES", 4)  # the fake league's skaters have 4 games
    # last season 28.5 min a game; this season 18.5, then 13 in the last game: under half of 28.5, but 76% of his average
    assert _toi_chg(monkeypatch, tmp_path, LessIce(-600, short_last={1: 780})) is None
    assert _toi_chg(monkeypatch, tmp_path, LessIce(-600)) is not None  # the same change without the short game counts


def _night(hist, dates, today, confirmed=None, **patch):
    nights = {}
    with pytest.MonkeyPatch.context() as mp:
        for k, v in patch.items():
            mp.setattr(rating, k, v)
        rating.goalie_starts(rating.Model(), "TOR", [1, 2], {"TOR": hist}, {1: 50, 2: 32}, dates, today, confirmed=confirmed,
                             by_date=nights)
    return nights


LOPSIDED = [(f"2026-10-{d:02d}", 1 if i % 4 else 2) for i, d in enumerate(range(1, 21, 2))]


def test_back_to_back_rule_skips_a_night_daily_faceoff_settled():
    """Handoff 447: once DFO confirms the other night of a pair, the no. 1's back-to-back-ahead shift on this night goes.
    Kills: G01, G02."""
    pair, today = ["2026-10-23", "2026-10-24"], date(2026, 10, 22)
    n2_conf = {"2026-10-24": 2}
    assert _night(LOPSIDED, pair, today, n2_conf)[1]["2026-10-23"] == pytest.approx(
        _night(LOPSIDED, pair, today, n2_conf, B2B_AHEAD_NIGHT1=0.0)[1]["2026-10-23"])
    assert _night(LOPSIDED, pair, today)[1]["2026-10-23"] < _night(LOPSIDED, pair, today, B2B_AHEAD_NIGHT1=0.0)[1]["2026-10-23"]
    n1_conf = {"2026-10-23": 1}
    assert _night(LOPSIDED, pair, today, n1_conf)[1]["2026-10-24"] == pytest.approx(
        _night(LOPSIDED, pair, today, n1_conf, B2B_AHEAD_NIGHT2_NEXT=0.0)[1]["2026-10-24"])


def test_no1_ordinary_lift_skips_tonight_when_it_is_night_two():
    """Handoff 363: the no. 1's ordinary-night lift is not on tonight's game when the team played yesterday. Kills: G05."""
    tonight = ["2026-10-20"]  # LOPSIDED's last game is Oct 19
    on, off = _night(LOPSIDED, tonight, date(2026, 10, 20)), _night(LOPSIDED, tonight, date(2026, 10, 20), NO1_ORDINARY_LOGIT=0.0)
    assert on[1]["2026-10-20"] == pytest.approx(off[1]["2026-10-20"])
    later = ["2026-10-22"]
    assert _night(LOPSIDED, later, date(2026, 10, 22))[1]["2026-10-22"] > \
        _night(LOPSIDED, later, date(2026, 10, 22), NO1_ORDINARY_LOGIT=0.0)[1]["2026-10-22"]


def test_no_start_yet_cut_ends_after_the_early_games():
    """Handoff 450: the cut for a goalie with no start yet holds only while the team has under EARLY_SHARE_GAMES games.
    Kills: G03."""
    hist = [(f"2026-10-{d:02d}", 1) for d in range(1, 25, 2)]  # 12 games, all goalie 1
    dates = ["2026-10-25", "2026-10-27"]
    assert _night(hist, dates, date(2026, 10, 25))[2] == pytest.approx(
        _night(hist, dates, date(2026, 10, 25), EARLY_NO_START_LOGIT=0.0)[2])


def test_even_tandem_scale_is_applied():
    """Handoff 451: on an even tandem the back-to-back shifts are EVEN_TANDEM_B2B_SCALE of full size. Kills: R45."""
    even = [(f"2026-10-{d:02d}", 1 if i % 2 else 2) for i, d in enumerate(range(1, 21))]
    pair, today = ["2026-10-23", "2026-10-24"], date(2026, 10, 22)
    zero = dict(B2B_AHEAD_NIGHT1=0.0, B2B_AHEAD_NIGHT2_NEXT=0.0)
    gap = lambda **kw: abs(_night(even, pair, today, **kw)[1]["2026-10-23"] - _night(even, pair, today, **zero, **kw)[1]["2026-10-23"])
    assert 0 < gap() < gap(EVEN_TANDEM_B2B_SCALE=1.0)


def test_back_from_spell_finds_the_latest_gap():
    """Handoff 711: games since his first start back and his share of the 20 starts before his latest 5+ game gap."""
    assert rating.back_from_spell([1] * 12 + [2] * 6 + [1], 1) == (0, 1.0)
    assert rating.back_from_spell([1] * 12 + [2] * 6 + [1, 2], 1) == (1, 1.0)
    assert rating.back_from_spell([1, 2] * 6 + [2] * 5 + [1], 1) == (0, 6 / 11)  # the window ends at his last start before the gap
    assert rating.back_from_spell([1] * 12 + [2] * 4 + [1], 1) is None  # a 4-game gap is no spell
    assert rating.back_from_spell([1] * 12, 1) is None and rating.back_from_spell([2] * 12, 1) is None


def test_no1_back_from_a_spell_gets_the_lift_for_two_games():
    """Handoff 711: a no. 1 back from 5+ team games without a start gets BACK_NO1_LOGIT on every game left while his first
    start back is one of the team's last 2; not once he is 2 games past it, nor for a goalie who was the backup."""
    hist = lambda tail: [(f"2026-09-{d:02d}", g) for d, g in zip(range(1, 31), [1] * 12 + [2] * 6 + tail)]
    dates, today = ["2026-10-20", "2026-10-22"], date(2026, 10, 20)
    for tail in ([1], [1, 2]):
        on, off = _night(hist(tail), dates, today), _night(hist(tail), dates, today, BACK_NO1_LOGIT=0.0)
        assert all(on[1][d] > off[1][d] for d in dates), tail
    late = hist([1, 2, 1])
    assert _night(late, dates, today)[1] == pytest.approx(_night(late, dates, today, BACK_NO1_LOGIT=0.0)[1])
    backup = [(f"2026-09-{d:02d}", g) for d, g in zip(range(1, 31), [2, 2, 2, 1] * 3 + [2] * 6 + [1])]
    assert _night(backup, dates, today)[1] == pytest.approx(_night(backup, dates, today, BACK_NO1_LOGIT=0.0)[1])
