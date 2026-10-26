"""Hunt 14 mutate stream: page invariants for move-advice rules no test pinned (copy into tests/; they reuse the
test_audit_invariants Thursday harness)."""
import re

import pytest

from test_audit_invariants import _card_drops, _et, _thursday
from test_page import _mine, browser, data, open_page, server  # noqa: F401 - pytest fixtures


def _cards(page):
    return page.evaluate("[...document.querySelectorAll('#upgrades .upg')].map(c => c.innerText)")


def _keep(p):
    return (p.get("cr") or 0) + 7 * (p.get("cr_season") or 0)


def test_drop_list_keeps_eleven_healthy_forwards(browser, server, data):  # noqa: F811
    """Handoff 643: the Drop list first offers players whose drop leaves at least 11 healthy F and 6 healthy D. Each team
    is cut to exactly 11 healthy forwards (the rest day-to-day) and its weakest healthy forward set to 0: he must not head
    the Drop list while anyone else can go. Kills: I05."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, _ = _thursday(data)
    weakest = {}
    for t in d["teams"]:
        fw = sorted([p for p in _mine(d, t) if p["pos"] not in ("G", "D") and not p["ir"] and not p.get("injury")
                     and not p.get("pending_drop")], key=_keep)
        if len(fw) < 12:
            continue
        for p in fw[11:]:
            p["injury"] = "DAY_TO_DAY"
        fw[0].update(cr=0.0, cr_season=0.0, cr_games=0.0)
        weakest[t["id"]] = fw[0]["name"]
    assert weakest
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
    bad = []
    for tid, name in weakest.items():
        page.select_option("#team", str(tid))
        card = next((c for c in _cards(page) if c.startswith("Drop list")), "")
        lines = [l for l in card.split("\n")[1:] if l.strip() and "Move to IR" not in l]
        if lines and lines[0].startswith(name):
            bad.append((tid, name))
    assert not bad, f"Drop list leads with a forward whose drop leaves 10 healthy forwards: {bad[:4]}"
    assert errors == []
    ctx.close()


def test_out_player_kept_out_of_ir_is_swapped_only_for_as_much_keep_value(browser, server, data):  # noqa: F811
    """Handoff 644: with no IR slot open, an OUT skater is swapped only for a sub who keeps as much value. Each team's best
    skater is listed OUT; no sub out-keeps a star, so none may be named. Kills: I07."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, _ = _thursday(data)
    d["ir_slots"] = 0
    stars = {}
    for t in d["teams"]:
        sk = [p for p in _mine(d, t) if p["pos"] != "G" and not p["ir"] and not p.get("injury")]
        if sk:
            top = max(sk, key=_keep)
            top["injury"] = "OUT"
            stars[t["id"]] = top
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
    by_name = {p["name"]: p for p in d["players"]}
    bad = []
    for tid, star in stars.items():
        page.select_option("#team", str(tid))
        card = next((c for c in _cards(page) if c.startswith("Mid-week swaps")), "")
        for line in card.split("\n")[1:]:
            if line.startswith(star["name"]) and "→" in line:
                sub = next((n for n in by_name if line.split("→")[1].strip().startswith(n)), None)
                if sub and _keep(by_name[sub]) < _keep(star):
                    bad.append((tid, star["name"], sub))
    assert not bad, f"OUT player swapped for a sub with less keep value: {bad[:4]}"
    assert errors == []
    ctx.close()


def _stream_setup(d, tonight):
    fa_g = next(p for p in d["players"] if p["pos"] == "G" and p["owner"] == "freeagent" and p.get("cr_matched")
                and tonight in (p.get("nights") or []))
    fa_g.update(injury=None, games_left=max(fa_g["games_left"], 1), today_start=d["first_start"])
    fa_g["cr_why"]["tonight"] = {"starting": True, "status": "Confirmed"}
    for p in d["players"]:
        if p["pos"] == "G" and p is not fa_g and p.get("cr_why"):
            p["cr_why"].pop("tonight", None)
            p["cr_why"]["p_today"] = 0
    return fa_g


def test_stream_card_never_drops_tonights_starter_of_three_goalies(browser, server, data):  # noqa: F811
    """A team with 3+ healthy goalies streams with a goalie who isn't starting tonight: its lowest-keep goalie is made
    tonight's confirmed starter, so the Stream card must name another. Kills: I09."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, tonight = _thursday(data)
    _stream_setup(d, tonight)
    starters = {}
    for t in d["teams"]:
        gs = sorted([p for p in _mine(d, t, "G") if not p["ir"] and p.get("injury") not in ("OUT", "INJURY_RESERVE", "SUSPENSION")
                     and p.get("cr_why")], key=_keep)
        if len(gs) == 3 and tonight in (gs[0].get("nights") or []):
            gs[0]["cr_why"]["tonight"] = {"starting": True, "status": "Confirmed"}
            starters[t["id"]] = gs[0]["name"]
    if not starters:
        pytest.skip("no 3-goalie team whose weakest goalie plays tonight")
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
    bad = []
    for tid, name in starters.items():
        page.select_option("#team", str(tid))
        st = next((c for c in _cards(page) if c.startswith("Stream")), "")
        m = re.search(r"Your roster is full: drop (.+?) to add him", st)
        if m and m.group(1) == name:
            bad.append((tid, name))
    assert not bad, f"Stream card drops tonight's confirmed starter: {bad}"
    assert errors == []
    ctx.close()


def test_ir_stash_and_stream_never_name_the_same_drop(browser, server, data):  # noqa: F811
    """Handoff 635: with an open IR slot and an injured 1.5+ pts/game free agent, the IR stash's drop is never the Stream
    card's. Kills: I10."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, tonight = _thursday(data)
    _stream_setup(d, tonight)
    for t in d["teams"]:
        t["moves_used"] = 0
    d["ir_slots"] = 3
    for p in d["players"]:
        if p.get("ir"):
            p["ir"] = False  # the IR slots start empty
    hurt = next(p for p in d["players"] if p["owner"] == "freeagent" and p["pos"] != "G" and p.get("cr_season") is not None)
    hurt.update(injury="OUT", cr_fpg=2.5, cr_season=1.5)
    hurt["cr_why"].pop("back", None)
    for p in d["players"]:  # weak healthy free agents: no Best moves or swap subs to spend the moves first
        if p["owner"] in ("freeagent", "waivers") and p["pos"] != "G" and p is not hurt:
            p.update(cr=0.1, cr_season=0.05)
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
    twice, both = [], 0
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        drops = _card_drops(page)
        st = [n for k, v in drops.items() if k.startswith("Stream") for n in v]
        ir = [n for k, v in drops.items() if k.startswith("IR stash") for n in v]
        both += bool(st and ir)
        if st and ir and st[0] == ir[0]:
            twice.append((t["id"], st[0]))
    assert both, "no team with both a Stream drop and an IR stash drop"
    assert not twice, f"IR stash and Stream drop the same player: {twice[:4]}"
    assert errors == []
    ctx.close()


def _best_count(page):
    return page.evaluate(r"""() => { const c = [...document.querySelectorAll('#upgrades .upg')].find(c => /^Best \d* ?moves?\b/.test(c.innerText));
      return c ? [...c.querySelectorAll(':scope > div:not(.sub)')].filter(l => / for |^Add /.test(l.innerText)).length : 0; }""")


def test_nights_without_nhl_games_hold_no_move(browser, server, data):  # noqa: F811
    """Findings 291 (5): Best moves holds one move per night still to come that has NHL games. With Friday and Saturday
    empty (a break) only Sunday holds one, so teams get more Best moves than with games every night. Kills: I21."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    counts = []
    for empty in (False, True):
        d, _ = _thursday(data)
        if empty:
            for n in d["nights"][-3:-1]:
                n["games"] = 0
        ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
        total = 0
        for t in d["teams"]:
            page.select_option("#team", str(t["id"]))
            total += _best_count(page)
        counts.append(total)
        assert errors == []
        ctx.close()
    assert counts[1] > counts[0], counts


def test_drop_list_names_each_replacement_once(browser, server, data):  # noqa: F811
    """Handoff 476: a free agent can be added once, so the Drop list never suggests him for two rows. Kills: I23."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, _ = _thursday(data)
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
    twice = []
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        card = next((c for c in _cards(page) if c.startswith("Drop list")), "")
        names = re.findall(r"replace with (.+?) \(", card)
        if len(names) != len(set(names)):
            twice.append((t["id"], names))
    assert not twice, f"Drop list repeats a replacement: {twice[:3]}"
    assert errors == []
    ctx.close()


def test_trade_check_points_a_week_is_2_1_times_the_season_gap(browser, server, data):  # noqa: F811
    """Handoff 642: a one-for-one skater trade with no fills or drops reads 'About +X points a week' with X = 2.1 x the
    Season gap. Kills: I27."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    import json
    d = json.loads(json.dumps(data))
    team, other = d["teams"][0], d["teams"][1]
    mine = [p for p in _mine(d, team) if p["pos"] not in ("G", "D") and not p["ir"] and p.get("cr_season") is not None]
    theirs = [p for p in _mine(d, other) if p["pos"] not in ("G", "D") and not p["ir"] and p.get("cr_season") is not None]
    for p in mine + theirs:
        p["injury"] = None
    give, get = min(mine, key=lambda p: p["cr_season"]), max(theirs, key=lambda p: p["cr_season"])
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    page.evaluate(f"state.tgive = [{give['id']}]; state.tget = [{get['id']}]; renderTrade()")
    text = page.locator("#trade").inner_text()
    m = re.search(r"About ([+−])([\d.]+) points a week", text)
    assert m, text
    shown = float(m[2]) * (1 if m[1] == "+" else -1)
    assert abs(shown - 2.1 * (get["cr_season"] - give["cr_season"])) <= 0.06, (shown, get["cr_season"], give["cr_season"])
    assert errors == []
    ctx.close()


def _trade(page, give, get):
    import json
    page.evaluate(f"state.tgive = {json.dumps(give)}; state.tget = {json.dumps(get)}; renderTrade()")
    return page.locator("#trade").inner_text()


def _season_gap(text):
    m = re.search(r"Season rating ([+−])([\d.]+) per team game", text)
    assert m, text
    return float(m[2]) * (1 if m[1] == "+" else -1)


def test_trade_partner_with_an_ir_move_needs_no_drop(browser, server, data):  # noqa: F811
    """Handoff 460: a partner at the roster limit who takes two for one doesn't have to drop anyone when he can move an
    OUT player of his into an open IR slot. Kills: I31."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    import json
    d = json.loads(json.dumps(data))
    team, other = d["teams"][0], d["teams"][1]
    mine = [p for p in _mine(d, team) if p["pos"] != "G" and not p["ir"]]
    theirs = _mine(d, other)
    for p in theirs:
        p.update(ir=False, injury=None)
    sk = [p for p in theirs if p["pos"] != "G"]
    d["roster_max"], d["ir_slots"] = len(theirs), 1
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    text = _trade(page, [mine[0]["id"], mine[1]["id"]], [sk[0]["id"]])
    assert "would have to drop a player to take two" in text, text  # the plain case still needs a drop
    page.evaluate(f"state.data.players.find(p => p.id === {sk[-1]['id']}).injury = 'OUT'")
    text = _trade(page, [mine[0]["id"], mine[1]["id"]], [sk[0]["id"]])
    assert "would have to drop" not in text, text
    assert errors == []
    ctx.close()


def test_trade_for_a_player_parked_on_ir_counts_the_spot_he_needs_when_back(browser, server, data):  # noqa: F811
    """Handoff 458: an OUT player you get goes on IR, but for the share of the season he plays he holds a roster spot the
    best free agent could fill: the Season gap drops by that free agent's rating x the share. Kills: I32."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    import json
    d = json.loads(json.dumps(data))
    team, other = d["teams"][0], d["teams"][1]
    mine = _mine(d, team)
    for p in mine:
        p.update(ir=False, injury=None)
    give = next(p for p in mine if p["pos"] not in ("G", "D"))
    r = next(p for p in _mine(d, other) if p["pos"] not in ("G", "D") and not p["ir"])
    r.update(injury="OUT", cr_season=1.0, cr_why={**(r.get("cr_why") or {}), "season_fpg": 1.0})  # plays all the rest
    d["ir_slots"] = 1
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    full = _trade(page, [give["id"]], [r["id"]])
    m = re.search(r"can go on IR, but when he's back \([^)]*\) he needs a roster spot: counts ([\d.]+)", full)
    assert m, full
    page.evaluate(f"state.data.players.find(p => p.id === {r['id']}).cr_why.season_fpg = 1000")  # plays almost none of it
    none = _trade(page, [give["id"]], [r["id"]])
    assert float(m[1]) > 0.05 and abs((_season_gap(none) - _season_gap(full)) - float(m[1])) <= 0.06, (full, none)  # counts is shown to 0.1
    assert errors == []
    ctx.close()


def test_unrated_cards_count_games_left_whatever_the_checkbox(browser, server, data):  # noqa: F811
    """Handoff 637: without ratings the cards score a player by ESPN points a game x games still to play, even when the
    table's "Remaining games only" box is unchecked. Kills: I38."""
    import json
    d = json.loads(json.dumps(data))
    d["rated"] = False
    for p in d["players"]:
        for k in [k for k in p if k.startswith("cr") or k == "nhl_id"]:
            del p[k]
    p = next((p for p in d["players"] if (p.get("ppg") or 0) > 0 and p["games"] > p["games_left"]), None)
    if p is None:
        pytest.skip("needs a player with a game already played this matchup")
    ctx, page, errors = open_page(browser, server, data=d)
    page.uncheck("#left")
    # the page's own games_left (a game that started since the update is already out of it)
    got, ppg, left, games = page.evaluate(f"(p => [score(p), p.ppg, p.games_left, p.games])(state.data.players.find(p => p.id === {p['id']}))")
    assert games > left and abs(got - ppg * left) < 1e-9, (got, ppg, left, games)
    assert errors == []
    ctx.close()


def test_goalie_counts_as_tonights_starter_only_above_even_odds(browser, server, data):  # noqa: F811
    """With no Daily Faceoff confirmation a goalie fills tonight's G slot (open slots, Stream card) only when the site's
    own chance is over 50%: a 35% backup is not tonight's starter, a 65% no. 1 is. Kills: I39."""
    from datetime import datetime
    g = next((p for p in data["players"] if p["pos"] == "G" and p.get("cr_why") is not None), None)
    if g is None:
        pytest.skip("needs rated goalies")
    ctx, page, errors = open_page(browser, server, clock=datetime.fromisoformat(data["updated_utc"].replace("Z", "+00:00")))
    js = (f"(c => {{ const p = structuredClone(state.data.players.find(p => p.id === {g['id']})); delete p.cr_why.tonight;"
          " p.cr_why.p_today = c; p.cr_why.p_today_pre = c; return startsTonight(p); })")
    assert page.evaluate(js + "(0.35)") is False and page.evaluate(js + "(0.65)") is True
    assert errors == []
    ctx.close()


def test_goalie_with_no_expected_starts_explains_his_season(browser, server, data):  # noqa: F811
    """Handoff 636: a goalie expected to start none of his team's remaining games says so in his Season line instead of
    leaving the Season number unexplained. Kills: I45."""
    import json
    from datetime import datetime
    d = json.loads(json.dumps(data))
    g = next((p for p in d["players"] if p["pos"] == "G" and p.get("cr_why") is not None and p.get("cr_season") is not None), None)
    if g is None:
        pytest.skip("needs a rated goalie")
    g["cr_why"]["g_season_share"] = 0
    ctx, page, errors = open_page(browser, server, data=d, clock=datetime.fromisoformat(d["updated_utc"].replace("Z", "+00:00")))
    text = page.evaluate(f"why(state.data.players.find(p => p.id === {g['id']}))")
    assert "(no share of his team's remaining starts expected)" in text, text
    assert errors == []
    ctx.close()


def test_skater_with_no_nhl_record_shows_no_week_formula(browser, server, data):  # noqa: F811
    """Handoff 607: a skater with no NHL record is rated 0 and the details say why, without a 'pts/game x games x 0%'
    formula. Kills: I49."""
    import json
    from datetime import datetime
    d = json.loads(json.dumps(data))
    s = next((p for p in d["players"] if p["pos"] != "G" and p.get("cr_why") is not None and p["games_left"]), None)
    if s is None:
        pytest.skip("needs a rated skater")
    s.update(cr=0.0, cr_fpg=0.0, cr_matched=False)
    s["cr_why"]["no_record"] = True
    ctx, page, errors = open_page(browser, server, data=d, clock=datetime.fromisoformat(d["updated_utc"].replace("Z", "+00:00")))
    text = page.evaluate(f"why(state.data.players.find(p => p.id === {s['id']}))")
    assert "Not found in NHL stats" in text and "pts/game ×" not in text, text
    assert errors == []
    ctx.close()


def test_likely_starter_details_say_how_it_is_counted(browser, server, data):  # noqa: F811
    """Handoff 634: a rostered goalie Daily Faceoff lists as Likely to start on the next night still to play reads
    'likely, counted as at least 95% to start' (and a Likely non-starter 'cut to 5% or less'), not 'not yet counted'.
    Kills: I50."""
    from datetime import datetime
    clock = datetime.fromisoformat(data["updated_utc"].replace("Z", "+00:00"))
    ctx, page, errors = open_page(browser, server, clock=clock)
    gid = page.evaluate("""(() => { const sp = state.data.nights.find(n => !n.past)?.sp;
      const g = state.data.players.find(p => p.pos === "G" && p.cr_why && p.owner !== "waivers" && p.nights.includes(sp) && !started(p));
      if (!g) return null; g.cr_why.tonight = {status: "Likely", starting: true}; return g.id; })()""")
    if gid is None or page.evaluate("stale(state.data)"):
        pytest.skip("no goalie with a game still to start on the next night")
    text = page.evaluate(f"why(state.data.players.find(p => p.id === {gid}))")
    assert "likely, counted as at least 95% to start" in text, text
    page.evaluate(f"state.data.players.find(p => p.id === {gid}).cr_why.tonight.starting = false")
    text = page.evaluate(f"why(state.data.players.find(p => p.id === {gid}))")
    assert "likely, so his chance is cut to 5% or less" in text, text
    assert errors == []
    ctx.close()


def test_stream_card_keeps_a_third_goalie_who_is_his_teams_no1(browser, server, data):  # noqa: F811
    """Handoffs 805, 806: the Stream card's third-goalie drop skips a goalie who started over 0.55 of his team's last 10
    games (dropping one for one night lost 3.5 points over the next month) and drops a skater instead; the Goalies card
    says to keep him. A backup (0.3) is still the drop. The model's Season share is set the other way round, so only the
    last-10 share decides."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    seen = {}
    for share in (0.3, 0.7):
        d, tonight = _thursday(data)
        _stream_setup(d, tonight)
        thirds = {}
        for t in d["teams"]:
            gs = sorted([p for p in _mine(d, t, "G") if not p["ir"] and p.get("injury") not in ("OUT", "INJURY_RESERVE", "SUSPENSION")
                         and p.get("cr_why")], key=_keep)
            if len(gs) == 3:
                gs[0]["cr_why"]["share10"], gs[0]["cr_why"]["g_season_share"] = share, 1 - share
                thirds[t["id"]] = gs[0]["name"]
        if not thirds:
            pytest.skip("no 3-goalie team")
        ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
        for tid, name in thirds.items():
            page.select_option("#team", str(tid))
            cards = _cards(page)
            st = next((c for c in cards if c.startswith("Stream")), "")
            m = re.search(r"Your roster is full: drop (.+?)( \(or trade him: see Trade ideas\))? to add him", st)
            goalies = next((c for c in cards if c.startswith("Goalies") and "third goalie" in c), "")
            seen.setdefault(tid, {})[share] = (m.group(1) if m else None, goalies, name)
        assert errors == []
        ctx.close()
    backups = [tid for tid, v in seen.items() if v[0.3][0] == v[0.3][2]]
    assert backups, seen  # some team's stream drops its backup third goalie
    for tid in backups:
        drop, goalies, name = seen[tid][0.7]
        assert drop and drop != name, (tid, drop, name)
        assert f"{name}, your third goalie, starts most of his team's games, so keep him" in goalies, goalies
