"""Hunt 14 mutate stream: pins for injury caps the suite leaves loose (copy into tests/)."""
import pytest

import rating
from test_add_ratings import espn, FakeSession, TODAY, MONDAY


def _rate(monkeypatch, tmp_path, players, fake=None, **kw):
    fake = fake or FakeSession()
    monkeypatch.setattr(rating.requests, "Session", lambda: fake)
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    rating.add_ratings(players, 2027, TODAY, MONDAY, [], **kw)
    return {p["name"]: p for p in players}


def test_out_skater_practising_since_his_last_game_takes_the_day_to_day_cap(monkeypatch, tmp_path):
    """Handoff 406: an OUT skater whom Daily Faceoff reports practising in full since his last game is capped like
    day-to-day (0.5), not OUT (0.35). Kills: R28."""
    news = {"recent": {"3": [{"ts": "2026-10-06T15:00:00", "cat": "inj", "f": ["full"]}]},
            "fetched": rating.utcnow().strftime("%Y-%m-%dT%H:%MZ")}
    p = _rate(monkeypatch, tmp_path, [espn(103, "David Pastrnak", "BOS", "RW", injury="OUT"), espn(101, "Auston Matthews", "TOR", "C")],
              FakeSession(missed={3: {3}}), dfo_news=news)["David Pastrnak"]
    assert p["cr_why"]["week_news"]["why"][0] == "practising"
    assert rating.SKATER_INJURY_CAP["OUT"] < p["cr_dress_next"] <= rating.SKATER_INJURY_CAP["DAY_TO_DAY"] + 1e-9


def test_day_to_day_goalie_keeps_part_of_his_starts(monkeypatch, tmp_path):
    """A day-to-day goalie starts with INJURY_DRESS['DAY_TO_DAY'] (0.5) of his chance, an OUT one never.
    Kills: zero:INJURY_DRESS, R39 (DTD goalie at 1.0)."""
    def woll(injury):
        players = [espn(111, "Joseph Woll", "TOR", "G", injury=injury), espn(112, "Anthony Stolarz", "TOR", "G")]
        return _rate(monkeypatch, tmp_path, players)["Joseph Woll"]
    healthy, dtd, out = woll(None), woll("DAY_TO_DAY"), woll("OUT")
    assert 0 < rating.INJURY_DRESS["DAY_TO_DAY"] < 1
    assert out["cr_games"] == 0 < dtd["cr_games"] < healthy["cr_games"]


def test_stated_return_cut_only_inside_a_week(monkeypatch, tmp_path):
    """Handoff 670: the games-before-return cut is for a stated return 0-7 days away; a return 9-10 days away is left
    alone (findings 496 measured 0-7 only). Pins the window and the cut's sign. Kills: N13, N14."""
    from test_add_ratings import rate
    assert rating.BEFORE_RETURN_LOGIT < 0

    def pasta(lift, short):
        monkeypatch.setattr(rating, "BEFORE_RETURN_LOGIT", lift)
        inj = [{"athlete_id": 103, "name": "David Pastrnak", "short": short, "long": "", "date": "2026-10-08T14:00Z"}]
        return rate(monkeypatch, tmp_path, [espn(103, "David Pastrnak", "BOS", "RW", injury="DAY_TO_DAY")], injuries=inj)["David Pastrnak"]
    near, far = "Pastrnak is expected to miss 3-5 days.", "Pastrnak is expected to miss 9-10 days."
    assert pasta(-0.6, near)["cr_games"] < pasta(0.0, near)["cr_games"]
    assert pasta(-0.6, far)["cr_games"] == pasta(0.0, far)["cr_games"]
