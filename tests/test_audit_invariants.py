"""Prototype invariant tests from the hunt 13 tests audit (hunt13/tests_audit/RESULTS.md). Each asserts a rule every
version must keep, rather than one bug's exact numbers, so it also catches the next bug of the same kind."""
import ast
import copy
import json
import re

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
    """{card title: [players the card says to drop]} from Stream, Best moves and Mid-week swaps."""
    return page.evaluate(r"""() => {
      const out = {};
      for (const c of document.querySelectorAll('#upgrades .upg')) {
        const t = c.querySelector('b')?.textContent || '', txt = c.textContent.replace(/\s+/g, ' ');
        const names = [];
        if (t.startsWith('Stream')) { const m = txt.match(/Your roster is full: drop (.+?) to add him/); if (m) names.push(m[1]); }
        if (t.startsWith('Best')) for (const l of c.querySelectorAll(':scope > div:not(.sub)')) {
          const m = l.textContent.replace(/\s+/g, ' ').match(/ for (.+?) \+/); if (m) names.push(m[1]); }
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
        g3 = next((c for c in txt if c.startswith("Goalies") and "third goalie spot" in c), None)
        st = next((c for c in txt if c.startswith("Stream")), None)
        m = st and re.search(r"Your roster is full: drop (.+?) to add him", st)
        if g3 and m:
            gname = re.search(r"drop (.+?) for a confirmed starter", g3).group(1)
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
