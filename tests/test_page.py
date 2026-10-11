"""Load index.html in a real browser with the committed data and click through it.

Catches a page that parses but throws while rendering (a bad field, a typo in a card), which the other tests can't see.
Runs in CI (the Tests workflow installs Playwright's Chromium); skipped locally when Playwright isn't installed."""
import functools
import http.server
import json
import os
import threading

import pytest

from conftest import ROOT

sync_api = pytest.importorskip("playwright.sync_api")
LOCAL_CHROMIUM = "/opt/pw-browsers/chromium"  # Claude cloud containers ship this; CI downloads its own


@pytest.fixture(scope="module")
def server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/index.html"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        kw = {"executable_path": LOCAL_CHROMIUM} if os.path.exists(LOCAL_CHROMIUM) else {}
        b = p.chromium.launch(**kw)
        yield b
        b.close()


@pytest.fixture(scope="module")
def data():
    return json.loads((ROOT / "data" / "players.json").read_text())


def open_page(browser, url, data=None, phone=False):
    """A fresh page (empty localStorage) that records every JS error. data replaces players.json if given."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844} if phone else {"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
    page.on("response", lambda r: errors.append(f"{r.status} {r.url}") if r.status >= 400 and not r.url.endswith("favicon.ico") else None)
    if data is not None:
        page.route("**/data/players.json", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(data)))
    page.goto(url)
    try:
        page.wait_for_function("document.getElementById('updated').textContent.startsWith('Matchup')", timeout=10000)
    except sync_api.TimeoutError:
        raise AssertionError(f"page never finished loading: {errors}") from None
    return ctx, page, errors


def rows(page):
    return page.locator("#rows tr[data-id]").count()


def test_loads_and_lists_free_agents(browser, server):
    ctx, page, errors = open_page(browser, server)
    assert rows(page) > 0
    assert page.locator("#nights > *").count() == 7
    assert "Pick your team" in page.locator("#upgrades").inner_text()
    assert errors == []
    ctx.close()


def test_every_team_view_and_details(browser, server, data):
    """Pick each team: roster view, open-slot strip, upgrade/drop/stream cards. Then open a few rows' details."""
    ctx, page, errors = open_page(browser, server)
    for t in data["teams"]:
        page.select_option("#team", str(t["id"]))
        assert rows(page) > 0, t["name"]
        assert "Pick your team" not in page.locator("#upgrades").inner_text(), t["name"]
        assert errors == [], (t["name"], errors)
    for i in range(min(5, rows(page))):
        page.locator("#rows tr[data-id]").nth(i).click()
        assert page.locator("#rows tr.why").count() == 1
    assert errors == []
    ctx.close()


def test_filters_and_sorting(browser, server):
    ctx, page, errors = open_page(browser, server)
    for pos in ["C", "LW", "RW", "F", "D", "G", ""]:
        page.select_option("#pos", pos)
    page.select_option("#who", "every")
    page.fill("#q", "a")
    page.fill("#ming", "2")
    page.click("#left")
    page.click("#healthy")
    for i in range(page.locator("#head th").count()):
        page.locator("#head th").nth(i).click()
        page.locator("#head th").nth(i).click()  # both directions
    page.fill("#q", "zzzz-nobody")
    assert "No players match" in page.locator("#rows").inner_text()
    assert errors == []
    ctx.close()


def test_remembers_choices_across_reloads(browser, server, data):
    ctx, page, errors = open_page(browser, server)
    team = str(data["teams"][0]["id"])
    page.select_option("#team", team)
    page.select_option("#pos", "D")
    page.reload()
    page.wait_for_function("document.getElementById('updated').textContent.startsWith('Matchup')")
    assert page.input_value("#team") == team and page.input_value("#pos") == "D"
    assert errors == []
    ctx.close()


def test_phone_layout_has_no_sideways_scroll(browser, server, data):
    ctx, page, errors = open_page(browser, server, phone=True)
    page.select_option("#team", str(data["teams"][0]["id"]))
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert errors == []
    ctx.close()


def test_page_works_without_ratings(browser, server, data):
    """When the rating step fails, fetch.py publishes rated=false without cr fields; the page must fall back to ESPN numbers."""
    unrated = json.loads(json.dumps(data))
    unrated["rated"] = False
    for p in unrated["players"]:
        for k in [k for k in p if k.startswith("cr") or k == "nhl_id"]:
            del p[k]
    ctx, page, errors = open_page(browser, server, data=unrated)
    assert rows(page) > 0
    assert "Week" not in [t.strip() for t in page.locator("#head th").all_inner_texts()]
    page.select_option("#team", str(data["teams"][0]["id"]))
    page.locator("#rows tr[data-id]").first.click()
    assert errors == [], errors
    ctx.close()


def test_trade_check(browser, server, data):
    """Give one player and get one: the panel prices the trade; an uneven trade names the free agent or drop; × removes."""
    ctx, page, errors = open_page(browser, server)
    page.select_option("#team", str(data["teams"][0]["id"]))
    give = page.locator("#tgive option").nth(1).get_attribute("value")
    page.select_option("#tgive", give)
    assert "points a week" not in page.locator("#trade").inner_text()
    page.select_option("#tget", page.locator("#tget option").nth(1).get_attribute("value"))
    assert "points a week" in page.locator("#trade").inner_text()
    page.select_option("#tget", page.locator("#tget option").nth(1).get_attribute("value"))
    assert "drop" in page.locator("#trade").inner_text()
    page.locator("#trade button[data-rm]").first.click()
    assert page.locator("#trade button[data-rm]").count() == 2
    page.select_option("#team", str(data["teams"][1]["id"]))
    assert page.locator("#trade button[data-rm]").count() == 0  # a new team starts a new trade
    assert errors == []
    ctx.close()


def test_no_moves_left_says_so(browser, server, data):
    """A team at its move limit sees its adds as next Monday's targets (findings 146)."""
    capped = json.loads(json.dumps(data))
    capped["move_limit"] = capped.get("move_limit") or 7
    for t in capped["teams"]:
        t["moves_used"] = capped["move_limit"]
    ctx, page, errors = open_page(browser, server, data=capped)
    found = False
    for t in data["teams"]:
        page.select_option("#team", str(t["id"]))
        found = found or "next Monday's targets" in page.locator("#upgrades").inner_text()
    assert found
    assert errors == []
    ctx.close()


def test_games_that_already_started_drop_out(browser, server, data):
    """A game today that started after the data run is no longer left to play (findings 178)."""
    from datetime import datetime, timedelta, timezone
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(timezone.utc)
    late = json.loads(json.dumps(data))
    late["updated_utc"] = iso(now - timedelta(hours=2))
    late["updated_date"] = datetime.now(timezone(timedelta(hours=-4))).date().isoformat()  # not stale
    sk = next(p for p in late["players"] if p["pos"] != "G" and p["games_left"] > 0 and (p.get("cr") or 0) > 0)
    later = next(p for p in late["players"] if p["pos"] != "G" and p["games_left"] > 0 and p["id"] != sk["id"])
    sk["today_start"], later["today_start"] = iso(now - timedelta(hours=1)), iso(now + timedelta(hours=3))
    # his last game this week started: nothing is left, not a rounding leftover
    last = next(p for p in late["players"] if p["games_left"] == 1 and (p.get("cr") or 0) > 0 and p["id"] not in (sk["id"], later["id"]))
    last["today_start"] = iso(now - timedelta(hours=1))
    ctx, page, errors = open_page(browser, server, data=late)
    got = lambda pid: page.evaluate(f"(() => {{ const p = state.data.players.find(p => p.id === {pid}); return [p.games_left, p.cr]; }})()")
    assert got(sk["id"]) == [sk["games_left"] - 1, pytest.approx(max(0, sk["cr"] - sk["cr_fpg"] * min(sk["cr_dress_next"] or 0, sk["cr_games"])), abs=0.02)]
    assert got(later["id"])[0] == later["games_left"]
    assert page.evaluate(f"(() => {{ const p = state.data.players.find(p => p.id === {last['id']}); return [p.games_left, p.cr, p.cr_games]; }})()") == [0, 0, 0]
    # the rest of the row follows (findings 182): no stale percentile, tonight's dot greyed, Open skips tonight
    assert page.evaluate(f"state.data.players.find(p => p.id === {sk['id']}).cr_pct") is None
    tonight = next(n for n in late["nights"] if not n["past"])
    gone = page.evaluate(f"(() => {{ const p = state.data.players.find(p => p.id === {sk['id']}); const n = state.data.nights.find(n => !n.past); return [goneTonight(p, n), dots(p)]; }})()")
    assert gone[0] is True
    if tonight["sp"] in sk["nights"]:
        assert gone[1].count("past") >= sum(n["past"] for n in late["nights"]) + 1
    for t in data["teams"]:
        page.select_option("#team", str(t["id"]))
    assert errors == []
    ctx.close()


def test_upgrades_roster_fixes(browser, server, data):
    """Healthy player in IR, injured players in Mid-week swaps, Best moves capped by moves left (findings 181)."""
    d = json.loads(json.dumps(data))
    team = d["teams"][0]
    mine = [p for p in d["players"] if str(p.get("team_id")) == str(team["id"]) and p["pos"] != "G"]
    for p in mine:
        p["ir"] = False
    healthy_ir, hurt = mine[0], mine[1]
    healthy_ir.update(ir=True, injury=None)
    hurt.update(injury="OUT", games_left=max(hurt["games_left"], 1))
    d["ir_slots"] = 1  # the IR slot is taken, so no "move him to IR" hint
    d["move_limit"], team["moves_used"] = 7, 6
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    text = page.locator("#upgrades").inner_text()
    assert "Activate from IR" in text and healthy_ir["name"] in text
    assert text.index("Activate from IR") < text.index("Mid-week swaps")
    swaps = text[text.index("Mid-week swaps"):].split("\n\n")[0]
    assert hurt["name"] in swaps and "IR-eligible" not in swaps
    assert "Best 2 moves" not in text  # one move left
    assert errors == []
    ctx.close()


def test_stream_card_follows_the_first_puck(browser, server, data):
    """Before the day's first game the card says when adds stop counting; after it, adds count from tomorrow (findings 179)."""
    from datetime import datetime, timedelta, timezone
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(timezone.utc)
    if not data.get("rated") or not any(not n["past"] for n in data["nights"]):
        pytest.skip("needs rated data with a night left")
    for first, want in ((now + timedelta(hours=3), "Add before the first game at"), (now - timedelta(hours=1), "plays from tomorrow")):
        d = json.loads(json.dumps(data))
        d["updated_date"] = datetime.now(timezone(timedelta(hours=-4))).date().isoformat()  # not stale
        d["first_start"], d["move_limit"] = iso(first), None
        for p in d["players"]:
            if p["pos"] == "G" and p.get("cr_why"):
                p["cr_why"].pop("tonight", None)  # every team has two open G slots tonight
                p["cr_why"]["p_today"] = 0
        ctx, page, errors = open_page(browser, server, data=d)
        page.select_option("#team", str(d["teams"][0]["id"]))
        assert want in page.locator("#upgrades").inner_text()
        assert errors == []
        ctx.close()


def test_goalie_in_net_fills_his_slot_and_swaps_need_games(browser, server, data):
    """A started goalie still counts as starting tonight; Mid-week swaps never offer a free agent with no games left (findings 183)."""
    from datetime import datetime, timedelta, timezone
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(timezone.utc)
    if not data.get("rated") or not any(not n["past"] for n in data["nights"]):
        pytest.skip("needs rated data with a night left")
    d = json.loads(json.dumps(data))
    d["updated_date"] = datetime.now(timezone(timedelta(hours=-4))).date().isoformat()
    d["first_start"], d["move_limit"] = iso(now + timedelta(hours=3)), None
    team = next(t for t in d["teams"] if sum(p["pos"] == "G" and str(p.get("team_id")) == str(t["id"]) for p in d["players"]) >= 2)
    goalies = [p for p in d["players"] if p["pos"] == "G" and str(p.get("team_id")) == str(team["id"])]
    for p in goalies:
        p.update(ir=False, injury=None)
        p["cr_why"] = dict(p.get("cr_why") or {}, p_today=0, tonight={"starting": False, "status": "Confirmed"})
    goalies[0].update(today_start=iso(now - timedelta(hours=1)))
    goalies[0]["cr_why"]["tonight"] = {"starting": False, "status": "Likely"}  # in net, so his p_today is 0
    goalies[0]["cr_why"]["p_today_pre"] = 0.8  # the site had him starting, whatever Daily Faceoff's unconfirmed guess says
    goalies[1]["cr_why"]["tonight"]["starting"] = True
    # someone sat, and no free agent has a game left
    sk = next(p for p in d["players"] if str(p.get("team_id")) == str(team["id"]) and p["pos"] != "G")
    sk.update(sat_last=True, dfo_out=None, injury=None, ir=False, games_left=max(sk["games_left"], 1))
    for p in d["players"]:
        if p["owner"] in ("freeagent", "waivers"):
            p["games_left"] = 0
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    text = page.locator("#upgrades").inner_text()
    assert "open G slot" not in text  # both G slots filled, one goalie already in net
    swaps = text[text.index("Mid-week swaps"):].split("\n\n")[0]
    assert "gm left" not in swaps
    assert errors == []
    ctx.close()


def _now_et():
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc), datetime.now(timezone(timedelta(hours=-4)))


def _mine(d, team, pos=None):
    return [p for p in d["players"] if str(p.get("team_id")) == str(team["id"]) and (pos is None or (p["pos"] == "G") == (pos == "G"))]


def test_week_ties_sort_by_keep_value(browser, server, data):
    """From Sunday's first puck every free agent's Week is 0.0: the table lists them by keep value (findings 211)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = json.loads(json.dumps(data))
    for p in d["players"]:
        if p["owner"] in ("freeagent", "waivers"):
            p["cr"], p["cr_games"], p["games_left"] = 0, 0, 0
    ctx, page, errors = open_page(browser, server, data=d)
    keeps = page.evaluate("[...document.querySelectorAll('#rows tr[data-id]')].slice(0, 30).map(r => keep(state.data.players.find(p => p.id === +r.dataset.id)))")
    assert len(keeps) > 5 and keeps == sorted(keeps, reverse=True)
    assert errors == []
    ctx.close()


def test_trade_check_and_ideas_keep_roster_shape(browser, server, data):
    """A 2-for-1 never drops a goalie (unless goalies are in the trade); trade ideas leave the partner 11-13 F and 6-8 D (findings 211)."""
    import re
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = json.loads(json.dumps(data))
    team = next(t for t in d["teams"] if len([p for p in _mine(d, t, "G") if not p.get("ir")]) >= 2)
    g = next(p for p in _mine(d, team, "G") if not p.get("ir"))
    g.update(cr=0, cr_season=0, injury=None)  # lowest keep value on the team
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    give = next(p for p in _mine(d, team) if p["pos"] != "G" and not p.get("ir"))
    page.select_option("#tgive", str(give["id"]))
    for _ in range(2):
        page.select_option("#tget", page.locator("#tget option:not([value=''])").first.get_attribute("value"))
    text = page.locator("#trade").inner_text()
    assert "drop" in text and g["name"] not in text.split("drop", 1)[1]
    # trade ideas: the partner keeps a legal shape
    by_name = {p["name"]: p for p in d["players"]}
    names = {t["name"]: t["id"] for t in d["teams"]}
    grp = lambda p: "G" if p["pos"] == "G" else "D" if p["pos"] == "D" else "F"
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        for line in page.locator("#upgrades").inner_text().splitlines():
            m = re.match(r"Offer (.+?)(?: \((F|D)\))? for (.+?)(?: \((F|D)\))? \((.+?)\) ·", line)
            if not m or not m.group(2):
                continue
            mine_p, theirs_p, partner = by_name[m.group(1)], by_name[m.group(3)], names[m.group(5)]
            healthy = [p for p in d["players"] if str(p.get("team_id")) == str(partner) and p["pos"] != "G" and not p.get("ir") and not p.get("injury")]
            f = sum(grp(p) == "F" for p in healthy) + (grp(mine_p) == "F") - (grp(theirs_p) == "F")
            dd = len(healthy) - sum(grp(p) == "F" for p in healthy) + (grp(mine_p) == "D") - (grp(theirs_p) == "D")
            assert 11 <= f <= 13 and 6 <= dd <= 8, line
    assert errors == []
    ctx.close()


def test_tonight_card_counts_started_players(browser, server, data):
    """Players whose game started already fill slots; only the rest are offered for the bench (findings 211)."""
    from datetime import timedelta
    if not data.get("rated") or not any(not n["past"] for n in data["nights"]):
        pytest.skip("needs rated data with a night left")
    now, et = _now_et()
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    d = json.loads(json.dumps(data))
    d["updated_date"], d["updated_utc"] = et.date().isoformat(), iso(now)
    sp = next(n for n in d["nights"] if not n["past"])["sp"]
    team = d["teams"][0]
    sk = [p for p in _mine(d, team) if p["pos"] != "G"]
    for p in sk:
        p.update(ir=False, injury=None, sat_last=False, dfo_out=None, today_start=None)
        p["nights"] = sorted(set(p["nights"]) | {sp})
    fwd = [p for p in sk if p["pos"] != "D"]
    if len(fwd) < 10 or len(sk) - len(fwd) < 5:
        pytest.skip("team too small")
    started = fwd[:3]
    for p in started:
        p["today_start"] = iso(now - timedelta(minutes=30))
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    card = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].map(c => c.innerText).find(t => t.startsWith('Tonight')) || ''")
    benched = [p for p in sk if p["name"] in card.split("bench these", 1)[-1]]
    assert len(benched) == len(sk) - 15
    assert not any(p in started for p in benched)
    assert errors == []
    ctx.close()


def test_goalies_card_after_first_puck(browser, server, data):
    """After the first puck the Goalies card says a stream counts from tomorrow, not tonight (findings 211)."""
    from datetime import timedelta
    if not data.get("rated") or not any(not n["past"] for n in data["nights"]):
        pytest.skip("needs rated data with a night left")
    now, et = _now_et()
    d = json.loads(json.dumps(data))
    d["updated_date"], d["move_limit"] = et.date().isoformat(), None
    d["first_start"] = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    team = d["teams"][0]
    gs = [p for p in d["players"] if p["pos"] == "G"]
    for p in _mine(d, team, "G"):
        p["team_id"] = None
    for p in [p for p in gs if p["owner"] in ("freeagent", "waivers")][:3]:
        p.update(team_id=team["id"], owner=str(team["id"]), ir=False, injury=None)
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    text = page.locator("#upgrades").inner_text()
    goalies = text[text.index("Goalies ·"):]
    assert "open tonight" not in goalies and ("from tomorrow" in goalies or "next matchup" in goalies)
    assert errors == []
    ctx.close()


def test_monday_before_first_run_resets_moves(browser, server, data):
    """Data from a matchup that already ended: moves are reset, not 'no moves left' (findings 211)."""
    from datetime import timedelta
    if not data.get("rated"):
        pytest.skip("needs rated data")
    now, et = _now_et()
    yday = (et - timedelta(days=1)).date()
    d = json.loads(json.dumps(data))
    d["updated_date"] = yday.isoformat()
    d["nights"][-1]["date"] = f"Sun {yday.day}"
    d["move_limit"] = 7
    for t in d["teams"]:
        t["moves_used"] = 7
    ctx, page, errors = open_page(browser, server, data=d)
    assert page.evaluate("weekOver(state.data)") is True
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        assert "next Monday's targets" not in page.locator("#upgrades").inner_text()
    assert errors == []
    ctx.close()


def test_phone_table_fits_and_cards_fold(browser, server, data):
    """On a phone the table fits its box, and card explainers and long cards fold (handoffs 106-107, findings 214)."""
    ctx, page, errors = open_page(browser, server, phone=True)
    for t in data["teams"]:
        page.select_option("#team", str(t["id"]))
        wrap = page.evaluate("(() => { const w = document.querySelector('.tablewrap'); return [w.scrollWidth, w.clientWidth]; })()")
        assert wrap[0] <= wrap[1] + 1, (t["id"], wrap)
        # no explainer left unfolded, and folded long cards start closed
        assert page.locator("#upgrades .upg > .drop").count() == 0
        assert page.evaluate("[...document.querySelectorAll('#upgrades details.fold')].every(d => !d.open)")
    assert errors == []
    ctx.close()


def test_gains_say_their_unit(browser, server, data):
    """Gains read "+x keep", or "+x Season" with no moves left (handoff 108)."""
    import re
    ctx, page, errors = open_page(browser, server)
    gains = []
    for t in data["teams"]:
        page.select_option("#team", str(t["id"]))
        gains += re.findall(r"\+\d+\.\d( \w+)?", " ".join(page.locator("#upgrades .up").all_inner_texts()))
    assert gains and all(g in (" keep", " Season") for g in gains), sorted(set(gains))  # a team at its limit sees Season
    ctx.close()
    capped = json.loads(json.dumps(data))
    capped["move_limit"] = capped.get("move_limit") or 7
    for t in capped["teams"]:
        t["moves_used"] = capped["move_limit"]
    ctx, page, errors2 = open_page(browser, server, data=capped)
    gains = []
    for t in data["teams"]:
        page.select_option("#team", str(t["id"]))
        gains += re.findall(r"\+\d+\.\d( \w+)?", " ".join(page.locator("#upgrades .up").all_inner_texts()))
    assert gains and all(g == " Season" for g in gains), gains[:5]
    assert errors == errors2 == []
    ctx.close()


def test_waiver_claim_note(browser, server, data):
    """Waiver players in Upgrades come with a note that a losing claim is free and the team's waiver order (handoff 101)."""
    d = json.loads(json.dumps(data))
    for p in d["players"]:
        if p["owner"] == "freeagent":
            p["owner"], p["clears"] = "waivers", "Tue 13"
    for i, t in enumerate(d["teams"]):
        t["waiver_rank"], t["moves_used"] = i + 1, 0
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(d["teams"][2]["id"]))
    note = page.locator("#upgrades .wvnote").inner_text()
    assert "A losing claim uses no move" in note and f"#3 of {len(d['teams'])}" in note
    # without the waiver order the note still shows, minus the rank
    for t in d["teams"]:
        t["waiver_rank"] = None
    ctx.close()
    ctx, page, errors2 = open_page(browser, server, data=d)
    page.select_option("#team", str(d["teams"][2]["id"]))
    assert "waiver order" not in page.locator("#upgrades .wvnote").inner_text()
    assert errors == errors2 == []
    ctx.close()
