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


@pytest.fixture(scope="module")
def data():
    return json.loads((ROOT / "data" / "players.json").read_text())


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
    assert any("Monday to Sunday" in e for e in check_data.problems(shifted))
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
    assert check_data.warnings(data, data) == [w for w in check_data.warnings(data, None)]
