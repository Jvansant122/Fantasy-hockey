"""Hunt 14 mutate stream: pins for the update slot gate, matchup length and Daily Faceoff wording rules (copy into tests/)."""
from datetime import datetime, timezone

import fetch
import leads
import slot_gate


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_slot_gate_names_every_slot_and_allows_an_early_landing():
    """Each of the four ET slots is its own (12:40, not 12:00), and data landing up to 10 minutes before a slot counts
    for it (a dispatched run lands early). Kills: S01, S02."""
    assert [slot_gate.latest_slot(utc(2026, 10, 15, h, m)).strftime("%H:%M") for h, m in ((10, 20), (13, 20), (16, 45), (21, 15))] \
        == ["06:10", "09:10", "12:40", "17:10"]
    assert slot_gate.latest_slot(utc(2026, 10, 15, 16, 30)).strftime("%H:%M") == "09:10"  # 12:30 ET: before the 12:40 slot
    assert slot_gate.should_skip("2026-10-15T10:05:00Z", utc(2026, 10, 15, 10, 41)) is True  # 6:05 ET for the 6:10 slot
    assert slot_gate.should_skip("2026-10-15T09:55:00Z", utc(2026, 10, 15, 10, 41)) is False  # 5:55 ET: too early


def test_next_matchup_after_the_preseason_is_the_six_day_opener():
    """The opener (week 1) is 6 days. Kills: F06."""
    assert fetch.next_matchup_days({"1": [1], "2": [2]}, 0) == 6


def test_season_wording_with_a_return_before_it_is_not_season_ending():
    """Handoff 410: a return word in the 40 characters before a season phrase vetoes it. Kills: L03."""
    f = lambda t: leads.news_flags({"details": t})
    assert not f("He should be good to go for the playoffs.")["season"]
    assert f("He is out for the playoffs.")["season"]


def test_stat_line_in_weeks_is_no_absence():
    """Handoff 428: 'over the past three weeks' is a stat line; only game counts had their own check. Kills: L07."""
    assert leads.news_timeline("He has 10 points over the past three weeks.") is None
    assert leads.news_timeline("He is expected to miss three weeks.") == 21


def test_lineup_posted_at_puck_drop_is_not_older_than_the_game():
    """leads.older: a lineup updated exactly at the start time is not older (strictly before). Kills: L10."""
    assert leads.older("2026-10-14T23:00:00Z", "2026-10-14T23:00:00Z") is False
    assert leads.older("2026-10-14T22:59:00Z", "2026-10-14T23:00:00Z") is True
