"""Prototype invariant tests from the hunt 13 tests audit (hunt13/tests_audit/RESULTS.md). Each asserts a rule every
version must keep, rather than one bug's exact numbers, so it also catches the next bug of the same kind."""
import ast
import copy
import json

import pytest

import check_data
from conftest import ROOT
from test_page import _mine, browser, data, open_page, server  # noqa: F401 - pytest fixtures

THU_SP_INDEX = 3  # Thursday of a Monday-Sunday matchup


# ---------------------------------------------------------------- 1. static: one definition per module-level name (400)
def test_no_module_level_name_is_defined_twice():
    """PR #158 re-bound EARLY_GAMES (goalie rule, 10) to 15 for an unrelated skater rule 600 lines later, and the goalie
    rule silently changed (handoff 400). A second module-level assignment of the same name is never intended."""
    dup = {}
    for f in sorted((ROOT / "scripts").glob("*.py")):
        seen = {}
        for node in ast.parse(f.read_text()).body:
            names = []
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                for t in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    names += [n.id for n in ast.walk(t) if isinstance(n, ast.Name)]
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.append(node.name)
            for n in names:
                seen.setdefault(n, []).append(node.lineno)
        dup.update({f"{f.name}:{k}": v for k, v in seen.items() if len(v) > 1})
    assert not dup, f"module-level names defined more than once (line numbers): {dup}"


# ---------------------------------------------------------------- 2. publish checks P7, G4, G5 (453, 446, 454)
def test_publish_check_catches_unrated_ghosts_and_zeroed_backups(data):
    assert not [e for e in check_data.rules(data) if e[:2] in ("P7", "G4")]
    assert not [w for w in check_data.warnings(data, None) if w.startswith("G5")]
    if not data["rated"]:
        return
    # P7: a retired name ESPN lists on a team, rated from the league-average prior (handoff 453)
    ghost = copy.deepcopy(data)
    p = next(q for q in ghost["players"] if not q.get("cr_matched") and q["pos"] != "G")
    p.update(cr=0.23, cr_games=0.2, cr_fpg=1.15, cr_season=0.12)
    assert any(e.startswith("P7") for e in check_data.rules(ghost))
    # G4: a healthy backup the Week expects to start, with Season 0 (handoff 446)
    zero = copy.deepcopy(data)
    g = next(q for q in zero["players"] if q["pos"] == "G" and q["team"] != "FA" and q.get("cr_matched") and not q.get("injury")
             and (q["cr_why"].get("exp_starts") or 0) >= 0.05)
    g["cr_season"] = 0.0
    assert any(e.startswith("G4") for e in check_data.rules(zero))
    g["injury"] = "INJURY_RESERVE"  # ... but a hurt goalie can be 0
    assert not any(e.startswith("G4") for e in check_data.rules(zero))
    # G5: a confirmed starter matched to nobody zeroes his team's night for every goalie (handoff 454)
    night = copy.deepcopy(data)
    ahead = {str(n["sp"]) for n in night["nights"] if not n["past"]}
    team, sp = next((q["team"], sp) for q in night["players"] if q["pos"] == "G" and q["team"] != "FA"
                    for sp in (q.get("cr_g_nights") or {}) if sp in ahead)
    for q in night["players"]:
        if q["pos"] == "G" and q["team"] == team and sp in (q.get("cr_g_nights") or {}):
            q["cr_g_nights"][sp] = 0.0
    assert any(w.startswith("G5") for w in check_data.warnings(night, None))


# ---------------------------------------------------------------- shared: the data as a Thursday run
def _thursday(data, run_hour=10):
    """The data as a Thursday Oct 15, 2026 run at run_hour ET: Mon-Wed past, first puck 7 PM ET, Daily Faceoff
    confirmations cleared, moves_used 3 of 7 (4 left)."""
    d = json.loads(json.dumps(data))
    nights = d["nights"][-7:]
    for i, n in enumerate(nights):
        n["date"] = f"{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][i]} {12 + i}"
        n["past"] = i < THU_SP_INDEX
    for n in d["nights"][:-7]:
        n["past"] = True
    tonight = nights[THU_SP_INDEX]["sp"]
    ahead = [n["sp"] for n in nights if not n["past"]]
    d.update(updated_date="2026-10-15", updated=f"Thu Oct 15, {run_hour}:00 AM ET", updated_utc=f"2026-10-15T{run_hour + 4:02d}:00:00Z",
             first_start="2026-10-15T23:00:00Z", final_matchup=False, move_limit=7, matchup_weeks=1, season_over=False)
    for t in d["teams"]:
        t["moves_used"] = 3
    for p in d["players"]:
        pn = p.get("nights") or []
        p["today_start"] = d["first_start"] if tonight in pn else None
        if p.get("owner") != "waivers":
            p["games_left"] = sum(sp in ahead for sp in pn)
        if p.get("cr_games") is not None:
            p["cr_games"] = min(p["cr_games"], p["games_left"])
            p["cr"] = round((p.get("cr_fpg") or 0) * p["cr_games"], 2)
        why = p.get("cr_why") or {}
        if p["pos"] == "G" and why:
            why.pop("tonight", None)
            why["p_today"] = (p.get("cr_g_nights") or {}).get(str(tonight), 0.0) if tonight in pn else 0.0
            why.pop("p_today_pre", None)
    return d, tonight


def _et(day, hour, minute=0):
    from datetime import datetime, timedelta, timezone
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone(timedelta(hours=-4)))


# ---------------------------------------------------------------- 3. page: the publish rules hold after dropStarted (448)
def test_page_numbers_keep_the_publish_rules_after_the_first_puck(browser, server, data):  # noqa: F811
    """Once tonight's games start, the page takes them out of Week and Games (dropStarted). What it shows must still keep
    the rules check_data enforces on the data: Week = pts/game x games (P4), games within games left (P3). Handoff 448:
    a goalie's tonight was priced at its own line, so his shown pts/start no longer matched Week / starts."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, tonight = _thursday(data)
    for p in d["players"]:  # tonight's line differs from the week's blended price, as it does for most goalies
        if p["pos"] == "G" and p["cr_why"].get("p_today"):
            p["cr_why"]["line"] = {"pps": round((p["cr_fpg"] or 0) + 1.0, 2), "win_prob": 0.5}
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 20))  # 8 PM ET, games under way
    shown = page.evaluate("state.data.players")
    gone = [p for p in shown if p.get("today_start") and p.get("cr_games") is not None]
    assert gone, "no player had a game tonight"
    bad = []
    for p in shown:
        cr, fpg, g = p.get("cr") or 0, p.get("cr_fpg") or 0, p.get("cr_games") or 0
        if abs(cr - fpg * g) > 0.006 + 0.005 * (g + abs(fpg)) + 0.01:
            bad.append(f"P4 {p['name']} {cr} != {fpg} x {g}")
        if g > p["games_left"] + check_data.EPS:
            bad.append(f"P3 {p['name']} {g} > {p['games_left']}")
    assert not bad, bad[:5]
    assert errors == []
    ctx.close()


# ---------------------------------------------------------------- 4. cards: never drop tonight's starter before his game (461)
def test_cards_never_drop_tonights_starting_goalie_before_his_game(browser, server, data):  # noqa: F811
    """Before the first puck, no card (Goalies, Best moves, Mid-week swaps) tells a team to drop a goalie Daily Faceoff has
    starting tonight. Handoff 461: the 4-goalie Goalies card named the lowest-keep goalie, tonight's confirmed starter."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, tonight = _thursday(data)
    team = d["teams"][0]
    plays = [p for p in d["players"] if p["pos"] == "G" and tonight in (p.get("nights") or []) and p.get("cr_matched")]
    others = [p for p in d["players"] if p["pos"] == "G" and p.get("cr_matched") and p not in plays]
    for p in _mine(d, team, "G"):
        p["team_id"], p["owner"] = None, "freeagent"
    starter = plays[0]
    for p in [starter] + others[:3]:  # four healthy goalies; tonight's confirmed starter has the lowest keep value
        p.update(team_id=team["id"], owner=str(team["id"]), ir=False, injury=None)
    starter.update(cr=0.5, cr_season=0.1)
    starter["cr_why"]["tonight"] = {"starting": True, "status": "Confirmed"}
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))  # 4 PM ET, before the 7 PM puck
    page.select_option("#team", str(team["id"]))
    cards = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].map(c => c.innerText)")
    for text in cards:
        title = text.split("·")[0].strip()
        if title.startswith("Goalies") and "you carry" in text:  # the stream-slot card ("your weakest" is the FA list)
            assert starter["name"] not in text, f"Goalies card drops tonight's starter: {text}"
        if title.startswith("Goalies") and "your weakest:" in text:  # the FA list names whom to replace (handoff 485)
            assert not text.split("your weakest:")[1].strip().startswith(starter["name"]), text
        if title.startswith("Best") or title.startswith("Mid-week"):
            for line in text.split("\n")[1:]:
                assert not line.split(" for ")[-1].startswith(starter["name"]) and not line.startswith(f"{starter['name']}"), line
    assert errors == []
    ctx.close()


# ---------------------------------------------------------------- 5. cards: adds offered never exceed moves left (463)
def _adds_offered(page):
    """Adds the cards offer: swap subs, Best moves lines, listed stream starters (before the puck) and IR stash lines."""
    return page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#upgrades .upg')];
      const find = re => cards.find(c => re.test(c.innerText));
      const lines = c => c ? [...c.querySelectorAll(':scope > div:not(.sub)')] : [];
      return {
        swaps: (find(/^Mid-week swaps/)?.innerText.match(/→/g) || []).length,
        best: lines(find(/^Best \\d* ?moves?\\b/)).filter(l => / for |^Add /.test(l.innerText)).length,
        stream: lines(find(/^Stream a goalie tonight/)).filter(l => l.innerText.includes('starts tonight')).length ? 1 : 0,
        stash: lines(find(/^IR stash/)).length ? 1 : 0,
      };
    }""")


def test_adds_offered_never_exceed_moves_left(browser, server, data):  # noqa: F811
    """Across every team, on the matchup's last call (Sunday before the puck) and mid-week, the cards together never offer
    more adds than the team has moves left: a listed stream starter counts too (handoff 463, findings 233). A stream line
    is one add at most, since only one goalie fills a slot."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    from test_page import _sunday
    sun = _sunday(data)
    last = sun["nights"][-1]["sp"]
    for scen, d, clock, used in (("sunday", sun, _et(11, 16, 30), 6), ("thursday", _thursday(data)[0], _et(15, 16), 6)):
        tonight = next(n["sp"] for n in d["nights"] if not n["past"])
        for t in d["teams"]:
            t["moves_used"] = used
        for p in d["players"]:  # every team's goalies sit tonight, so every team has an open G slot
            if p["pos"] == "G" and p.get("cr_why"):
                p["cr_why"].pop("tonight", None)
                p["cr_why"]["p_today"] = 0
        fa_g = next(p for p in d["players"] if p["pos"] == "G" and p["owner"] == "freeagent" and p.get("cr_matched")
                    and tonight in (p.get("nights") or []))
        fa_g.update(injury=None, games_left=max(fa_g["games_left"], 1), today_start=d["first_start"])
        fa_g["cr_why"]["tonight"] = {"starting": True, "status": "Confirmed"}
        ctx, page, errors = open_page(browser, server, data=d, clock=clock)
        over = []
        for t in d["teams"]:
            page.select_option("#team", str(t["id"]))
            n = _adds_offered(page)
            if sum(n.values()) > 7 - used:
                over.append((scen, t["id"], n))
        assert not over, f"more adds offered than moves left: {over[:4]}"
        assert errors == []
        ctx.close()


# ---------------------------------------------------------------- 6. cards: a swap sub must gain this week (462)
def test_swap_sub_beats_the_player_he_replaces(browser, server, data):  # noqa: F811
    """Mid-week swaps names a sub only when he projects more this week than the player who sat (handoff 462: McAvoy
    Week 5.19 -> Mateychuk 4.83 cost a move and points). OUT/IR players are exempt: they score nothing."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, tonight = _thursday(data)
    for t in d["teams"]:  # each team's best healthy skater with games left sat its last game
        sk = [p for p in _mine(d, t) if p["pos"] != "G" and not p["ir"] and not p.get("injury") and p["games_left"] > 0]
        if sk:
            top = max(sk, key=lambda p: p["cr"])
            top.update(sat_last=True, dfo_out=None)
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
    by_name = {p["name"]: p for p in page.evaluate("state.data.players")}
    worse = []
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        text = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].find(c => c.innerText.startsWith('Mid-week swaps'))?.innerText || ''")
        for line in text.split("\n")[1:]:
            if "→" not in line or "sat last game" not in line:
                continue
            out_name = line.split("sat last game")[0].strip()
            sub_name = next((n for n in by_name if line.split("→")[1].strip().startswith(n)), None)
            if out_name in by_name and sub_name and by_name[sub_name]["cr"] <= by_name[out_name]["cr"]:
                worse.append((t["id"], out_name, by_name[out_name]["cr"], sub_name, by_name[sub_name]["cr"]))
    assert not worse, f"swap subs who project less this week than the player they replace: {worse[:4]}"
    assert errors == []
    ctx.close()
