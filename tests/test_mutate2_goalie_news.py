"""Hunt 14 mutate2: goalie Daily Faceoff injury news (handoffs 695, 698, 699). Passes on main af32a42; kills the mutants named
in each docstring (hunt14 mutate2 RESULTS.md)."""
from datetime import timedelta

import pytest

import rating
from test_add_ratings import espn, FakeSession, TODAY, MONDAY


def _toronto(monkeypatch, tmp_path, items, starters=(), woll=None, stolarz=None):
    monkeypatch.setattr(rating.requests, "Session", lambda: FakeSession())
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    (tmp_path / "injury_log.jsonl").unlink(missing_ok=True)
    monkeypatch.setattr(rating, "load_news", lambda: {})
    news = {"fetched": "2099-01-01T00:00Z", "recent": {"11": items}}
    players = [espn(111, "Joseph Woll", "TOR", "G", injury=woll), espn(112, "Anthony Stolarz", "TOR", "G", injury=stolarz)]
    rating.add_ratings(players, 2027, TODAY, MONDAY, list(starters), dfo_news=news)
    return {p["name"]: p for p in players}


def _conf(*days):
    return [{"date": str(d), "games": [{"team": "TOR", "goalie": "Joseph Woll", "status": "Confirmed"}]} for d in days]


def test_latest_confirmed_start_sets_when_news_counts(monkeypatch, tmp_path):
    """Handoff 698: with starts confirmed tonight and tomorrow, news posted between them (before tomorrow's 20:00 UTC) is
    news before his confirmed start, so it cuts nothing. Kills: B02 (earliest date used)."""
    news = [{"ts": f"{TODAY}T22:00:00", "cat": "inj", "f": []}]
    both = _conf(TODAY, TODAY + timedelta(days=1))
    cut = _toronto(monkeypatch, tmp_path, news, starters=both)["Joseph Woll"]
    assert "dfo_inj" not in cut["cr_why"]
    assert cut["cr_games"] == pytest.approx(_toronto(monkeypatch, tmp_path, [], starters=both)["Joseph Woll"]["cr_games"])


@pytest.mark.parametrize("woll,stolarz", [("OUT", None), (None, "OUT")])
def test_news_reason_only_when_the_news_moved_his_starts(monkeypatch, tmp_path, woll, stolarz):
    """Handoffs 695/699: his details name the news cut only when it moved his starts: not when ESPN already has him out
    (his starts are 0 either way), nor when his only partner is ESPN-out (no healthy partner takes them). Kills: B05, B07."""
    news = [{"ts": f"{TODAY}T01:00:00", "cat": "inj", "f": []}]
    assert "dfo_inj" in _toronto(monkeypatch, tmp_path, news)["Joseph Woll"]["cr_why"]  # the plain case names it
    assert "dfo_inj" not in _toronto(monkeypatch, tmp_path, news, woll=woll, stolarz=stolarz)["Joseph Woll"]["cr_why"]
