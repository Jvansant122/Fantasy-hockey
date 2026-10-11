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


def open_page(browser, url, data=None, phone=False, clock=None):
    """A fresh page (empty localStorage) that records every JS error. data replaces players.json if given.
    clock: a datetime the page's clock stands still at, or "install" for a fake clock the test moves on."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844} if phone else {"width": 1280, "height": 900})
    page = ctx.new_page()
    if clock == "install":
        page.clock.install()
    elif clock is not None:
        page.clock.set_fixed_time(clock)
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
    # the same players sitting on ESPN's bench hold no slot: the rest get all 15 (findings 233)
    for p in started:
        p["bench"] = True
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    card = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].map(c => c.innerText).find(t => t.startsWith('Tonight')) || ''")
    benched = [p for p in sk if p["name"] in card.split("bench these", 1)[-1]]
    assert len(benched) == (len(sk) - 3 - 15 if card else 0)
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
        # long explainers fold, the lines managers act on (stream deadline, bench these, your weakest) stay (findings 233)
        assert page.evaluate("""[...document.querySelectorAll('#upgrades .upg')].every(c => {
          const t = c.querySelector(':scope > b, :scope > details > summary > b')?.textContent || '';
          return !!c.querySelector(':scope > .drop') !== /^(Mid-week swaps|Best|Drop list|Activate from IR|Two-week)/.test(t)
            || c.classList.contains('fold') || !c.querySelector('.drop'); })""")
        # tags wrap below the name instead of being cut off (findings 232)
        cut = page.evaluate("""[...document.querySelectorAll('#rows td:first-child .tags')].filter(t => {
          const td = t.closest('td').getBoundingClientRect(); return [...t.children].some(c => c.getBoundingClientRect().right > td.right + 1); }).length""")
        assert cut == 0
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


def test_last_night_before_first_puck_uses_every_move(browser, server, data):
    """On the matchup's last night before the first game, Best moves lists every move left, since they expire (findings 231)."""
    from datetime import datetime, timedelta, timezone
    if not data.get("rated"):
        pytest.skip("needs rated data")
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(timezone.utc)
    for first, want_note in ((now + timedelta(hours=3), True), (now - timedelta(hours=1), False)):
        d = json.loads(json.dumps(data))
        d["updated_date"] = datetime.now(timezone(timedelta(hours=-4))).date().isoformat()  # not stale
        d["final_matchup"] = False
        for n in d["nights"]:
            n["past"] = n is not d["nights"][-1]
        d["first_start"] = iso(first)
        for p in d["players"]:
            p["today_start"] = iso(first) if d["nights"][-1]["sp"] in (p.get("nights") or []) else None
        d["move_limit"] = 7
        for t in d["teams"]:
            t["moves_used"] = 1
        ctx, page, errors = open_page(browser, server, data=d)
        most = 0
        for t in d["teams"]:
            page.select_option("#team", str(t["id"]))
            text = page.locator("#upgrades").inner_text()
            assert ("expire tonight" in text) == want_note, (t["id"], first)
            card = page.locator("#upgrades .upg", has_text="largest keep-value gains")
            if card.count():
                most = max(most, card.locator(":scope > div").count())
        assert (most > 2) == want_note, most
        assert errors == []
        ctx.close()


def test_swaps_and_best_moves_share_the_moves_left(browser, server, data):
    """Mid-week swaps and Best moves never drop the same player or offer more moves than are left (findings 233)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = json.loads(json.dumps(data))
    d["move_limit"] = 7
    for t in d["teams"]:
        t["moves_used"] = 5  # 2 left
    for t in d["teams"]:
        sk = sorted((p for p in _mine(d, t) if p["pos"] != "G" and not p["ir"]), key=lambda p: p.get("cr_season") or 0)
        for p in sk[:3]:  # the weakest three are out: Best moves would want to drop them too
            p.update(injury="OUT", games_left=max(p["games_left"], 1))
    ctx, page, errors = open_page(browser, server, data=d)
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        swaps = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].find(c => c.innerText.startsWith('Mid-week swaps'))?.innerText || ''")
        best = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].find(c => /^Best/.test(c.innerText))?.innerText || ''")
        n_sub = swaps.count("→")
        n_best = len([l for l in best.split("\n")[1:] if " for " in l])
        assert n_sub + n_best <= 2, (t["id"], swaps, best)
        swapped = [l.split("→")[0] for l in swaps.split("\n") if "→" in l]
        for line in best.split("\n")[1:]:
            assert not any(name.split(" (")[0].strip()[:12] and line.split(" for ")[-1].startswith(name.strip()[:12]) for name in swapped), (line, swapped)
    assert errors == []
    ctx.close()


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _card(page, title):
    """The inner text of the Upgrades card whose title starts with title, or ''."""
    return page.evaluate(f"[...document.querySelectorAll('#upgrades .upg')].map(c => c.innerText).find(t => t.startsWith({json.dumps(title)})) || ''")


def _fresh(data):
    """A copy of the data dated today in New York, so nothing reads it as stale."""
    d = json.loads(json.dumps(data))
    d["updated_date"] = _now_et()[1].date().isoformat()
    return d


def test_roster_size_rules(browser, server, data):
    """With roster_max (handoffs 128-131): a full roster needs a drop to activate or stash; an IR-eligible player moves to IR
    instead of the Drop list; an open spot means an add with no drop."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = json.loads(json.dumps(data))
    team = d["teams"][0]
    roster = _mine(d, team)
    sk = [p for p in roster if p["pos"] != "G"]
    for p in roster:
        p.update(ir=False, injury=None, sat_last=False)
    healthy_ir, hurt = sk[0], sk[1]
    healthy_ir["ir"] = True
    hurt["injury"] = "INJURY_RESERVE"
    d["ir_slots"] = 2  # one open
    non_ir = len(roster) - 1
    d["roster_max"], d["move_limit"], team["moves_used"] = non_ir, 7, 0
    fa = next(p for p in d["players"] if p["owner"] == "freeagent" and p["pos"] != "G")  # worth stashing
    fa.update(injury="OUT", cr_fpg=2.0, cr_season=1.0)
    fa["cr_why"] = {k: v for k, v in (fa.get("cr_why") or {}).items() if k != "back"}
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    activate = _card(page, "Activate from IR")
    assert "Your roster is full: drop" in activate
    named = activate.split("Your roster is full: drop ", 1)[1].split(" first", 1)[0]
    drops = _card(page, "Drop list")
    lines = [l for l in drops.split("\n") if hurt["name"] in l]
    assert len(lines) == 1 and "Move to IR" in lines[0]
    assert named != hurt["name"] and named in drops
    best = _card(page, "Best").split("\n")[1:]
    assert best and best[0].startswith("Add ") and "no drop needed" in best[0]  # the open IR slot frees a roster spot
    assert all(not l.startswith("Add ") for l in best[1:])
    stash = _card(page, "IR stash")
    assert "Your roster is full" in stash and "(2 moves)" in stash
    assert errors == []
    ctx.close()
    # full, nothing to move to IR, one move left: no open spot, and no stash that needs two moves
    hurt["injury"] = None
    team["moves_used"] = 6
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    assert _card(page, "IR stash") == ""
    assert all(not l.startswith("Add ") for l in _card(page, "Best").split("\n")[1:])
    assert errors == []
    ctx.close()
    # an open spot: the first add needs no drop, and activating needs none either
    d["roster_max"], team["moves_used"] = non_ir + 1, 0
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    assert "roster is full" not in _card(page, "Activate from IR")
    best = _card(page, "Best").split("\n")[1:]
    assert best and best[0].startswith("Add ")
    assert "roster is full" not in _card(page, "IR stash")
    assert errors == []
    ctx.close()


def test_trade_deadline_hides_trades(browser, server, data):
    """After ESPN's trade deadline neither Trade ideas nor Trade check offer a trade (handoff 132)."""
    import time
    d = json.loads(json.dumps(data))
    d["trade_deadline"] = int(time.time() * 1000) - 86400000
    ctx, page, errors = open_page(browser, server, data=d)
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        assert "Trade ideas" not in page.locator("#upgrades").inner_text()
        assert "deadline has passed" in page.locator("#trade").inner_text()
        assert page.locator("#tpick").is_hidden()
    assert errors == []
    ctx.close()
    d["trade_deadline"] = int(time.time() * 1000) + 86400000
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(d["teams"][0]["id"]))
    assert page.locator("#tpick").is_visible() and "deadline" not in page.locator("#trade").inner_text()
    assert errors == []
    ctx.close()


def test_playoff_round_and_final_wording(browser, server, data):
    """A round's first week says 'next round'; the final with no moves offers nothing; its last night adds nothing (handoffs 137-138)."""
    from datetime import timedelta
    if not data.get("rated"):
        pytest.skip("needs rated data")
    # the first week of a two-week round, its Sunday: 8 nights left
    d = _fresh(data)
    week1 = [dict(n, past=i < 6) for i, n in enumerate(d["nights"])]
    d["nights"] = week1 + [dict(n, sp=n["sp"] + 7, past=False, light=False) for n in week1]
    d["matchup_weeks"], d["move_limit"], d["final_matchup"] = 2, 14, False
    for t in d["teams"]:
        t["moves_used"] = 14
    ctx, page, errors = open_page(browser, server, data=d)
    found = False
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        text = page.locator("#upgrades").inner_text()
        assert "next Monday's targets" not in text
        found = found or "targets for the next round" in text
    assert found and errors == []
    ctx.close()
    # the final with every move used: no adds, no 'next Monday', no keep-for-later text
    d = _fresh(data)
    d["final_matchup"], d["move_limit"] = True, 7
    for t in d["teams"]:
        t["moves_used"] = 7
    ctx, page, errors = open_page(browser, server, data=d)
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        text = page.locator("#upgrades").inner_text()
        assert "season ends with this matchup" in text and "next Monday" not in text
        assert not any(_card(page, c) for c in ("Best", "Forwards", "Defense", "Goalies · your third"))
        assert "goes on waivers" not in text and "two more weeks" not in text
    assert errors == []
    ctx.close()
    # the final's last night after the first puck: a streamed goalie can't count
    if not any(not n["past"] for n in data["nights"]):
        return
    now, _ = _now_et()
    d = _fresh(data)
    d["final_matchup"], d["move_limit"], d["first_start"] = True, None, _iso(now - timedelta(hours=1))
    for n in d["nights"]:
        n["past"] = n is not d["nights"][-1]
    for p in d["players"]:
        if p["pos"] == "G" and p.get("cr_why"):
            p["cr_why"].pop("tonight", None)
            p["cr_why"]["p_today"] = 0
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(d["teams"][0]["id"]))
    assert "nothing added now counts" in _card(page, "Stream a goalie tonight")
    assert errors == []
    ctx.close()


def test_after_the_season(browser, server, data):
    """After the final's last day the page says the season ended and hides the nights, Upgrades and Trade check (handoff 139)."""
    d = json.loads(json.dumps(data))
    d["season_over"], d["season_end"] = True, "Sun Apr 4"
    ctx, page, errors = open_page(browser, server, data=d)
    assert page.locator("#stale").is_visible() and "The fantasy season ended Sun Apr 4" in page.locator("#stale").inner_text()
    page.select_option("#team", str(d["teams"][0]["id"]))
    assert page.locator("#upgcard").is_hidden() and page.locator("#tradecard").is_hidden() and page.locator("#nightscard").is_hidden()
    assert rows(page) > 0
    assert errors == []
    ctx.close()


def test_open_tab_reloads(browser, server, data):
    """An open tab reloads a minute after a game starts, and when it comes back after 10+ minutes (handoff 120)."""
    from datetime import timedelta
    now, _ = _now_et()
    d = json.loads(json.dumps(data))
    d["players"][0]["today_start"] = _iso(now + timedelta(seconds=30))
    ctx, page, errors = open_page(browser, server, data=d, clock="install")
    assert page.evaluate("reloadDue(Date.now())") is False
    with page.expect_navigation():
        page.clock.fast_forward("01:05")
    page.wait_for_function("document.getElementById('updated').textContent.startsWith('Matchup')")
    # back after a short look away: no reload
    page.evaluate("window.marker = 1; document.dispatchEvent(new Event('visibilitychange'))")
    assert page.evaluate("window.marker") == 1
    with page.expect_navigation():
        page.evaluate("loadedAt -= 11 * 60e3; document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function("document.getElementById('updated').textContent.startsWith('Matchup')")
    assert errors == []
    ctx.close()


def test_table_fixes(browser, server, data):
    """Waiver note in details, own players past row 300, rostered 'FA' players, tonight tags after puck drop, no badge on
    Week 0.0, punctuation-free and NHL-code search, Min games up to the nights, Pos sort (handoffs 142-145, 151, 152)."""
    from datetime import timedelta
    now, _ = _now_et()
    d = json.loads(json.dumps(data))
    team = d["teams"][0]
    mine = _mine(d, team)
    for p in mine:  # the team's players rank last, past the 300-row cap
        p.update(cr=0, cr_season=0, cr_games=0, ppg=0)
    mine[0]["team"] = "FA"  # rostered but unsigned
    waiver = next(p for p in d["players"] if p["owner"] == "freeagent" and p["team"] != "FA")
    waiver.update(owner="waivers", clears="Tue 13")
    g = next(p for p in d["players"] if p["pos"] == "G" and p.get("cr_why") and p["owner"] == "freeagent" and p["team"] != "FA")
    g["cr_why"]["tonight"] = {"starting": True, "status": "Confirmed"}
    g["today_start"] = _iso(now - timedelta(minutes=10))
    zero = next(p for p in d["players"] if p["id"] not in (g["id"], waiver["id"]) and p["owner"] == "freeagent")
    zero.update(cr=0.01, cr_pct=87)
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    shown = set(page.evaluate("[...document.querySelectorAll('#rows tr[data-id]')].map(r => +r.dataset.id)"))
    assert {p["id"] for p in mine} <= shown
    assert "more from the roster" in page.locator("#count").inner_text()
    # details
    page.select_option("#who", "fa")
    page.fill("#q", waiver["name"])
    page.locator(f"#rows tr[data-id='{waiver['id']}']").click()
    assert "on waivers: he can't play until he clears Tue 13" in page.locator("#rows tr.why").inner_text()
    page.fill("#q", g["name"])
    row = page.locator(f"#rows tr[data-id='{g['id']}']")
    assert "starts tonight" not in row.inner_text()
    row.click()
    why = page.locator("#rows tr.why").inner_text()
    assert "tonight's game has started" in why and "Daily Faceoff:" not in why
    assert page.evaluate(f"pctBadge(state.data.players.find(p => p.id === {zero['id']}))") == ""
    # search: punctuation and NHL codes
    named = next((p for p in d["players"] if any(c in p["name"] for c in ".'") and p["owner"] == "freeagent" and p["team"] != "FA"), None)
    if named:
        page.fill("#q", "".join(c for c in named["name"] if c not in ".'").lower())
        assert page.locator(f"#rows tr[data-id='{named['id']}']").count() == 1
    page.fill("#q", "TBL")
    teams_shown = set(page.evaluate("[...document.querySelectorAll('#rows tr[data-id]')].map(r => state.data.players.find(p => p.id === +r.dataset.id).team)"))
    assert teams_shown == {"TB"}
    page.fill("#q", "")
    assert page.get_attribute("#ming", "max") == str(len(d["nights"]))
    # Pos sorts by what the column shows
    page.locator("#head th[data-k='pos']").click()
    assert page.evaluate("""(() => { const s = [...document.querySelectorAll('#rows tr[data-id]')].map(r => r.children[2].textContent);
      return s.length > 1 && s.every((x, i) => i === 0 || s[i - 1].localeCompare(x) <= 0); })()""")
    assert errors == []
    ctx.close()


def test_stale_note_and_monday_rollover(browser, server, data):
    """Old data says when it's from and when the next update is, fades last night; until ESPN's ~3:30 AM Monday rollover the
    old matchup's moves still count (handoffs 146, 156)."""
    from datetime import datetime, timezone
    d = json.loads(json.dumps(data))
    d["updated_date"], d["updated"] = "2026-10-11", "Sun Oct 11, 5:02 PM ET"
    d["updated_utc"] = "2026-10-11T21:02:00Z"
    d["nights"][-1]["date"] = "Sun 11"
    d["move_limit"] = 7
    for t in d["teams"]:
        t["moves_used"] = 7
    # Monday 2 AM ET: ESPN is still on Sunday
    ctx, page, errors = open_page(browser, server, data=d, clock=datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc))
    assert page.evaluate("weekOver(state.data)") is False
    note = page.locator("#stale").inner_text()
    assert "from Sun Oct 11, 5:02 PM ET" in note and "already out of Week" in note and "Next update about 6 AM ET" in note
    first_left = next(i for i, n in enumerate(d["nights"]) if not n["past"])
    assert "past" in page.locator("#nights > *").nth(first_left).get_attribute("class")
    page.select_option("#team", str(d["teams"][0]["id"]))
    assert "next Monday's targets" in page.locator("#upgrades").inner_text()  # Sunday's moves are still the ones in use
    assert errors == []
    ctx.close()
    # Monday 5 AM ET: ESPN has rolled over
    ctx, page, errors = open_page(browser, server, data=d, clock=datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc))
    assert page.evaluate("weekOver(state.data)") is True
    assert "last matchup's numbers" in page.locator("#stale").inner_text()
    assert errors == []
    ctx.close()
    # the next run after 1 PM ET is 5 PM
    ctx, page, errors = open_page(browser, server, data=d, clock=datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc))
    assert page.evaluate("nextRun()") == "5 PM"
    ctx.close()


def test_drop_list_and_goalie_cards(browser, server, data):
    """Drop list gains use the cards' measure; the Goalies card never names tonight's starter; goalie keep shows (handoffs 157-159)."""
    import re
    from datetime import timedelta
    if not data.get("rated"):
        pytest.skip("needs rated data")
    capped = json.loads(json.dumps(data))
    capped["move_limit"] = 7
    for t in capped["teams"]:
        t["moves_used"] = 7
    ctx, page, errors = open_page(browser, server, data=capped)
    for t in capped["teams"]:
        page.select_option("#team", str(t["id"]))
        drops = _card(page, "Drop list")
        assert "keep" not in "".join(re.findall(r"\+\d+\.\d \w+", drops)), drops
    assert errors == []
    ctx.close()
    # goalies: the lowest keep value is tonight's confirmed starter
    now, _ = _now_et()
    d = _fresh(data)
    d["first_start"], d["move_limit"] = _iso(now + timedelta(hours=3)), None
    team = d["teams"][0]
    for p in _mine(d, team, "G"):
        p["team_id"] = None
    gs = [p for p in d["players"] if p["pos"] == "G" and p["owner"] == "freeagent" and p.get("cr_why")][:3]
    for i, p in enumerate(gs):
        p.update(team_id=team["id"], owner="other", ir=False, injury=None, cr=1.0 + i, cr_season=0.5 + i)
        p["cr_why"]["tonight"] = {"starting": i == 0, "status": "Confirmed"}
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    card = _card(page, "Goalies · your third")
    assert card and gs[0]["name"] not in card.split("drop ", 1)[1].split(" for ")[0]
    stream = bool(_card(page, "Stream a goalie tonight"))
    assert ("see Stream a goalie tonight" in card) == stream
    gcard = _card(page, "Goalies · your weakest")
    if gcard:
        assert "keep" in gcard.split("\n")[0]
    assert errors == []
    ctx.close()


def test_swap_subs_and_trade_chips(browser, server, data):
    """Next week's swap subs show Season, not '0 gm left'; a Best move dropping a trade-idea player says so (handoffs 160-161)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = json.loads(json.dumps(data))
    d["move_limit"] = 7
    for t in d["teams"]:
        t["moves_used"] = 7
        sk = next(p for p in _mine(d, t) if p["pos"] != "G" and not p["ir"])
        sk.update(injury="OUT", games_left=max(sk["games_left"], 1))
    ctx, page, errors = open_page(browser, server, data=d)
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        swaps = _card(page, "Mid-week swaps")
        assert "gm left" not in swaps and ("→" not in swaps or "Season" in swaps), swaps
    assert errors == []
    ctx.close()
    ctx, page, errors = open_page(browser, server)
    for t in data["teams"]:
        page.select_option("#team", str(t["id"]))
        ideas = _card(page, "Trade ideas")
        for line in _card(page, "Best").split("\n")[1:]:
            if "try trading him first" in line:
                dropped = line.split(" for ", 1)[1].split(" +", 1)[0]
                assert f"Offer {dropped}" in ideas, (line, ideas)
    assert errors == []
    ctx.close()


def test_trade_ideas_match_this_season_and_one_per_target(browser, server, data):
    """Each idea's pair also matches on this season's blended points, and no target is offered twice (handoffs 176, 181)."""
    import re
    blend = lambda p: ((p.get("cur_ppg") or 0) * (p.get("gp") or 0) + (p.get("last_ppg") or 0) * 40) / ((p.get("gp") or 0) + 40)
    by_name = {p["name"]: p for p in data["players"]}
    ctx, page, errors = open_page(browser, server)
    for t in data["teams"]:
        page.select_option("#team", str(t["id"]))
        targets = []
        for line in _card(page, "Trade ideas").split("\n")[1:]:
            m = re.match(r"Offer (.+?)(?: \((F|D)\))? for (.+?)(?: \((F|D)\))? \(", line)
            if m:
                assert abs(blend(by_name[m.group(1)]) - blend(by_name[m.group(3)])) <= 0.05 + 1e-9, line
                targets.append(m.group(3))
        assert len(targets) == len(set(targets)), targets
    assert errors == []
    ctx.close()


def _trade(page, give, get):
    page.evaluate(f"state.tgive = {json.dumps(give)}; state.tget = {json.dumps(get)}; renderTrade()")
    return page.locator("#trade").inner_text()


def test_trade_check_roster_spots(browser, server, data):
    """IR players hold no roster spot; a needed spot costs at least the best free agent; two healthy goalies; ESPN's line
    matches; an open spot needs no drop and a full partner must drop (handoffs 177-180, 182)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = json.loads(json.dumps(data))
    team = d["teams"][0]
    mine = _mine(d, team)
    for p in mine:
        p.update(ir=False, injury=None)
    sk = [p for p in mine if p["pos"] != "G"]
    gs = [p for p in mine if p["pos"] == "G"]
    for p in gs[2:]:  # carry exactly two goalies
        p["team_id"] = None
    gs = gs[:2]
    mine = [p for p in mine if p["pos"] != "G" or p in gs]
    sk[0]["ir"], sk[0]["injury"] = True, "INJURY_RESERVE"
    d["ir_slots"] = 2
    other = d["teams"][1]
    theirs = [p for p in _mine(d, other) if p["pos"] != "G" and not p["ir"]]
    for p in theirs:
        p["injury"] = None
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    # an IR player for a healthy one: the new player needs a roster spot
    text = _trade(page, [sk[0]["id"]], [theirs[0]["id"]])
    assert "drop" in text and "roster spot worth about" in text and "the best free agent" in text
    # 1-for-2: a spot that costs at least the best free agent
    text = _trade(page, [sk[1]["id"]], [theirs[0]["id"], theirs[1]["id"]])
    assert "roster spot worth about" in text and "the best free agent" in text
    # trading a goalie away with only two healthy: a free-agent goalie fills in, and ESPN's line is left out
    if len(gs) == 2:
        text = _trade(page, [gs[0]["id"]], [theirs[0]["id"]])
        assert "You'd have only 1 healthy goalie: add" in text and "ESPN's projections" not in text
    assert errors == []
    ctx.close()
    # with roster_max: an open spot means no drop; a partner at the limit has to drop for a 2-for-1
    d["roster_max"] = len([p for p in mine if not p["ir"]]) + 1
    other_n = len([p for p in d["players"] if str(p.get("team_id")) == str(other["id"]) and not p["ir"]])
    d["roster_max"] = max(d["roster_max"], other_n)
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    text = _trade(page, [sk[1]["id"]], [theirs[0]["id"], theirs[1]["id"]])
    if d["roster_max"] - len([p for p in mine if not p["ir"]]) >= 1:
        assert "You'd need to drop" not in text and "you drop nobody" in text
    text = _trade(page, [sk[1]["id"], sk[2]["id"]], [theirs[0]["id"]])
    if other_n >= d["roster_max"]:
        assert "would have to drop a player to take two" in text
    assert errors == []
    ctx.close()


def test_goalie_slots_follow_start_chances_and_new_details(browser, server, data):
    """The nights strip counts a goalie by his chance to start each later night (findings 236); the details name his own
    points per start, the team games counted, the season a fallback line is from and a yet-to-play share (findings 232-234)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    from datetime import datetime, timedelta, timezone
    base = json.loads(json.dumps(data))
    base["updated_date"] = datetime.now(timezone(timedelta(hours=-4))).date().isoformat()
    for n in base["nights"]:
        n["past"] = False  # tonight is the first night; the six after it use the per-night chances
    sps = [n["sp"] for n in base["nights"]]
    team = next(t for t in base["teams"] if len(_mine(base, t, "G")) >= 2)
    for p in base["players"]:
        if p["pos"] == "G":
            p["cr_why"] = dict(p.get("cr_why") or {}, own_pps=3.1, n10=6)
        else:
            p["cr_why"] = dict(p.get("cr_why") or {}, season_share=0.45, last_season="2024-25", last_fpg=1.2)
    for p in _mine(base, team, "G"):
        p.update(ir=False, injury=None, nights=sps, today_start=None)
    strips = {}
    for q in (0.0, 0.6):
        d = json.loads(json.dumps(base))
        for p in _mine(d, team, "G"):
            p["cr_g_nights"] = {str(sp): q for sp in sps}
        ctx, page, errors = open_page(browser, server, data=d)
        page.select_option("#team", str(team["id"]))
        strips[q] = page.locator("#nights .night").nth(3).inner_text()
        for i in range(min(8, rows(page))):
            page.locator("#rows tr[data-id]").nth(i).click()
            text = page.locator("#rows tr.why").inner_text()
            assert "his own 3.10 pts per start" in text or "expects him in about 45% of the rest" in text, text
            assert "his team's last 10" not in text
        assert errors == []
        ctx.close()
    goalies = len(_mine(base, team, "G"))
    assert "G2" in strips[0.0]  # no start expected: both slots open
    assert f"G{max(0, 2 - round(0.6 * goalies))}" in strips[0.6], strips[0.6]
