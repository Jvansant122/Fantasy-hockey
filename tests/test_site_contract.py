"""The data the page reads, and the page's own script.

index.html reads data/players.json directly, so a renamed or dropped field breaks the site without any error in Actions."""
import copy
import json
import re
import shutil
import subprocess

import pytest

from conftest import ROOT, reopen_tonight

import check_data


@pytest.fixture(scope="module")
def data():
    return json.loads((ROOT / "data" / "players.json").read_text())


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
    """Every p.<field> in index.html exists on some player (cr_news and pending_drop are optional: only present with news or a drop after the first puck)."""
    html = (ROOT / "index.html").read_text()
    used = set(re.findall(r"\bp\.([a-z_][a-z0-9_]*)", html))
    have = set().union(*(p.keys() for p in data["players"])) | {"cr_news", "pending_drop"}
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


def test_check_rules_for_season_weeks(data):
    """season_weeks and team_nights (handoff 204): both required, empty allowed (a failed week step), and broken ones caught."""
    missing = copy.deepcopy(data)
    del missing["team_nights"]
    assert any("missing top-level fields" in e for e in check_data.problems(missing))
    empty = copy.deepcopy(data)
    empty["season_weeks"], empty["team_nights"] = [], {}
    assert check_data.problems(empty) == []
    w = data["season_weeks"][-1]
    later = {**w, "week": w["week"] + 1, "sps": [sp + 7 for sp in w["sps"]], "games": {str(int(k) + 7): v for k, v in w["games"].items()},
             "light": [sp + 7 for sp in w["light"]]}
    ok = copy.deepcopy(data)
    ok["season_weeks"].append(later)
    ahead = {n["sp"] for n in data["nights"] if not n["past"]}
    team = next(t for t, sps in ok["team_nights"].items() if ahead & set(sps))
    ok["team_nights"][team] = ok["team_nights"][team] + [later["sps"][2]]
    assert check_data.problems(ok) == []
    shifted = copy.deepcopy(ok)
    shifted["season_weeks"][-1]["sps"] = [sp + 1 for sp in later["sps"]]
    assert any(e.startswith("S1") for e in check_data.problems(shifted))
    twice = copy.deepcopy(ok)
    twice["team_nights"][team].append(later["sps"][2])
    assert any(e.startswith("S2") for e in check_data.problems(twice))
    outside = copy.deepcopy(ok)
    outside["team_nights"][team].append(later["sps"][-1] + 30)
    assert any(e.startswith("S2") for e in check_data.problems(outside))
    differ = copy.deepcopy(ok)
    differ["team_nights"][team] = [sp for sp in differ["team_nights"][team] if sp not in ahead]
    assert not check_data.problems(differ) and any(w.startswith("W2") for w in check_data.warnings(differ, None))


def playoff_like(data, start, nights, today, weeks, period):
    """data reshaped to a matchup of `nights` nights from start, run on today: every rostered player plays every other
    night, free agents none (the synthetic in findings 241)."""
    from datetime import timedelta
    x = copy.deepcopy(data)
    sp0, tsp = (start - check_data.SP1).days + 1, (today - check_data.SP1).days + 1
    x["nights"] = [{"sp": sp0 + i, "date": (start + timedelta(days=i)).strftime("%a ") + str((start + timedelta(days=i)).day),
                    "games": 16, "light": False, "past": sp0 + i < tsp} for i in range(nights)]
    x.update(updated_date=today.isoformat(), updated_utc=today.isoformat() + "T10:00:00Z", matchup_weeks=weeks,
             matchup_period=period, season_weeks=[], team_nights={})
    sps = [n["sp"] for n in x["nights"]]
    for p in x["players"]:
        p["nights"] = [] if p["team"] == "FA" else sps[::2]
        p["games"], p["games_left"], p["today_start"] = len(p["nights"]), sum(s >= tsp for s in p["nights"]), None
        w = p.get("cr_why") or {}
        if p["pos"] == "G" and "exp_starts" in w:
            w["exp_starts"], w["team_games"] = min(w["exp_starts"], p["games_left"] / 2), p["games_left"]
            p["cr_games"] = w["exp_starts"]
        else:
            p["cr_games"] = min(p.get("cr_games") or 0, p["games_left"])
        p["cr"] = round((p.get("cr_fpg") or 0) * p["cr_games"], 2)
    return x


def test_check_passes_every_day_of_the_playoffs_and_the_opener(data):
    """Each day of both two-week playoff rounds and the 6-night opener publishes: the N1 date check reads 'Sun 21' as the
    nearest Sunday the 21st, not Feb 21 (findings 241 (1), which blocked all four runs on Mar 8, 2027)."""
    from datetime import date, timedelta
    if not data["rated"]:
        return
    data = reopen_tonight(copy.deepcopy(data))
    for start, nights, weeks, period in ((date(2027, 3, 8), 14, 2, 24), (date(2027, 3, 22), 14, 2, 25), (date(2026, 9, 29), 6, 1, 1)):
        for k in range(nights):
            today = start + timedelta(days=k)
            assert check_data.problems(playoff_like(data, start, nights, today, weeks, period)) == [], today
    day1 = playoff_like(data, date(2027, 3, 8), 14, date(2027, 3, 8), 2, 24)
    assert check_data._night_dates(day1)[day1["nights"][-1]["sp"]] == date(2027, 3, 21)
    for n in day1["nights"]:  # the overnight shift is still caught on day 1
        n["sp"] += 1
    assert any(e.startswith("N1") for e in check_data.problems(day1))


def test_a_player_dropped_after_the_first_puck_raises_no_schedule_warning(data):
    """A pending drop's nights stop at today (findings 241 (3)): W2 doesn't flag his team for it."""
    ahead = {n["sp"] for n in data["nights"] if not n["past"]}
    d = copy.deepcopy(data)
    p = next((p for p in d["players"] if p["team"] in d["team_nights"] and p.get("owner") == "other"
              and len([sp for sp in p.get("nights") or [] if sp in ahead]) >= 2), None)
    if p is None:
        return
    p["nights"] = [sp for sp in p["nights"] if sp <= min(ahead)]
    assert any(w.startswith("W2") for w in check_data.warnings(d, None))
    p["pending_drop"] = True
    assert not any(w.startswith("W2") for w in check_data.warnings(d, None))


def test_check_reads_period_one_from_the_data(data):
    """With sp1_date in the data, the day-shift rule uses it: a wrong one is caught, the real one passes (findings 242)."""
    import copy
    from datetime import date, timedelta
    d = copy.deepcopy(data)
    d["sp1_date"] = check_data.SP1.isoformat()
    assert not [m for m in check_data.problems(d) if m.startswith("N1")]
    d["sp1_date"] = (check_data.SP1 + timedelta(days=1)).isoformat()
    assert [m for m in check_data.problems(d) if m.startswith("N1")]
    d["sp1_date"] = "not a date"
    assert not [m for m in check_data.problems(d) if m.startswith("N1")]
