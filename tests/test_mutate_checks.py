"""Hunt 14 mutate stream: every publish rule in check_data.rules fires on a version broken its way (copy into tests/).
Dropping P2, P4, S1, S2, P6, G1, N1 (consecutive), N2, G2, week S1 (contents) or X2 passed every test on 4940e32."""
import copy
import json

import pytest

from conftest import ROOT
import check_data


@pytest.fixture(scope="module")
def data():
    d = json.loads((ROOT / "data" / "players.json").read_text())
    if not d.get("rated"):
        pytest.skip("needs rated data")
    return d


def _first(d, pred):
    return next(p for p in d["players"] if pred(p))


def _break(d, how):
    d = copy.deepcopy(d)
    ahead = [n["sp"] for n in d["nights"] if not n["past"]]
    if how == "P2":
        _first(d, lambda p: p.get("owner") != "waivers")["games_left"] += 1
    elif how == "P4":
        p = _first(d, lambda p: (p.get("cr_fpg") or 0) > 0.5 and (p.get("cr_games") or 0) > 0.5)
        p["cr"] = round(p["cr"] + 1.0, 2)
    elif how == "S1":
        _first(d, lambda p: p.get("cr_season") is not None)["cr_season"] = -1.0
    elif how == "S2":
        _first(d, lambda p: p["pos"] != "G" and p.get("proj_gp") is not None)["proj_gp"] = 200
    elif how == "P6":
        _first(d, lambda p: p.get("cr_dress") is not None)["cr_dress"] = 1.5
    elif how == "G1":
        g = _first(d, lambda p: p["pos"] == "G" and (p.get("cr_why") or {}).get("exp_starts") is not None)
        g["cr_why"]["exp_starts"] = g["games_left"] + 2
    elif how == "N1":
        d["nights"][-1]["sp"] += 5
    elif how == "N2":
        n = next(n for n in d["nights"] if n["games"] > 0)
        n["games"] = 0
    elif how == "G2":
        team = next(t for t in {p["team"] for p in d["players"] if p["pos"] == "G" and p["team"] != "FA"}
                    if sum(1 for p in d["players"] if p["pos"] == "G" and p["team"] == t and p["games_left"] > 0
                           and (p.get("cr_why") or {}).get("exp_starts") is not None) >= 2)
        for p in d["players"]:
            if p["pos"] == "G" and p["team"] == team and (p.get("cr_why") or {}).get("exp_starts") is not None:
                p["cr_why"]["exp_starts"] = p["games_left"]
    elif how == "WS1":
        w = d["season_weeks"][0]
        w["light"] = list(w.get("light") or []) + [w["sps"][-1] + 1]
    return d, ahead


@pytest.mark.parametrize("rule, text", [("P2", "P2 "), ("P4", "P4 "), ("S1", "players"), ("S2", "players"), ("P6", "P6 "),
                                        ("G1", "G1 "), ("N1", "not consecutive"), ("N2", "N2 "), ("G2", "G2 "),
                                        ("WS1", "lists games, light nights or dates outside")])
def test_each_publish_rule_fires(data, rule, text):
    """Kills: C-P2, C-P4, C-S1, C-S2, C-P6, C-G1, C-N1a, C-N2, C-G2, C-WS1b."""
    assert check_data.rules(data) == []
    broken, _ = _break(data, rule)
    hits = [e for e in check_data.rules(broken) if e.startswith({"WS1": "S1"}.get(rule, rule)) and text in e]
    assert hits, check_data.rules(broken)


def test_games_played_going_back_blocks_publishing(data):
    """X2: skater games played falling by more than 20 since the published version (an overnight run that lost last
    night's games). Kills: C-X2."""
    prev = copy.deepcopy(data)
    for p in prev["players"]:
        if p["pos"] != "G":
            p.setdefault("cr_why", {})["gp"] = ((p.get("cr_why") or {}).get("gp") or 0) + 1
    assert any(e.startswith("X2") for e in check_data.problems_vs_prev(data, prev))
    assert not any(e.startswith("X2") for e in check_data.problems_vs_prev(data, data))
