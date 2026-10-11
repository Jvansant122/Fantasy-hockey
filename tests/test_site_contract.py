"""The data the page reads, and the page's own script.

index.html reads data/players.json directly, so a renamed or dropped field breaks the site without any error in Actions."""
import copy
import json
import re
import shutil
import subprocess

import pytest

from conftest import ROOT

import check_data


# fields the code now writes that the committed data/players.json predates: filled in here until the next data run
UNTIL_NEXT_DATA_RUN = {"cr_g_nights": lambda p: {} if p["pos"] == "G" else None}


@pytest.fixture(scope="module")
def data():
    d = json.loads((ROOT / "data" / "players.json").read_text())
    if d.get("rated"):
        for field, fill in UNTIL_NEXT_DATA_RUN.items():  # until the next data run
            if not any(field in p for p in d["players"]):
                for p in d["players"]:
                    if fill(p) is not None:
                        p[field] = fill(p)
    return d


def test_goalies_carry_the_goalie_fields(data):
    """Every goalie has check_data's goalie-only fields; a goalie without them fails the publish check."""
    if not data["rated"]:
        pytest.skip("unrated data")
    assert all(check_data.RATED_GOALIE_FIELDS <= set(p) for p in data["players"] if p["pos"] == "G")
    broken = copy.deepcopy(data)
    del next(p for p in broken["players"] if p["pos"] == "G")["cr_g_nights"]
    assert any("missing fields" in e for e in check_data.problems(broken))


def test_committed_data_passes_the_publish_check(data):
    assert check_data.problems(data) == []


def test_check_catches_broken_data(data):
    broken = json.loads(json.dumps(data))
    del broken["players"][0]["games_left"]
    broken["nights"] = broken["nights"][:6]
    errs = check_data.problems(broken)
    assert any("missing fields" in e for e in errs) and any("nights" in e for e in errs)
    assert check_data.problems({"players": []}) and check_data.problems({**data, "players": data["players"][:5]})


def test_page_only_reads_fields_the_data_has(data):
    """Every p.<field> in index.html exists on some player (cr_news is optional and only present with news)."""
    html = (ROOT / "index.html").read_text()
    used = set(re.findall(r"\bp\.([a-z_][a-z0-9_]*)", html))
    have = set().union(*(p.keys() for p in data["players"])) | {"cr_news"}
    have |= set(UNTIL_NEXT_DATA_RUN)  # until the next data run: the page reads them with ?. and a fallback
    assert not used - have, used - have


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_script_parses(tmp_path):
    html = (ROOT / "index.html").read_text()
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts
    js = tmp_path / "page.mjs" if any("import " in s for s in scripts) else tmp_path / "page.js"
    js.write_text("\n;\n".join(scripts))
    out = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_check_catches_a_shifted_week(data):
    """An overnight run that mixed the clock's date with ESPN's period labelled the week a day off (findings 180)."""
    shifted = json.loads(json.dumps(data))
    for n, label in zip(shifted["nights"], ["Tue 6", "Wed 7", "Thu 8", "Fri 9", "Sat 10", "Sun 11", "Mon 12"]):
        n["date"] = label
    assert any("to a Sunday" in e for e in check_data.problems(shifted))
    wrong_past = json.loads(json.dumps(data))
    for n in wrong_past["nights"]:
        n["past"] = True
    assert any("past nights" in e for e in check_data.problems(wrong_past))


def test_data_rules_catch_past_bugs(data):
    """The publish-time rules (findings 215) pass on today's data and catch each kind of bug they were built from."""
    assert check_data.rules(data) == []
    if not data["rated"]:
        return
    first = lambda pred: next(p for p in data["players"] if pred(p))
    made_up = copy.deepcopy(data)  # a player with no games left given one (Andrae, findings 182)
    p = first(lambda p: p["games_left"] == 0 and p["pos"] != "G")
    p = next(q for q in made_up["players"] if q["id"] == p["id"])
    p["cr_games"], p["cr"] = 0.9, round(0.9 * (p["cr_fpg"] or 0), 2)
    assert any(e.startswith("P3") for e in check_data.problems(made_up))
    shifted = copy.deepcopy(data)  # each night labelled a day late (the overnight shift, findings 180)
    for n in shifted["nights"]:
        n["sp"] += 1
    assert any(e.startswith("N1") for e in check_data.problems(shifted))
    assert any(e.startswith("X1") for e in check_data.problems_vs_prev(
        {**data, "nights": [dict(n, date=m["date"]) for n, m in zip(data["nights"], data["nights"][1:] + data["nights"][:1])]}, data))
    # a model change moving players who haven't played: a warning, not a failure
    moved = copy.deepcopy(data)
    for p in moved["players"]:
        if p["pos"] != "G" and p.get("cr_fpg") is not None:
            p["cr_fpg"] = round(p["cr_fpg"] + 0.1, 2)
    assert any(w.startswith("X3") for w in check_data.warnings(moved, data))
    # a team's goalies all at Season 0: a week with no games, or an unread roster (findings 230)
    no_season = copy.deepcopy(data)
    team = next(p["team"] for p in no_season["players"] if p["pos"] == "G" and p["team"] != "FA" and p.get("cr_matched") and not p.get("injury"))
    for p in no_season["players"]:
        if p["pos"] == "G" and p["team"] == team:
            p["cr_season"] = 0
    assert any(e.startswith("G3") for e in check_data.problems(no_season))
    for p in no_season["players"]:  # ... unless they are all hurt
        if p["pos"] == "G" and p["team"] == team:
            p["injury"] = "INJURY_RESERVE"
    assert not any(e.startswith("G3") for e in check_data.problems(no_season))
    assert check_data.warnings(data, data) == [w for w in check_data.warnings(data, None)]


def test_check_allows_playoff_rounds_and_empty_weeks(data):
    """14 nights in a two-week playoff round, and a week with no games left, publish (findings 230, 232)."""
    from datetime import date, timedelta
    d = copy.deepcopy(data)
    first = date.fromisoformat(d["updated_date"]) - timedelta(days=date.fromisoformat(d["updated_date"]).weekday())
    sp0 = d["nights"][0]["sp"]
    d["matchup_weeks"] = 2
    d["nights"] = [{"sp": sp0 + i, "date": (first + timedelta(days=i)).strftime("%a ") + str((first + timedelta(days=i)).day),
                    "games": 8, "light": False, "past": first + timedelta(days=i) < date.fromisoformat(d["updated_date"])}
                   for i in range(14)]
    errs = [e for e in check_data.problems(d) if not e.startswith(("N", "P2", "P3"))]  # players' nights still the 7-night week
    assert not any("nights" in e for e in errs), errs
    d["nights"][3]["past"] = not d["nights"][3]["past"]
    assert any("past nights" in e for e in check_data.problems(d))
    over = copy.deepcopy(data)  # Sunday night after the last game, or the Olympic break: every Week rating 0
    for p in over["players"]:
        p["games_left"], p["cr"], p["cr_games"] = 0, 0, 0
    assert not any("every Claude Rating is 0" in e for e in check_data.problems(over))


def test_check_blocks_a_run_with_no_free_agents_or_injuries(data):
    """A run that lost the free-agent list or the injury feed passes every other rule (findings 230)."""
    no_fa = copy.deepcopy(data)
    no_fa["players"] = [p for p in no_fa["players"] if p["owner"] not in ("freeagent", "waivers")]
    assert any("free agents" in e for e in check_data.problems(no_fa))
    no_inj = copy.deepcopy(data)
    for p in no_inj["players"]:
        p["injury"] = None
    assert any("injury status" in e for e in check_data.problems(no_inj))
