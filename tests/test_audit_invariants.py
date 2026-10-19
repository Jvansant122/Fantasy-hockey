"""Prototype invariant tests from the hunt 13 tests audit (hunt13/tests_audit/RESULTS.md). Each asserts a rule every
version must keep, rather than one bug's exact numbers, so it also catches the next bug of the same kind."""
import ast
import copy
import json
import re

import pytest

import check_data
from conftest import ROOT
from test_page import _card, _mine, browser, data, open_page, server  # noqa: F401 - pytest fixtures

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
        why = p.get("cr_why") or {}
        if p["pos"] == "G" and why:
            why.pop("tonight", None)
            why["p_today"] = (p.get("cr_g_nights") or {}).get(str(tonight), 0.0) if tonight in pn else 0.0
            why.pop("p_today_pre", None)
        if p.get("cr_games") is not None:
            # dropStarted takes out tonight's chance, not a whole game, so cap at what the page will keep (handoff 719)
            cap = p["games_left"]
            if tonight in pn and cap > 0:
                t = why.get("p_today") if p["pos"] == "G" else p.get("cr_dress_next", p.get("cr_dress"))
                cap = cap - 1 + min(max(t or 0.0, 0.0), 1.0)
            p["cr_games"] = round(min(p["cr_games"], cap), 2)
            p["cr"] = round((p.get("cr_fpg") or 0) * p["cr_games"], 2)
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


# ---------------------------------------------------------------- page: slots, rounding, sorting (handoffs 493-499)
def test_stream_card_after_the_puck_counts_tomorrows_open_goalie_slots(browser, server, data):  # noqa: F811
    """After tonight's first puck an added goalie plays from tomorrow, so the Stream card names tomorrow and its open G
    slots, and stays hidden when tomorrow has none (handoff 493: '2 open G slots on Wed 14' with 0 open Thursday)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, _ = _thursday(data, run_hour=6)
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 20))  # 8 PM ET, games under way
    shown = 0
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        g = page.evaluate("state.openSlots[state.data.nights.find(n => n.date === 'Fri 16').sp].G")
        card = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].map(c => c.innerText).find(x => x.startsWith('Stream a goalie')) || ''")
        if g == 0:
            assert card == "", (t["id"], card)
        elif card:
            assert "from tomorrow" in card and f"{g} open G slot" in card and "on Fri 16" in card, (t["id"], card)
            shown += 1
    assert shown, "no team showed the Stream card"
    assert errors == []
    ctx.close()


def test_a_forward_can_take_the_empty_utl(browser, server, data):  # noqa: F811
    """With 9 forwards and 3 D playing, the UTL is empty, so a free-agent forward who plays that night has an open game
    (handoff 499: Frondell and Wennberg showed Open 0)."""
    d = json.loads(json.dumps(data))
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(d["teams"][0]["id"]))
    assert page.evaluate("JSON.stringify(slotsLeft(9, 3, 2))") == json.dumps({"F": 0, "D": 2, "U": 1, "G": 0}).replace(" ", "")
    assert page.evaluate("room(slotsLeft(9, 3, 2), 'F') && !room(slotsLeft(10, 3, 2), 'F') && room(slotsLeft(10, 3, 2), 'D')")
    assert page.evaluate("!room(slotsLeft(9, 6, 2), 'F') && !room(slotsLeft(9, 6, 2), 'D') && !room(slotsLeft(9, 3, 2), 'G')")
    assert errors == []
    ctx.close()


def test_details_round_like_the_table_and_blanks_sort_last(browser, server, data):  # noqa: F811
    """The details show the table's figure (handoff 494: Season 2.2 in the table, 2.1 in details), and a column with
    blanks puts them last both ways (handoff 497)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = json.loads(json.dumps(data))
    p = next(p for p in d["players"] if p.get("owner") == "freeagent" and p["pos"] != "G" and (p.get("cr_season") or 0) > 0.5)
    p["cr_season"], p["cr"] = 2.15, 3.45  # x.x5: Math.round goes up, toFixed down
    p["today_start"] = None  # after his first puck the table shows the games left, not cr (handoff 645)
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#who", "every")
    page.fill("#q", p["name"])
    row = page.locator(f'#rows tr[data-id="{p["id"]}"]')
    cells = row.inner_text()
    assert "2.2" in cells and "3.5" in cells
    row.click()
    why = page.locator("#rows tr.why").inner_text()
    assert "Season: 2.2" in why and "= 3.5" in why, why
    page.fill("#q", "")
    for k in ("cur_ppg", "last_ppg"):
        for _ in range(2):
            page.locator(f'#head th[data-k="{k}"]').click()
            vals = page.evaluate(f"[...document.querySelectorAll('#rows tr[data-id]')].map(r => state.data.players.find(p => p.id === +r.dataset.id)).map(p => val(p, '{k}'))")
            blank = [v is None for v in vals]
            assert blank == sorted(blank), (k, page.evaluate("state.dir"))  # every blank after every number
    assert errors == []
    ctx.close()


def _card_drops(page):
    """{card title: [players the card says to drop]} from Stream, Best moves, IR stash and Mid-week swaps."""
    return page.evaluate(r"""() => {
      const out = {};
      for (const c of document.querySelectorAll('#upgrades .upg')) {
        const t = c.querySelector('b')?.textContent || '', txt = c.textContent.replace(/\s+/g, ' ');
        const names = [];
        if (t.startsWith('Stream')) { const m = txt.match(/Your roster is full: drop (.+?) to add him/); if (m) names.push(m[1]); }
        if (t.startsWith('Best')) for (const l of c.querySelectorAll(':scope > div:not(.sub)')) {
          const m = l.textContent.replace(/\s+/g, ' ').match(/ for (.+?) \+/); if (m) names.push(m[1]); }
        if (t.startsWith('IR stash')) { const m = txt.match(/Your roster is full: drop (.+?), add him/); if (m) names.push(m[1]); }
        if (t.startsWith('Mid-week')) for (const l of c.querySelectorAll(':scope > div')) {
          if (l.textContent.includes('→')) names.push(l.firstChild.textContent.trim()); }
        if (names.length) out[t] = names;
      }
      return out;
    }""")


def test_no_player_is_dropped_by_two_cards(browser, server, data):
    """One roster spot, one drop: Stream a goalie, Best moves and Mid-week swaps never name the same player to drop
    (hunt 14: on Thu Oct 15 the Stream card said 'drop Kaapo Kakko to add him' and Best move said 'Tyson Foerster for
    Kaapo Kakko')."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, tonight = _thursday(data)
    for t in d["teams"]:  # 6 of 7 moves left: room for a stream and Best moves
        t["moves_used"] = 1
    fa_g = next(p for p in d["players"] if p["pos"] == "G" and p["owner"] == "freeagent" and p.get("cr_matched")
                and tonight in (p.get("nights") or []))
    fa_g.update(injury=None, games_left=max(fa_g["games_left"], 1), today_start=d["first_start"])
    fa_g["cr_why"]["tonight"] = {"starting": True, "status": "Confirmed"}
    for p in d["players"]:  # every rostered goalie sits tonight: every team has an open G slot
        if p["pos"] == "G" and p is not fa_g and p.get("cr_why"):
            p["cr_why"].pop("tonight", None)
            p["cr_why"]["p_today"] = 0
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
    twice = []
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        seen = {}
        for title, names in _card_drops(page).items():
            for n in names:
                if n in seen:
                    twice.append((t["id"], n, seen[n], title))
                seen[n] = title
    assert not twice, f"same player dropped by two cards: {twice[:4]}"
    assert errors == []
    ctx.close()


def test_stream_card_drops_the_third_goalie(browser, server, data):
    """A team carrying 3 healthy goalies streams with its third goalie spot (the Goalies card says so): a full roster's
    Stream card names that goalie as the drop, not a skater."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, tonight = _thursday(data)
    fa_g = next(p for p in d["players"] if p["pos"] == "G" and p["owner"] == "freeagent" and p.get("cr_matched")
                and tonight in (p.get("nights") or []))
    fa_g.update(injury=None, games_left=max(fa_g["games_left"], 1), today_start=d["first_start"])
    fa_g["cr_why"]["tonight"] = {"starting": True, "status": "Confirmed"}
    for p in d["players"]:
        if p["pos"] == "G" and p is not fa_g and p.get("cr_why"):
            p["cr_why"].pop("tonight", None)
            p["cr_why"]["p_today"] = 0
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
    bad = []
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        txt = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].map(c => c.textContent.replace(/\\s+/g, ' '))")
        g3 = next((c for c in txt if c.startswith("Goalies") and ("third goalie spot" in c or "you carry" in c)), None)
        st = next((c for c in txt if c.startswith("Stream")), None)
        m = st and re.search(r"Your roster is full: drop (.+?) to add him", st)
        if g3 and m:  # 3 goalies: the stream slot's goalie; 4+: the first the card lists (handoff 633)
            gm = re.search(r"drop (.+?) for a confirmed starter", g3) or re.search(r"(?:skater|start)\. ([^.]+?) [A-Z]{2,3}, ", g3)  # after handoff 653's note when it shows
            gname = gm.group(1)
            if m.group(1) != gname:
                bad.append((t["id"], m.group(1), gname))
    assert not bad, f"Stream card drops a skater while the Goalies card names the third goalie: {bad[:4]}"
    assert errors == []
    ctx.close()


def test_stream_recheck_names_every_run_before_the_puck(browser, server, data):
    """Before 9:10 AM the Stream card's 'rechecks at about ...' lists the 9:10 AM update too (RUNS has 4 runs a day)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, tonight = _thursday(data, run_hour=6)
    for p in d["players"]:
        if p["pos"] == "G" and p.get("cr_why"):
            p["cr_why"].pop("tonight", None)
            p["cr_why"]["p_today"] = 0
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 7))
    page.select_option("#team", str(d["teams"][0]["id"]))
    st = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].map(c => c.textContent).find(t => t.includes('No free-agent goalie is confirmed')) || ''")
    assert "rechecks" in st, st
    assert "9" in st.split("rechecks")[1].split("ET")[0], st
    assert errors == []
    ctx.close()


def test_every_rating_why_key_is_read_by_the_page():
    """Every cr_why key rating.py writes is read by index.html's details, or listed here as internal (handoff 613). Hunt 14:
    week_slack and mover_pps were written but never shown, while the Season slack line said the opposite for 35 players."""
    r = (ROOT / "scripts" / "rating.py").read_text()
    h = (ROOT / "index.html").read_text()
    keys = set(re.findall(r'why\[["\'](\w+)["\']\]', r)) | set(re.findall(r'cr_why["\']\]\[["\'](\w+)["\']\]', r))
    used = set(re.findall(r'(?:cr_why\??\.|\bw\.|why\??\.)(\w+)', h))
    # shown through boost_week_move / used by check_data / the logit move, shown as missing_regs_pp (handoff 604)
    internal = {"boost_week", "season_pts", "missing_regs"}
    assert not keys - used - internal, sorted(keys - used - internal)


# ---------------------------------------------------------------- a lineup cut after week_slack keeps Daily Faceoff's next-game floor (722)
def test_lineup_cuts_keep_the_dfo_floor(monkeypatch, tmp_path):
    """The returning-regular and newcomer cuts run after week_slack. A skater in his team's fresh Daily Faceoff lines keeps
    the next-game floor (721) through them; later games still take the cut, and games stay the sum of the chances."""
    from datetime import datetime, timezone
    from test_add_ratings import espn, rate, rating
    monkeypatch.setattr(rating, "utcnow", lambda: datetime(2026, 10, 9, 16, tzinfo=timezone.utc))
    for name, fn, ret in (("displaced", "displaced_skaters", {1: (0.12, "R")}), ("newcomer", "roster_moves", ({1: (0.12, "N")}, {}))):
        for lines in (False, None):
            with monkeypatch.context() as m:
                m.setattr(rating, fn, lambda *a, **k: ret)
                p = rate(m, tmp_path, [dict(espn(101, "Auston Matthews", "TOR", "C"), today_start="2026-10-09T23:00:00Z",
                                            dfo_out=lines)])["Auston Matthews"]
            assert name in p["cr_why"]
            assert (p["cr_dress_next"] >= rating.DFO_IN_LINES_NEXT) == (lines is False), (name, lines, p["cr_dress_next"])
            assert p["cr_games"] == pytest.approx(p["games_left"] * p["cr_dress"], abs=0.02)


# ---------------------------------------------------------------- cards: Activate from IR's drop isn't offered a replacement (723)
def _activate_vs_drop_list(page):
    return page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#upgrades .upg')];
      const act = cards.find(c => /^Activate from IR/.test(c.innerText));
      const m = act && act.innerText.match(/drop (.+?) first/);
      if (!m) return null;
      const drop = cards.find(c => /^Drop list/.test(c.innerText));
      const line = drop && [...drop.querySelectorAll(':scope > div')].map(l => l.innerText).find(t => t.startsWith(m[1]));
      return {name: m[1], line: line || ""};
    }""")


def test_activate_from_ir_drop_is_not_offered_a_replacement(browser, server, data):  # noqa: F811
    """With a full roster, Activate from IR says "drop X first" so the healthy IR player gets X's spot. The Drop list must not
    then offer "X ... replace with <free agent>", which would fill that spot (handoff 723)."""
    d = copy.deepcopy(data)
    for t in d["teams"]:  # a healthy player sits in each team's IR slot
        sk = [p for p in _mine(d, t) if p["pos"] != "G" and not p["ir"] and not p.get("injury")]
        if sk:
            sk[-1]["ir"] = True
    d["roster_max"] = min(sum(not p["ir"] for p in _mine(d, t)) for t in d["teams"])  # every roster is full
    seen, bad = 0, []
    for case in (data, d):
        ctx, page, errors = open_page(browser, server, data=case)
        for t in case["teams"]:
            page.select_option("#team", str(t["id"]))
            r = _activate_vs_drop_list(page)
            if r:
                seen += 1
                if "replace with" in r["line"]:
                    bad.append((t["id"], r["line"]))
        assert errors == []
        ctx.close()
    assert seen >= len(d["teams"]) // 2, seen
    assert not bad, bad[:3]


# ---------------------------------------------------------------- cards: one roster spot, one plan (handoffs 724-728)
def _plans(page):
    """Who each card drops, the Drop list's lines, the Trade ideas' chips and whether Best moves holds everything."""
    return page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#upgrades .upg')];
      const find = re => cards.find(c => re.test(c.innerText));
      const lines = c => c ? [...c.querySelectorAll(':scope > div:not(.sub)')].map(l => l.innerText) : [];
      const drops = [];
      const m1 = find(/^Activate from IR/)?.innerText.match(/drop (.+?) first/);
      if (m1) drops.push(['activate', m1[1], find(/^Activate from IR/).innerText]);
      for (const l of lines(find(/^Mid-week swaps/))) if (l.includes('→')) drops.push(['swap', l.split(/[\\n→]/)[0].trim(), l]);
      for (const l of lines(find(/^Best \\d* ?moves?\\b/))) { const m = l.match(/ for (.+?)(?: \\+| \\(|$)/m); if (m && !l.startsWith('Add ')) drops.push(['best', m[1].trim(), l]); }
      const st = find(/^Stream a goalie/)?.innerText.match(/drop (.+?)(?: \\(or trade him: see Trade ideas\\))? to add him/);
      if (st) drops.push(['stream', st[1], find(/^Stream a goalie/).innerText]);
      const sh = find(/^IR stash/)?.innerText.match(/drop (.+?)(?: \\(or trade him: see Trade ideas\\))?, add him/);
      if (sh) drops.push(['stash', sh[1], find(/^IR stash/).innerText]);
      return {
        drops,
        dropList: lines(find(/^Drop list/)),
        chips: lines(find(/^Trade ideas/)).map(l => (l.match(/^Offer (.+?)(?: \\([FD]\\))? for /) || [])[1]).filter(Boolean),
        held: !!find(/^Best moves · none today/),
      };
    }""")


def _ir_full(data):
    """Every team with a healthy player in its IR slot and a full roster (the Activate, stash and Drop list cards all show)."""
    d = copy.deepcopy(data)
    for t in d["teams"]:
        sk = [p for p in _mine(d, t) if p["pos"] != "G" and not p["ir"] and not p.get("injury")]
        if sk:
            sk[-1]["ir"] = True
    d["roster_max"] = min(sum(not p["ir"] for p in _mine(d, t)) for t in d["teams"])
    return d


def test_one_roster_spot_one_plan(browser, server, data):  # noqa: F811
    """No player is dropped by two cards (Activate from IR, Mid-week swaps, Best moves, Stream, IR stash), and a card's drop
    is never offered 'replace with' on the Drop list (handoffs 723-725). Every card that drops a Trade-ideas chip says so (726).
    The Drop list's 'replace with' adds, with the other cards' adds, stay within the moves left, and none under 'none today'
    (727)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    bad, seen = [], 0
    for scen, d, clock, used in (("live", data, None, None), ("ir-full", _ir_full(data), None, None),
                                 ("thursday", _ir_full(_thursday(data)[0]), _et(15, 10), 2)):
        if used is not None:
            for t in d["teams"]:
                t["moves_used"] = used
        ctx, page, errors = open_page(browser, server, data=d, clock=clock)
        for t in d["teams"]:
            page.select_option("#team", str(t["id"]))
            r = _plans(page)
            names = [n for _, n, _ in r["drops"]]
            seen += len(names)
            if len(names) != len(set(names)):
                bad.append((scen, t["id"], "dropped twice", r["drops"]))
            for name in names:
                line = next((l for l in r["dropList"] if l.startswith(name)), "")
                if "replace with" in line:
                    bad.append((scen, t["id"], "card drop offered a replacement", line))
            for card, name, text in r["drops"]:
                if name in r["chips"] and card != "swap" and "Trade ideas" not in text:
                    bad.append((scen, t["id"], "trade chip dropped silently", card, name))
            n = _adds_offered(page)
            replace = sum("replace with" in l for l in r["dropList"])
            left = 7 - (used if used is not None else (t.get("moves_used") or 0))
            if t.get("moves_used") is not None and sum(n.values()) + replace > left:
                bad.append((scen, t["id"], "adds beyond moves left", n, replace, left))
            if r["held"] and replace:
                bad.append((scen, t["id"], "replace with under none today"))
        assert errors == []
        ctx.close()
    assert seen, "no card dropped anyone"
    assert not bad, bad[:4]


def test_activated_ir_player_takes_the_open_roster_spot(browser, server, data):  # noqa: F811
    """One open roster spot and a healthy IR player: activating takes the spot, so Best moves still names a drop and the IR
    stash counts the roster as full (handoff 728)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = _ir_full(data)
    d["roster_max"] += 1  # the fullest rosters now have one open spot
    ctx, page, errors = open_page(browser, server, data=d)
    checked = 0
    for t in d["teams"]:
        if sum(not p["ir"] for p in _mine(d, t)) + 1 != d["roster_max"] or not any(p["ir"] for p in _mine(d, t)):
            continue
        page.select_option("#team", str(t["id"]))
        checked += 1
        best = _card(page, "Best").split("\n")[1:]
        assert not any(l.startswith("Add ") for l in best), (t["id"], best)
        stash = _card(page, "IR stash")
        assert not stash or "roster is full" in stash, (t["id"], stash)
    assert checked
    assert errors == []
    ctx.close()


# ---------------------------------------------------------------- publish checks P8 and P9 (handoffs 743-744)
def test_publish_check_keeps_daily_faceoffs_floors(data):
    """Handoffs 720 and 722 were the same bug twice: a later step pushed an in-lines skater under Daily Faceoff's
    next-game floor and it reached the live site. P9 catches it and blocks publishing (a warning until
    every run of Oct 19 kept it, handoff 743)."""
    import rating
    assert check_data.DFO_FLOORS == (rating.DFO_LINEUP_NEXT, rating.DFO_IN_LINES_NEXT)
    assert set(check_data.IDLE_CAPS) == {c for _, c in rating.IDLE_DRESS} | {rating.NOT_PLAYING_DRESS}
    if not data.get("rated"):
        pytest.skip("no ratings in this data")
    assert not [e for e in check_data.rules(data, soft=True) if e.startswith("P9")]
    d = copy.deepcopy(data)
    started = lambda p: p.get("today_start") and d.get("updated_utc") and p["today_start"] <= d["updated_utc"]
    p = next((p for p in d["players"] if p["pos"] != "G" and p.get("dfo_out") is False and not p.get("injury")
              and p.get("owner") != "waivers" and p.get("games_left") and (p.get("cr_dress_next") or 0) > 0.001
              and not started(p) and (p.get("sat_last") or (p.get("cr_why") or {}).get("gp"))), None)
    if p is None:
        pytest.skip("no skater in Daily Faceoff's lines with a game ahead")
    p["cr_dress_next"] = 0.83
    assert [e for e in check_data.rules(d, soft=True) if e.startswith("P9")]
    assert [e for e in check_data.rules(d) if e.startswith("P9")]  # blocks publishing


def _rewound(data):
    """The data as a run before the matchup's first night."""
    d = json.loads(json.dumps(data))
    nights = d["nights"][-7:]
    for n in d["nights"]:
        n["past"] = n["sp"] < nights[0]["sp"]
    ahead = {n["sp"] for n in nights}
    for p in d["players"]:
        old = p.get("games_left") or 0
        if p.get("owner") != "waivers":
            p["games_left"] = sum(sp in ahead for sp in p.get("nights") or [])
        p["today_start"] = None
        if p.get("cr_games") is not None:
            q = p.get("cr_dress") if p["pos"] != "G" else (p["cr_games"] / old if old else 0.5)
            p["cr_games"] = round(p["cr_games"] + max(p["games_left"] - old, 0) * min(max(q or 0.0, 0.0), 1.0), 2)
            p["cr"] = round((p.get("cr_fpg") or 0) * p["cr_games"], 2)
    return d


@pytest.mark.parametrize("rewind", [False, True])
def test_thursday_fixture_keeps_the_publish_rules(data, rewind):
    """The Thursday fixture's handoff 719 bug only failed the page test on Monday-Wednesday data; rewinding the data
    to before the matchup's first night makes the check fail on every weekday (handoff 744)."""
    if not data.get("rated"):
        pytest.skip("no ratings in this data")
    d, tonight = _thursday(_rewound(data) if rewind else data)
    assert any(tonight in (p.get("nights") or []) and p.get("cr_games") is not None for p in d["players"])
    assert not [e for e in check_data.rules(d) if e[:2] in ("P3", "P4", "P8")]
