"""Daily Faceoff page parsing in leads.py, on a canned page."""
import json

import pytest

import leads


class Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, timeout=None, **kw):
        for key, props in self.pages.items():
            if key in url:
                body = json.dumps({"props": {"pageProps": props}})
                return Resp(f'<html><script id="__NEXT_DATA__" type="application/json">{body}</script></html>')
        return Resp("<html>nothing here</html>")


@pytest.fixture(autouse=True)
def clear_teams():
    leads.NHL_TEAM.clear()
    yield
    leads.NHL_TEAM.clear()


def test_fetch_starters_maps_teams_and_both_sides():
    s = FakeSession({
        "line-combinations": {"sortedTeams": [{"name": "Toronto Maple Leafs", "shortName": "TOR"},
                                              {"name": "Nashville Predators", "shortName": "NAS"}]},
        "starting-goalies": {"date": "2026-10-09", "data": [{
            "homeTeamName": "Toronto Maple Leafs", "awayTeamName": "Nashville Predators",
            "homeGoalieName": "Anthony Stolarz", "awayGoalieName": "Juuse Saros",
            "homeNewsStrengthName": "Confirmed", "awayNewsStrengthName": "Likely", "dateGmt": "2026-10-09T23:00:00Z"}]},
    })
    from datetime import date
    out = leads.fetch_starters(s, date(2026, 10, 9))
    assert out["date"] == "2026-10-09"
    by_team = {g["team"]: g for g in out["games"]}
    assert by_team["TOR"]["goalie"] == "Anthony Stolarz" and by_team["TOR"]["status"] == "Confirmed" and by_team["TOR"]["home"]
    assert by_team["NSH"]["opp"] == "TOR" and by_team["NSH"]["status"] == "Likely"  # Daily Faceoff's NAS is the NHL's NSH


def test_page_without_data_raises():
    with pytest.raises(ValueError):
        leads.page_data(FakeSession({}), "https://example.com/x")
