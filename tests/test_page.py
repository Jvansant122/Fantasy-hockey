"""Load index.html in a real browser with the committed data and click through it.

Catches a page that parses but throws while rendering (a bad field, a typo in a card), which the other tests can't see.
Runs in CI (the Tests workflow installs Playwright's Chromium); skipped locally when Playwright isn't installed."""
import functools
import http.server
import json
import os
import threading

import pytest

from conftest import ROOT, reopen_tonight

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


def open_page(browser, url, data=None, phone=False, clock=None, all_rows=True):
    """A fresh page (empty localStorage) that records every JS error. data replaces players.json if given.
    clock: a datetime the page's clock stands still at, or "install" for a fake clock the test moves on.
    all_rows: draw every table row at once (the page draws the first 60 and the rest on scroll), so tests can read them all."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844} if phone else {"width": 1280, "height": 900})
    page = ctx.new_page()
    if all_rows:
        page.add_init_script("window.ALL_ROWS = true")
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
    for pos in ["S", "G", "D", "F", "C", "LW", "RW", ""]:
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
        text = page.locator("#upgrades").inner_text()
        found = found or "next Monday's targets" in text
        # said once, in the note at the top, not again in every card (findings 238)
        assert text.count("No moves left") <= 1, (t["id"], text)
    assert found
    assert errors == []
    ctx.close()


def test_games_that_already_started_drop_out(browser, server, data):
    """A game today that started after the data run is no longer left to play (findings 178)."""
    from datetime import datetime, timedelta, timezone
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(timezone.utc)
    late = reopen_tonight(json.loads(json.dumps(data)))
    late["updated_utc"] = iso(now - timedelta(hours=2))
    late["updated_date"] = datetime.now(timezone(timedelta(hours=-4))).date().isoformat()  # not stale
    sk = next(p for p in late["players"] if p["pos"] != "G" and p["games_left"] > 0 and (p.get("cr") or 0) > 0)
    later = next(p for p in late["players"] if p["pos"] != "G" and p["games_left"] > 0 and p["id"] != sk["id"])
    sk["today_start"], later["today_start"] = iso(now - timedelta(hours=1)), iso(now + timedelta(hours=3))
    # his last game this week started: nothing is left, not a rounding leftover
    last = next((p for p in late["players"] if p["games_left"] == 1 and (p.get("cr") or 0) > 0 and p["id"] not in (sk["id"], later["id"])), None)
    if last is None:  # early in the week everyone has more games: give one player tonight's game only
        tonight_sp = next(n["sp"] for n in late["nights"] if not n["past"])
        last = next(p for p in late["players"] if p["pos"] != "G" and tonight_sp in (p.get("nights") or []) and (p.get("cr_dress_next") or 0) > 0
                    and (p.get("cr_fpg") or 0) > 0 and p["id"] not in (sk["id"], later["id"]))
        last.update(games_left=1, nights=[tonight_sp], cr_games=last["cr_dress_next"], cr=round(last["cr_fpg"] * last["cr_dress_next"], 2))
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
                p["cr_why"]["p_today"] = p["cr_why"]["p_today_pre"] = 0  # pre: a game already started on the real clock
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
            healthy = [p for p in d["players"] if str(p.get("team_id")) == str(partner) and p["pos"] != "G" and not p.get("ir") and not p.get("injury")
                       and not p.get("pending_drop")]  # dropped after the first puck: off the roster at the rollover
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
        p.update(ir=False, injury=None, sat_last=False, dfo_out=None, today_start=None, bench=False)
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
    yday = (et - timedelta(days=1, hours=4)).date()  # until 4 AM ET the page still counts the old matchup (findings 233)
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
    """On the matchup's last night before the first game, Best moves lists every move left, since they expire (findings 231).
    The expiry line is its own line, so phones show it too, and it shows with 1-2 moves left as well (findings 238)."""
    from datetime import datetime, timedelta, timezone
    if not data.get("rated"):
        pytest.skip("needs rated data")
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(timezone.utc)
    for first, want_note, used, phone in ((now + timedelta(hours=3), True, 1, False), (now - timedelta(hours=1), False, 1, False),
                                          (now + timedelta(hours=3), True, 1, True), (now + timedelta(hours=3), True, 6, False),
                                          (None, True, 1, True)):
        d = json.loads(json.dumps(data))
        d["updated_date"] = datetime.now(timezone(timedelta(hours=-4))).date().isoformat()  # not stale
        d["final_matchup"] = False
        for n in d["nights"]:
            n["past"] = n is not d["nights"][-1]
        d["first_start"] = iso(first) if first else None
        for p in d["players"]:
            p["today_start"] = iso(first) if first and d["nights"][-1]["sp"] in (p.get("nights") or []) else None
        d["move_limit"] = 7
        for t in d["teams"]:
            t["moves_used"] = used
        ctx, page, errors = open_page(browser, server, data=d, phone=phone)
        most, seen = 0, 0
        for t in d["teams"]:
            page.select_option("#team", str(t["id"]))
            text = page.locator("#upgrades").inner_text()  # visible text only: a folded line doesn't count
            card = page.locator("#upgrades .upg", has_text="Best")
            if not card.count():
                continue
            seen += 1
            assert ("expire tonight" in text) == want_note, (t["id"], first, phone)
            if want_note:
                assert text.count("expire tonight") == 1
                assert (f"You have {7 - used} move left" if used == 6 else f"You have {7 - used} moves left") in text, text
                assert ("overnight rollover" in text) == (first is None) and ("before the first game at" in text) == (first is not None)
            most = max(most, card.locator(":scope > div:not(.sub)").count())
        assert seen
        if used == 1 and not phone:
            assert (most > 2) == want_note, most
        if used == 6:
            assert most <= 1
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
    # Saturday morning: one move held for Sunday (handoff 207), and no free agent with a game left, so no Mid-week swap
    # sub takes the spot the IR move opens (handoff 211)
    d = _weekday(data, 5)
    for p in d["players"]:
        if p["owner"] in ("freeagent", "waivers") and p["pos"] != "G":
            p["games_left"] = 0
    team = d["teams"][0]
    roster = _mine(d, team)
    sk = [p for p in roster if p["pos"] != "G"]
    for p in roster:
        p.update(ir=False, injury=None, sat_last=False)
    healthy_ir, hurt = sk[0], sk[1]
    healthy_ir["ir"] = True
    hurt["injury"] = "INJURY_RESERVE"
    d["ir_slots"] = 3  # two open: one for the IR move, one for the stash (findings 240)
    non_ir = len(roster) - 1
    d["roster_max"], d["move_limit"], team["moves_used"] = non_ir, 7, 0
    d["move_limit_next"], team["moves_used_next"] = 7, 0  # after Sunday's first puck adds use next week's moves (handoff 263)
    fa = next(p for p in d["players"] if p["owner"] == "freeagent" and p["pos"] != "G")  # worth stashing
    fa.update(injury="OUT", cr_fpg=2.0, cr_season=1.0)
    fa["cr_why"] = {k: v for k, v in (fa.get("cr_why") or {}).items() if k != "back"}
    ctx, page, errors = open_page(browser, server, data=d, clock=SAT_10AM)
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
    # the IR move took one of the two open IR slots (handoff 212)
    assert "· 1 open IR slot." in stash, stash
    # Activate, Best moves and IR stash each drop a different player (handoff 213)
    best_drops = [l.split(" for ", 1)[1].split(" +", 1)[0] for l in best if " for " in l]
    assert best_drops and named not in best_drops, (named, best_drops)
    stash_drop = stash.split("Your roster is full: drop ", 1)[1].split(", add him", 1)[0]
    assert stash_drop != named and stash_drop not in best_drops, (named, best_drops, stash_drop)
    assert errors == []
    ctx.close()
    # only one IR slot open and the Drop list's IR move takes it: no IR stash (handoff 212)
    d["ir_slots"] = 2
    ctx, page, errors = open_page(browser, server, data=d, clock=SAT_10AM)
    page.select_option("#team", str(team["id"]))
    assert "Move to IR" in _card(page, "Drop list") and _card(page, "IR stash") == ""
    assert errors == []
    ctx.close()
    d["ir_slots"] = 3
    # full, nothing to move to IR, one move left: no open spot, and no stash that needs two moves
    hurt["injury"] = None
    team["moves_used"] = team["moves_used_next"] = 6
    ctx, page, errors = open_page(browser, server, data=d, clock=SAT_10AM)
    page.select_option("#team", str(team["id"]))
    assert _card(page, "IR stash") == ""
    assert all(not l.startswith("Add ") for l in _card(page, "Best").split("\n")[1:])
    assert errors == []
    ctx.close()
    # an open spot: the first add needs no drop, and activating needs none either
    d["roster_max"], team["moves_used"], team["moves_used_next"] = non_ir + 1, 0, 0
    ctx, page, errors = open_page(browser, server, data=d, clock=SAT_10AM)
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
            p["cr_why"]["p_today"] = p["cr_why"]["p_today_pre"] = 0  # pre: a game already started on the real clock
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
    assert "from Sun Oct 11, 5:02 PM ET" in note and "already out of Week" in note and "Next update about 6:10 AM ET" in note
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
    # the next numbers after 1 PM ET come about 5:10 PM
    ctx, page, errors = open_page(browser, server, data=d, clock=datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc))
    assert page.evaluate("nextRun()") == "5:10 PM"
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
    # a pending drop doesn't hold a spot (the page counts it the same way)
    other_n = len([p for p in d["players"] if str(p.get("team_id")) == str(other["id"]) and not p["ir"] and not p.get("pending_drop")])
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


def test_nothing_jumps_while_the_data_loads(browser, server):
    """The cards and footer stay hidden until the data has drawn them, so the page doesn't shift as it fills in (findings 237)."""
    ctx, page, errors = open_page(browser, server)
    assert page.evaluate("document.body.classList.contains('ready')")
    page.evaluate("document.body.classList.remove('ready')")
    assert page.evaluate("[...document.querySelectorAll('main > .card, main > p')].every(e => getComputedStyle(e).display === 'none')")
    ctx.close()
    # no data at all: the cards still show, with the 'No data yet' line
    ctx = browser.new_context()
    page = ctx.new_page()
    page.route("**/data/players.json", lambda r: r.fulfill(status=404, body="no"))
    page.goto(server)
    page.wait_for_function("document.body.classList.contains('ready')")
    assert "No data yet" in page.locator("#updated").inner_text()
    ctx.close()


def test_table_only_changes_leave_the_cards_alone(browser, server, data):
    """Sorting, opening details, searching and filtering don't rebuild Upgrades and Trade check (findings 237)."""
    ctx, page, errors = open_page(browser, server)
    page.select_option("#team", str(data["teams"][0]["id"]))
    page.evaluate("""window.redraws = 0; const u = renderUpgrades, t = renderTrade;
      renderUpgrades = () => { window.redraws++; u(); }; renderTrade = () => { window.redraws++; t(); }; 0""")
    page.locator("#head th").nth(3).click()
    page.locator("#rows tr[data-id]").first.click()
    page.fill("#q", "a")
    page.select_option("#pos", "D")
    page.fill("#ming", "1")
    page.click("#healthy")
    page.select_option("#who", "every")
    assert page.evaluate("window.redraws") == 0
    page.click("#left")  # Remaining games only changes the games the cards count
    assert page.evaluate("window.redraws") > 0
    page.select_option("#team", str(data["teams"][1]["id"]))
    assert page.evaluate("window.redraws") > 2
    assert errors == []
    ctx.close()


def test_keyboard_opens_details_and_sorts(browser, server):
    """Rows take focus and open with Enter or Space; sorting from the keyboard keeps focus on the header (findings 237)."""
    ctx, page, errors = open_page(browser, server)
    first = page.locator("#rows tr[data-id]").first
    pid = first.get_attribute("data-id")
    assert first.get_attribute("tabindex") == "0" and first.get_attribute("aria-expanded") == "false"
    first.focus()
    page.keyboard.press("Enter")
    assert page.locator("#rows tr.why").count() == 1
    assert page.evaluate("document.activeElement.dataset.id") == pid
    assert page.locator(f'#rows tr[data-id="{pid}"]').get_attribute("aria-expanded") == "true"
    page.keyboard.press(" ")
    assert page.locator("#rows tr.why").count() == 0
    page.locator('#head th[data-k="games"]').focus()
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.dataset.k") == "games"
    first_dir = page.locator('#head th[data-k="games"]').get_attribute("aria-sort")
    page.keyboard.press("Enter")
    second_dir = page.locator('#head th[data-k="games"]').get_attribute("aria-sort")
    assert {first_dir, second_dir} == {"ascending", "descending"}
    assert page.locator("#head th[aria-sort]").count() == 1
    assert page.get_attribute("#pos", "aria-label") == "Position" and page.get_attribute("#q", "aria-label")
    assert errors == []
    ctx.close()


CONTRAST_JS = """sel => {
  const rgb = c => { const m = c.match(/[\\d.]+/g).map(Number); return { r: m[0], g: m[1], b: m[2], a: m.length > 3 ? m[3] : 1 }; };
  const lum = ({ r, g, b }) => { const f = v => (v /= 255) <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const bg = e => { for (; e; e = e.parentElement) { const c = rgb(getComputedStyle(e).backgroundColor); if (c.a > 0.5) return c; } return { r: 255, g: 255, b: 255 }; };
  return [...document.querySelectorAll(sel)].filter(e => e.offsetParent).map(e => {
    const a = lum(rgb(getComputedStyle(e).color)), b = lum(bg(e));
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  });
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_tags_badges_and_nights_are_readable(browser, server, data, scheme):
    """Tags, badges, gains and past nights reach 4.5:1 in both themes; open nights aren't told by colour alone (findings 237)."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
    page = ctx.new_page()
    page.goto(server)
    page.wait_for_function("document.body.classList.contains('ready')")
    page.select_option("#team", str(data["teams"][0]["id"]))
    page.select_option("#who", "every")
    for sel in (".tag.inj", ".tag.wv", ".tag.news", ".pct.hi", ".pct.mid", "#upgrades .up", ".night.past", "main > p.sub a"):
        ratios = page.evaluate(CONTRAST_JS, sel)
        assert all(r >= 4.5 for r in ratios), (scheme, sel, min(ratios, default=None))
    assert page.evaluate("[...document.querySelectorAll('.night.past')].every(e => getComputedStyle(e).opacity === '1')")
    assert page.evaluate("[...document.querySelectorAll('.dots i')].every(e => getComputedStyle(e).opacity === '1')")
    open_dot = page.locator(".dots i.play.open").first
    if open_dot.count():
        assert open_dot.evaluate("e => getComputedStyle(e).borderRadius") == "50%"
    ctx.close()


PHONE_SPILL_JS = """[...document.querySelectorAll('#rows td:first-child .tags .tag')].filter(e => e.offsetParent).filter(e => {
  const td = e.closest('td').getBoundingClientRect(); return e.getBoundingClientRect().right > td.right + 1; }).length"""


@pytest.mark.parametrize("width", [320, 360, 390])
def test_phone_text_taps_names_and_tags(browser, server, data, width):
    """Phones: 12 px text at least, 24 px tap targets, names wrap instead of being cut, and a long tag ends in … inside
    its column (findings 237, 238)."""
    ctx = browser.new_context(viewport={"width": width, "height": 800})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server)
    page.wait_for_function("document.body.classList.contains('ready')")
    for t in data["teams"][:4]:
        page.select_option("#team", str(t["id"]))
        page.select_option("#who", "every")
        small = page.evaluate("""[...document.querySelectorAll('.night, .night .open, .meta, .tag, .pct, #rows td')].filter(e => e.offsetParent)
          .filter(e => parseFloat(getComputedStyle(e).fontSize) < 12).length""")
        assert small == 0
        short = page.evaluate("""[...document.querySelectorAll('details.more > summary, details.fold > summary, main > .sub a')]
          .filter(e => e.offsetParent).map(e => e.getBoundingClientRect()).filter(r => r.height < 24).length""")
        assert short == 0
        # no name cut short: the cell's text fits its box
        cut = page.evaluate("[...document.querySelectorAll('#rows td:first-child')].filter(td => td.scrollWidth > td.clientWidth + 1).length")
        assert cut == 0
        assert page.evaluate(PHONE_SPILL_JS) == 0
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    # the longest tag the page shows, in a narrow name column
    d = json.loads(json.dumps(data))
    for p in d["players"][:20]:
        p["injury"], p["ir"] = "SUSPENSION", False
    page.route("**/data/players.json", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(d)))
    page.reload()
    page.wait_for_function("document.body.classList.contains('ready')")
    page.select_option("#who", "every")
    assert page.evaluate(PHONE_SPILL_JS) == 0
    assert errors == []
    ctx.close()


def test_position_filter_order_and_skaters(browser, server):
    """Positions read All, Skaters (D + F), G, D, Any F, C, LW, RW; Skaters shows everyone but goalies (findings 300)."""
    ctx, page, errors = open_page(browser, server)
    assert page.evaluate("[...document.querySelectorAll('#pos option')].map(o => o.value)") == ["", "S", "G", "D", "F", "C", "LW", "RW"]
    assert page.locator("#pos option").nth(1).inner_text() == "Skaters (D + F)"
    page.select_option("#who", "every")
    page.select_option("#pos", "S")
    shown = page.evaluate("[...document.querySelectorAll('#rows tr[data-id]')].map(r => state.data.players.find(p => p.id === +r.dataset.id).pos)")
    assert shown and "G" not in shown and {"D"} < set(shown)
    page.reload()
    page.wait_for_function("document.body.classList.contains('ready')")
    assert page.input_value("#pos") == "S"  # remembered like the others
    assert errors == []
    ctx.close()


def test_column_order(browser, server):
    """Proj/G, This yr/G and Proj GP follow Games, Last yr GP sits before Last yr/G; phones still show Player, Week, Season, Nights, Open (findings 300)."""
    ctx, page, errors = open_page(browser, server)
    heads = page.locator("#head th").all_inner_texts()
    assert [h.rstrip(" ▾▴") for h in heads] == ["Player", "Team", "Pos", "Week", "Season", "Nights", "Games", "Proj/G", "This yr/G",
                                                "Proj GP", "Open", "Light", "ESPN week", "Last yr GP", "Last yr/G", "% Own"]
    ctx.close()
    ctx, page, errors2 = open_page(browser, server, phone=True)
    shown = page.evaluate("[...document.querySelectorAll('#head th')].filter(th => th.offsetParent).map(th => th.dataset.k)")
    assert shown == ["name", "cr", "cr_season", "nights", "opengm"]
    assert errors == errors2 == []
    ctx.close()


def _with_weeks(data):
    """data with season_weeks and team_nights. Data from before handoff 204 has neither: then one made-up later week, the
    week after this matchup, where each NHL team plays on the same weekdays as this matchup."""
    import check_data
    d = json.loads(json.dumps(data))
    if d.get("season_weeks") and d.get("team_nights") and any(w["sps"][0] > d["nights"][-1]["sp"] for w in d["season_weeks"]):
        return d
    from datetime import timedelta
    last = d["nights"][-1]
    sunday = check_data._night_dates(d)[last["sp"]]
    sps = list(range(last["sp"] + 1, last["sp"] + 8))
    days = [sunday + timedelta(days=i + 1) for i in range(7)]
    games = {str(sp): n["games"] for sp, n in zip(sps, d["nights"][-7:])}
    ahead = {n["sp"] for n in d["nights"] if not n["past"]}
    d["season_weeks"] = [{"week": (sps[0] + 7) // 7, "sps": sps, "label": f"{days[0]:%b} {days[0].day}-{days[-1].day}",
                          "dates": [f"{x:%a} {x.day}" for x in days], "games": games,
                          "light": [sp for sp in sps if 0 < games[str(sp)] <= 5]}]
    d["team_nights"] = {}
    for p in d["players"]:
        if p["team"] != "FA" and p["owner"] != "waivers":
            d["team_nights"][p["team"]] = sorted({sp for sp in p["nights"] if sp in ahead} | {sp + 7 for sp in p["nights"] if sp + 7 in sps})
    return d


def test_nights_for_a_later_week(browser, server, data):
    """Nights week (findings 300): a later week's dots, games, light nights and open slots in the table; Week and the cards
    stay on this matchup; old data has no picker."""
    d = _with_weeks(data)
    ctx, page, errors = open_page(browser, server, data=d)
    team = d["teams"][0]
    page.select_option("#team", str(team["id"]))
    assert page.locator("#wk").is_visible()
    later = [w for w in d["season_weeks"] if w["sps"][0] > d["nights"][-1]["sp"]]
    assert page.locator("#wk option").count() == 1 + len(later)
    assert page.locator("#wk option").first.inner_text() == "This matchup"
    w = later[0]
    cards = page.locator("#upgrades").inner_html()
    week_before = page.evaluate("[...document.querySelectorAll('#rows tr[data-id]')].slice(0, 40).map(r => [r.dataset.id, r.children[3].innerText])")
    page.select_option("#wk", str(w["week"]))
    head = page.locator('#head th[data-k="nights"]').inner_text()
    assert head.startswith("Nights") and f"({w['label']})" in head
    assert page.locator("#left").is_disabled()
    rows_ = page.evaluate("""[...document.querySelectorAll('#rows tr[data-id]')].map(r => {
      const p = state.data.players.find(p => p.id === +r.dataset.id);
      return { id: r.dataset.id, team: p.team, fa: p.owner === 'freeagent' || p.owner === 'waivers', pos: p.pos,
               games: +r.children[6].innerText, open: r.children[10].innerText, light: +r.children[11].innerText,
               dots: [...r.children[5].querySelectorAll('i')].map(i => i.className.trim()), week: r.children[3].innerText }; })""")
    light = set(w["light"])
    for r in rows_:
        plays = [sp in d["team_nights"].get(r["team"], []) for sp in w["sps"]]
        assert r["games"] == sum(plays), r
        assert r["light"] == sum(p and sp in light for p, sp in zip(plays, w["sps"])), r
        assert len(r["dots"]) == len(w["sps"]) and [c.startswith("play") for c in r["dots"]] == plays, r
        assert "past" not in " ".join(r["dots"])
        if r["fa"]:
            assert r["open"] != "–" and int(r["open"]) <= r["games"], r
            assert int(r["open"]) == sum("open" in c for c in r["dots"])
        else:
            assert r["open"] == "–"
    # Week stays this matchup's; the cards don't change
    before = dict(week_before)
    assert all(before[r["id"]] == r["week"] for r in rows_ if r["id"] in before)
    assert page.locator("#upgrades").inner_html() == cards
    # an empty slot every night the week's schedule leaves one: count it the same way here
    slots = page.evaluate("state.tw.slots")
    mine = [p for p in _mine(d, team) if not p["ir"] and p.get("injury") not in ("OUT", "INJURY_RESERVE", "SUSPENSION") and p["pos"] != "G"]
    for sp in w["sps"]:
        f = sum(1 for p in mine if p["pos"] != "D" and sp in d["team_nights"].get(p["team"], []))
        dd = sum(1 for p in mine if p["pos"] == "D" and sp in d["team_nights"].get(p["team"], []))
        assert slots[str(sp)]["F"] == max(0, 9 - f) and slots[str(sp)]["D"] == max(0, 5 - dd) + (1 if f <= 9 and dd <= 5 else 0)
    # Min games counts the week's games; sorting and details follow it
    page.fill("#ming", "4")
    assert all(int(t) >= 4 for t in page.evaluate("[...document.querySelectorAll('#rows tr[data-id]')].map(r => r.children[6].innerText)"))
    page.locator('#head th[data-k="opengm"]').click()
    page.locator("#rows tr[data-id]").first.click()
    assert f"in the week of {w['label']}" in page.locator("#rows tr.why").inner_text()
    # back to this matchup
    page.select_option("#wk", "")
    assert page.locator('#head th[data-k="nights"]').inner_text().startswith("Nights") and "(" not in page.locator('#head th[data-k="nights"]').inner_text()
    assert not page.locator("#left").is_disabled()
    assert errors == []
    ctx.close()
    # data without the new fields: no picker, the page as before
    old = json.loads(json.dumps(data))
    old.pop("season_weeks", None), old.pop("team_nights", None)
    ctx, page, errors = open_page(browser, server, data=old)
    assert page.locator("#wk").is_hidden() and rows(page) > 0
    assert errors == []
    ctx.close()


def test_wide_desktop_and_all_columns_on_phones(browser, server, data):
    """The desktop table fits without sideways scrolling from 1366 px; phones can turn on every column, which scroll
    inside the table, not the page (findings 300)."""
    for width in (1366, 1920):
        ctx = browser.new_context(viewport={"width": width, "height": 900})
        page = ctx.new_page()
        page.goto(server)
        page.wait_for_function("document.body.classList.contains('ready')")
        page.select_option("#team", str(data["teams"][0]["id"]))
        wrap = page.evaluate("(() => { const w = document.querySelector('.tablewrap'); return [w.scrollWidth, w.clientWidth]; })()")
        assert wrap[0] <= wrap[1] + 1, (width, wrap)
        assert page.locator("#allcols").is_hidden()  # phones only
        assert page.evaluate("document.querySelector('main > p.sub').getBoundingClientRect().width") < 900
        ctx.close()
    ctx, page, errors = open_page(browser, server, phone=True)
    page.select_option("#team", str(data["teams"][0]["id"]))
    assert page.locator("#allcols").is_visible() and not page.locator("#allcols").is_checked()
    page.check("#allcols")
    assert page.evaluate("[...document.querySelectorAll('#head th')].filter(th => th.offsetParent).length") == 16
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    wrap = page.evaluate("(() => { const w = document.querySelector('.tablewrap'); return [w.scrollWidth, w.clientWidth]; })()")
    assert wrap[0] > wrap[1]  # the table scrolls sideways inside its box
    page.evaluate("document.querySelector('.tablewrap').scrollLeft = 400")
    assert page.evaluate("document.querySelector('#rows td').getBoundingClientRect().left") >= page.evaluate("document.querySelector('.tablewrap').getBoundingClientRect().left") - 1
    page.reload()
    page.wait_for_function("document.body.classList.contains('ready')")
    assert page.locator("#allcols").is_checked() and page.evaluate("document.body.classList.contains('allcols')")
    page.uncheck("#allcols")
    assert page.evaluate("[...document.querySelectorAll('#head th')].filter(th => th.offsetParent).length") == 5
    assert page.evaluate("(() => { const w = document.querySelector('.tablewrap'); return w.scrollWidth <= w.clientWidth + 1; })()")
    assert errors == []
    ctx.close()


def _et(month, day, hour, minute=0):
    """A fixed clock at that time in New York in October 2026 (EDT, UTC-4)."""
    from datetime import datetime, timedelta, timezone
    return datetime(2026, month, day, hour, minute, tzinfo=timezone(timedelta(hours=-4)))


SUN_6PM = _et(10, 11, 18)  # Sun Oct 11, 6 PM ET: after the first puck of the matchup's last night
SAT_10AM = _et(10, 10, 10)  # Sat Oct 10, 10 AM ET: _weekday(data, 5)'s run, before the first puck


def _sunday(data):
    """The data as a Sunday 3 PM ET run on Oct 11, 2026: Sunday the matchup's last night, first puck at 5 PM ET, 7 moves."""
    d = json.loads(json.dumps(data))
    d.update(updated_date="2026-10-11", updated="Sun Oct 11, 3:00 PM ET", updated_utc="2026-10-11T19:00:00Z",
             first_start="2026-10-11T21:00:00Z", final_matchup=False, move_limit=7, matchup_weeks=1, season_over=False)
    for i, n in enumerate(d["nights"][-7:]):  # Mon 5 .. Sun 11, whatever week the committed data is from
        n["date"] = f"{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][i]} {5 + i}"
    for n in d["nights"]:
        n["past"] = n is not d["nights"][-1]
    last = d["nights"][-1]["sp"]
    for p in d["players"]:
        p["today_start"] = d["first_start"] if last in (p.get("nights") or []) else None
        if p["games_left"] > 1 or (p["games_left"] and not p["today_start"]):  # earlier-week data: only tonight is left
            p["games_left"] = 1 if p["today_start"] else 0
    for t in d["teams"]:
        t["moves_used"] = 0
    return d


def test_monday_before_rollover_says_nothing_expires(browser, server, data):
    """Monday 0-4 AM, Sunday's data still has Sunday as tonight: no 'expire tonight ... before the first game' (handoff 210)."""
    from datetime import datetime, timezone
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = _sunday(data)
    for clock, want in ((datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc), True),   # Sun 4 PM ET: before the first puck
                        (datetime(2026, 10, 12, 4, 30, tzinfo=timezone.utc), False),  # Mon 0:30 AM ET
                        (datetime(2026, 10, 12, 7, 30, tzinfo=timezone.utc), False)):  # Mon 3:30 AM ET
        ctx, page, errors = open_page(browser, server, data=d, clock=clock)
        assert page.evaluate("weekOver(state.data)") is False
        seen = 0
        for t in d["teams"]:
            page.select_option("#team", str(t["id"]))
            seen += "expire tonight" in page.locator("#upgrades").inner_text()
        assert (seen > 0) == want, (clock, seen)
        assert errors == []
        ctx.close()


def test_swap_ir_move_isnt_an_open_spot_twice(browser, server, data):
    """A Mid-week swap of an IR-eligible player uses the spot his IR move opens, so Best moves gets no 'open roster spot';
    only the player who actually fits the open IR slot is told to move to IR (handoff 211)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = _sunday(data)  # Sunday after the first puck: no night left to hold moves for
    d["move_limit"] = 7
    team = d["teams"][0]
    team["moves_used"] = 0
    roster = _mine(d, team)
    for p in roster:
        p.update(ir=False, injury=None, sat_last=False)
    sk = sorted((p for p in roster if p["pos"] != "G"), key=lambda p: -(p.get("cr_season") or 0))
    out1, out2 = sk[0], sk[1]  # two out, one IR slot: only one can move to IR
    for p in (out1, out2):
        p.update(injury="OUT", games_left=max(p["games_left"], 1))
    d["ir_slots"], d["roster_max"] = 1, len(roster)
    for p in d["players"]:  # free agents with a game left, whatever the clock, so the swaps get subs
        if p["owner"] in ("freeagent", "waivers"):
            p.update(games_left=max(p["games_left"], 1), today_start=None)
    ctx, page, errors = open_page(browser, server, data=d, clock=SUN_6PM)
    page.select_option("#team", str(team["id"]))
    swaps = _card(page, "Mid-week swaps")
    assert swaps.count("→") == 2, swaps
    assert len([l for l in swaps.split("\n") if "IR-eligible" in l]) == 1, swaps
    best = _card(page, "Best")
    assert best and "open roster spot" not in best, best
    assert errors == []
    ctx.close()
    # one out player and 2 moves left: one swap and one Best move, and the swap uses the IR move's spot
    team["moves_used"] = 5
    out2["injury"] = None
    ctx, page, errors = open_page(browser, server, data=d, clock=SUN_6PM)
    page.select_option("#team", str(team["id"]))
    swaps, best = _card(page, "Mid-week swaps"), _card(page, "Best")
    assert swaps.count("→") == 1 and "IR-eligible" in swaps, swaps
    assert best and "open roster spot" not in best, best
    assert errors == []
    ctx.close()


def test_final_with_no_moves_shows_no_drops(browser, server, data):
    """The final with every move used: no Drop list, no 'fourth goalie' advice; with moves, the goalie line has no keep value
    and says 'lowest points left' (handoff 214)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = _fresh(data)
    d["final_matchup"], d["move_limit"] = True, 7
    team = d["teams"][0]
    for p in _mine(d, team, "G"):
        p["team_id"] = None
    for p in [p for p in d["players"] if p["pos"] == "G" and p["owner"] == "freeagent" and p.get("cr_why")][:4]:
        p.update(team_id=team["id"], owner="other", ir=False, injury=None)
    for used, shown in ((7, False), (0, True)):
        team["moves_used"] = used
        ctx, page, errors = open_page(browser, server, data=d)
        page.select_option("#team", str(team["id"]))
        text = page.locator("#upgrades").inner_text()
        goalies = _card(page, "Goalies · you carry")  # not the 'Goalies · your weakest' add card
        if not shown:
            assert _card(page, "Drop list") == "" and "fourth spot" not in text and goalies == "", text
        else:
            assert "fourth spot" in goalies and _card(page, "Drop list")
            lines = [l for l in goalies.split("\n")[1:] if l.strip()]
            assert lines and all("keep" not in l and "lowest points left" in l for l in lines), goalies
        assert errors == []
        ctx.close()


def test_trade_check_counts_an_ir_move_spot(browser, server, data):
    """A full roster with an IR-eligible player outside IR and an IR slot open: a 1-for-2 needs no drop (handoff 215)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = json.loads(json.dumps(data))
    team, other = d["teams"][0], d["teams"][1]
    mine = _mine(d, team)
    for p in mine:
        p.update(ir=False, injury=None)
    sk = [p for p in mine if p["pos"] != "G"]
    hurt = sk[-1]
    hurt["injury"] = "OUT"
    d["ir_slots"], d["roster_max"] = 1, len(mine)
    theirs = [p for p in _mine(d, other) if p["pos"] != "G" and not p["ir"]]
    for p in theirs:
        p["injury"] = None
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    text = _trade(page, [sk[0]["id"]], [theirs[0]["id"], theirs[1]["id"]])
    assert f"Moving {hurt['name']} to IR opens the spot, so you drop nobody until he's back." in text, text
    assert "You'd need to drop" not in text
    # 1-for-3: the IR move opens one spot, the other still needs a drop
    text = _trade(page, [sk[0]["id"]], [theirs[0]["id"], theirs[1]["id"], theirs[2]["id"]])
    assert f"Move {hurt['name']} to IR, and then you'd need to drop" in text, text
    assert f"drop {hurt['name']}" not in text, text  # never drops the player it moves to IR (handoff 456)
    # giving the injured player away: no IR move to count
    text = _trade(page, [hurt["id"]], [theirs[0]["id"], theirs[1]["id"]])
    assert "to IR" not in text and "You'd need to drop" in text, text
    assert errors == []
    ctx.close()


def test_ir_stash_counts_best_moves(browser, server, data):
    """IR stash needs a move left after Best moves takes its picks (handoff 216)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = _weekday(data, 5)  # Saturday morning: one move held for Sunday (handoff 207)
    d["move_limit"] = 7
    team = d["teams"][0]
    roster = _mine(d, team)
    for p in roster:
        p.update(ir=False, injury=None, sat_last=False)
    d["ir_slots"], d["roster_max"] = 1, len(roster) + 1  # an open IR slot and an open roster spot
    fa = next(p for p in d["players"] if p["owner"] == "freeagent" and p["pos"] != "G")
    fa.update(injury="OUT", cr_fpg=2.0, cr_season=1.0)
    fa["cr_why"] = {k: v for k, v in (fa.get("cr_why") or {}).items() if k != "back"}
    for used, shown in ((0, True), (4, False)):  # 7 left: room for it; 3 left, one held for Sunday: Best moves takes the other 2
        team["moves_used"] = team["moves_used_next"] = used  # after Sunday's first puck adds use next week's moves (handoff 263)
        d["move_limit_next"] = 7
        ctx, page, errors = open_page(browser, server, data=d, clock=SAT_10AM)
        page.select_option("#team", str(team["id"]))
        best = [l for l in _card(page, "Best").split("\n")[1:] if l.strip()]
        assert len(best) == 2, best
        assert bool(_card(page, "IR stash")) == shown, (used, _card(page, "IR stash"))
        assert errors == []
        ctx.close()


def test_reload_keeps_trade_picks(browser, server, data):
    """Trade check's picks come back after a reload (the open tab reloads at each game start) and clear with a new team (handoff 217)."""
    ctx, page, errors = open_page(browser, server)
    team, other = data["teams"][0], data["teams"][1]
    give = next(p for p in _mine(data, team) if p["pos"] != "G")
    get = next(p for p in _mine(data, other) if p["pos"] != "G")
    page.select_option("#team", str(team["id"]))
    page.select_option("#tgive", str(give["id"]))
    page.select_option("#tget", str(get["id"]))
    page.reload()
    page.wait_for_function("document.getElementById('updated').textContent.startsWith('Matchup')")
    assert page.evaluate("[state.tgive, state.tget]") == [[give["id"]], [get["id"]]]
    text = page.locator("#trade").inner_text()
    assert give["name"] in text and get["name"] in text and "points a week" in text
    page.select_option("#team", str(other["id"]))
    page.reload()
    page.wait_for_function("document.getElementById('updated').textContent.startsWith('Matchup')")
    assert page.evaluate("[state.tgive, state.tget]") == [[], []]
    assert errors == []
    ctx.close()


def test_player_dropped_after_first_puck_frees_his_spot(browser, server, data):
    """A player dropped after the first puck (pending_drop) holds no roster spot and is never offered as a drop or trade (handoff 220)."""
    from datetime import datetime, timezone
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = _sunday(data)
    team = d["teams"][0]
    roster = _mine(d, team)
    for p in roster:
        p.update(ir=False, injury=None, sat_last=False)
    d["roster_max"], d["ir_slots"] = len(roster), 1
    gone = min((p for p in roster if p["pos"] != "G"), key=lambda p: (p.get("cr") or 0) + 7 * (p.get("cr_season") or 0))
    clock = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)  # Sun 6 PM ET, after the first puck
    for pending in (False, True):
        gone["pending_drop"] = pending
        ctx, page, errors = open_page(browser, server, data=d, clock=clock)
        page.select_option("#team", str(team["id"]))
        drops, best = _card(page, "Drop list"), _card(page, "Best").split("\n")[1:]
        assert best, best
        # full roster: every Best move needs a drop, and he's first on the Drop list; dropped already: one spot is open
        assert (gone["name"] in drops) == (not pending), drops
        assert ("open roster spot" in best[0]) == pending, best
        assert not pending or not any(f" for {gone['name']}" in l for l in best), best
        give = page.evaluate("[...document.querySelectorAll('#tgive option')].map(o => +o.value)")
        assert (gone["id"] in give) == (not pending)


def _weekday(data, day, puck_hour=19):
    """The data as a 10 AM ET run on day `day` (0 = Mon Oct 5) of the Oct 5-11, 2026 matchup, first puck at puck_hour ET."""
    d = json.loads(json.dumps(data))
    d["nights"] = d["nights"][:7]
    for i, n in enumerate(d["nights"]):
        n.update(date=f"{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][i]} {5 + i}", past=i < day)
    d.update(updated_date=f"2026-10-{5 + day:02d}", updated=f"Oct {5 + day}, 10:00 AM ET", updated_utc=f"2026-10-{5 + day:02d}T14:00:00Z",
             first_start=f"2026-10-{5 + day:02d}T{puck_hour + 4:02d}:00:00Z", final_matchup=False, move_limit=7, matchup_weeks=1)
    sp = d["nights"][day]["sp"]
    for p in d["players"]:
        p["today_start"] = d["first_start"] if sp in (p.get("nights") or []) else None
    return d


def test_best_moves_hold_moves_for_the_week(browser, server, data):
    """Best moves offers 2 on Monday, then holds a move for each night still to come instead of 2 more every update (handoff 207)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    for day, used, most in ((0, 0, 2), (0, 2, 0), (2, 2, 1), (4, 2, 2)):  # Mon: 2 then hold 5; Wed: 5 left, 4 held; Fri: 2 held
        d = _weekday(data, day)
        for t in d["teams"]:
            t["moves_used"] = used
        for p in d["players"]:  # no Mid-week swap takes one of the moves first
            if p.get("team_id") is not None:
                p.update(injury=None, sat_last=False)
        ctx, page, errors = open_page(browser, server, data=d, clock=_et(10, 5 + day, 10))
        counts, held = [], 0
        for t in d["teams"]:
            page.select_option("#team", str(t["id"]))
            best = _card(page, "Best")
            counts.append(len([l for l in best.split("\n")[1:] if l.strip()]))
            if "one is held for each night still to come" in best:
                held += 1
                assert f"You have {7 - used} moves left" in best, best
        assert max(counts) == most, (day, used, counts)
        assert (held > 0) == (most == 0), (day, used, held)
        assert errors == []
        ctx.close()


def test_last_night_skips_claims_that_go_through_monday(browser, server, data):
    """Sunday's use-every-move list leaves out a waiver player who clears after the matchup (handoff 208)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    for clears, listed in (("Mon 12", False), ("Sun 11", True)):
        d = _sunday(data)
        star = next(p for p in d["players"] if p["owner"] == "freeagent" and p["pos"] not in ("G", "D") and p.get("cr") is not None)
        star.update(owner="waivers", clears=clears, injury=None, cr_season=4.0, cr=4.0)
        ctx, page, errors = open_page(browser, server, data=d, clock=_et(10, 11, 16))
        shown = 0
        for t in d["teams"]:
            page.select_option("#team", str(t["id"]))
            best = _card(page, "Best")
            assert "expire tonight" in page.locator("#upgrades").inner_text(), t["id"]
            shown += star["name"] in best
        assert (shown > 0) == listed, (clears, shown)
        assert errors == []
        ctx.close()


def test_stream_card_names_only_rechecks_before_the_puck(browser, server, data):
    """'No free-agent goalie confirmed yet' names only the updates before tonight's first game (handoff 209)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    for puck, now, want in ((19, (10, 0), "rechecks at about 12:40 and 5 PM ET."),
                            (13, (10, 0), "rechecks at about 12:40 PM ET."),
                            (19, (13, 0), "rechecks at about 5 PM ET."),
                            (13, (12, 50), "The next update comes after tonight's first game at 1:00 PM ET, so a goalie confirmed later can't be added for tonight."),
                            (12, (10, 0), "The next update comes after tonight's first game at 12:00 PM ET")):
        d = _weekday(data, 5, puck)  # Saturday
        for p in d["players"]:
            if p["pos"] == "G" and p.get("cr_why"):
                p["cr_why"].pop("tonight", None)
                p["cr_why"]["p_today"] = 0
        team = d["teams"][0]
        team["moves_used"] = 2
        ctx, page, errors = open_page(browser, server, data=d, clock=_et(10, 10, *now))
        page.select_option("#team", str(team["id"]))
        card = _card(page, "Stream a goalie tonight")
        assert "No free-agent goalie is confirmed" in card and want in card, (puck, now, card)
        assert errors == []
        ctx.close()


def test_picked_week_keeps_the_phone_table_inside_the_screen(browser, server, data):
    """A later week's dates don't widen the phone's Nights header, and with All columns an opened row's details stay on
    screen while the table scrolls sideways (findings 242)."""
    later = [w for w in data.get("season_weeks", []) if w["sps"][0] > data["nights"][-1]["sp"]]
    assert later, "the data has no later week to pick"
    for width in (320, 360, 390):
        ctx = browser.new_context(viewport={"width": width, "height": 800})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server)
        page.wait_for_function("document.body.classList.contains('ready')")
        page.select_option("#team", str(data["teams"][0]["id"]))
        page.select_option("#wk", str(later[0]["week"]))
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert page.evaluate("document.querySelector('#rows').closest('div').scrollWidth <= window.innerWidth + 1")
        # All columns: the table scrolls, the opened row's text stays in view
        page.check("#allcols")
        page.locator("#rows tr[data-id]").first.click()
        page.evaluate("document.querySelector('#rows').closest('div').scrollLeft = 400")
        r = page.evaluate("(() => { const b = document.querySelector('tr.why .whytxt').getBoundingClientRect(); return [b.left, b.right]; })()")
        assert r[0] >= 0 and r[1] <= width + 1, r
        assert errors == []
        ctx.close()


def test_goalie_details_name_the_pull_to_the_league_rate(browser, server, data):
    """A goalie whose week price differs from his own rate says it was pulled toward the league's rate, before naming the
    schedule, so a soft schedule isn't blamed for a lower price (findings 231, 242)."""
    d = json.loads(json.dumps(data))
    g = next(p for p in d["players"] if p["pos"] == "G" and p.get("cr_why"))
    g["cr_fpg"], g["cr_why"]["own_pps"], g["cr_why"]["sched"], g["cr_why"]["gs"] = 3.47, 3.96, 0.2, 8
    g["cr_why"].pop("line", None)
    ctx, page, errors = open_page(browser, server, d)
    text = page.evaluate(f"why(state.data.players.find(p => p.id === {g['id']}))")
    assert "his own 3.96 pts per start" in text
    assert "this week priced at 3.47: pulled most of the way to the league's rate" in text and "then the schedule" in text
    g2 = page.evaluate(f"(() => {{ const p = state.data.players.find(p => p.id === {g['id']}); p.cr_why.sched = null; return why(p); }})()")
    assert "pulled most of the way to the league's rate (this week's starts keep 0.4 of his gap)" in g2 and "then" not in g2.split("priced at")[1]
    assert errors == []
    ctx.close()


def test_sunday_after_first_puck_counts_next_weeks_moves(browser, server, data):
    """After Sunday's first puck an add is charged to the next matchup (handoff 263): a team out of this week's moves
    still has next week's, less any it already made tonight."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = _sunday(data)
    team = d["teams"][0]
    d["move_limit"], d["move_limit_next"], team["moves_used"], team["moves_used_next"] = 7, 7, 7, 1
    ctx, page, errors = open_page(browser, server, data=d, clock=SUN_6PM)
    page.select_option("#team", str(team["id"]))
    text = page.locator("#upgrades").inner_text()
    assert "an add now uses next week's moves (6 of 7 left) and plays from Monday" in text
    assert "No moves left" not in text
    assert errors == []
    ctx.close()


def test_update_now_opens_the_workflow_and_reloads_when_new_numbers_land(browser, server, data):
    """Update now (Jack's ask, Oct 12) opens the workflow on GitHub, shows the run starting, and reloads on fresh data."""
    import datetime as dt
    ctx, page, errors = open_page(browser, server, data=data, clock="install")
    now = page.evaluate("new Date().toISOString()")
    run = {"status": "in_progress", "conclusion": None, "created_at": now, "updated_at": now, "html_url": "https://github.com/x"}
    page.route("https://api.github.com/**", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"workflow_runs": [run]})))
    page.evaluate("window.opened = []; window.open = (u) => { window.opened.push(u); return null; }; 0")
    page.click("#refresh")
    # a league member can't press Run workflow on GitHub (handoff 421): told so, with the next scheduled update
    msg = page.locator("#refreshmsg").inner_text()
    assert "Only the site owner can start an update" in msg and "next scheduled update is about" in msg and "Run workflow" not in msg
    assert page.evaluate("window.opened") == []
    page.click("#iamowner")
    assert page.evaluate("localStorage.getItem('faf_owner')") == "1"
    assert page.evaluate("window.opened") == ["https://github.com/Jvansant122/Fantasy-hockey/actions/workflows/update.yml"]
    page.wait_for_function("document.getElementById('refreshmsg').textContent.includes('is running')")
    assert page.evaluate("localStorage.getItem('faf_refresh')")
    # the run fails: the page says so, keeps its numbers and stops watching
    run.update(status="completed", conclusion="failure")
    page.clock.run_for(31_000)
    page.wait_for_function("document.getElementById('refreshmsg').textContent.includes(\"didn't finish\")")
    assert page.evaluate("localStorage.getItem('faf_refresh')") is None
    # pressed again and new numbers are published: the page reloads with them
    page.click("#refresh")
    fresh = dict(data, updated_utc=(dt.datetime.fromisoformat(now.replace("Z", "+00:00")) + dt.timedelta(minutes=2)).isoformat())
    page.route("**/data/players.json", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(fresh)))
    with page.expect_navigation():
        page.clock.run_for(31_000)
    page.wait_for_function("document.getElementById('updated').textContent.startsWith('Matchup')")
    assert page.evaluate("localStorage.getItem('faf_refresh')") is None
    assert page.locator("#refresh").inner_text() == "Update now"
    assert errors == []
    ctx.close()


def test_season_range_shows_upside(browser, server, data):
    """Rookies and second-year skaters show their Season upside in the table, the details and the drop list (findings 345)."""
    if not data["rated"]:
        pytest.skip("unrated data")
    d = json.loads(json.dumps(data))
    for i, p in enumerate(q for q in d["players"] if q["pos"] != "G" and q.get("cr_season") is not None):
        p["cr_why"]["season_range"] = [max(p["cr_season"] - 0.4, 0), p["cr_season"] + 0.5]
        if i % 2 == 0:
            p["cr_why"]["exp"] = "rookie"
    ctx, page, errors = open_page(browser, server, d)
    assert page.locator("#rows .upside").count() > 0
    drops = ""
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        drops += page.locator("#upgrades").inner_text()
    assert "rookie, upside" in drops
    page.locator("#rows tr[data-id]:has(.upside)").first.click()
    assert "80% range" in page.locator("#rows tr.why").inner_text()
    assert errors == []
    ctx.close()


def test_table_draws_the_first_rows_then_the_rest_on_scroll(browser, server):
    """The first 60 rows draw at once and the rest as the list scrolls near them (speed audit plan 1); details still open."""
    ctx, page, errors = open_page(browser, server, phone=True, all_rows=False)
    total = page.evaluate("state.data.players.length")
    page.locator("#who").select_option("every")
    assert rows(page) == 60 and page.locator("#rows tr.more").count() == 1 and total > 60
    for _ in range(20):
        page.locator("#rows tr.more").last.scroll_into_view_if_needed() if page.locator("#rows tr.more").count() else None
        page.wait_for_timeout(100)
    assert rows(page) == min(300, total) and page.locator("#rows tr.more").count() == 0
    page.locator("#rows tr[data-id]").nth(200).click()
    assert page.locator("#rows tr.why").count() == 1
    assert not errors, errors
    ctx.close()


def test_phone_table_leaves_out_its_hidden_columns(browser, server):
    """A phone draws only the 5 phone columns until "All columns" is on, and redraws when the window widens (audit plan 2)."""
    ctx, page, errors = open_page(browser, server, phone=True)
    cells = lambda: page.evaluate("document.querySelector('#rows tr[data-id]').children.length")
    heads = lambda: page.evaluate("document.querySelectorAll('#head th').length")
    assert cells() == heads() == 5
    page.locator("#allcols").check()
    assert cells() == heads() == page.evaluate("COLS.length")
    page.locator("#allcols").uncheck()
    assert cells() == 5
    page.set_viewport_size({"width": 1280, "height": 900})
    page.wait_for_function("document.querySelectorAll('#head th').length === COLS.length")
    page.locator("#rows tr[data-id]").first.click()
    assert page.evaluate("+document.querySelector('#rows tr.why td').colSpan") == page.evaluate("COLS.length")
    assert not errors, errors
    ctx.close()


def test_overnight_monday_counts_the_new_matchups_moves(browser, server, data):
    """Monday 0-4 AM with overnight data: adds since Sunday's first puck use the new matchup's moves, so a team that used
    all of last week's still has its fresh ones (handoff 415)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = _sunday(data)
    d["move_limit_next"] = 7
    for t in d["teams"]:
        t["moves_used"], t["moves_used_next"] = 7, 0
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(10, 12, 3, 30))
    assert page.evaluate("stale(state.data)") and not page.evaluate("weekOver(state.data)")
    page.select_option("#team", str(d["teams"][0]["id"]))
    text = page.locator("#upgrades").inner_text()
    assert "No moves left" not in text
    assert "uses the new matchup's moves (7 of 7 left) and plays from today" in text
    assert errors == []
    ctx.close()


def test_next_weeks_moves_hold_all_but_two(browser, server, data):
    """From Sunday's first puck to Monday's first run, Best moves offers next week's 2 Monday moves once, not 2 more at every
    update; Mid-week swaps offers subs for next week (handoff 416)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    for clock in (SUN_6PM, _et(10, 12, 5, 30)):  # Sunday night, and Monday after ESPN's rollover before the first run
        for used, most in ((0, 2), (2, 0)):
            d = _sunday(data)
            d["move_limit_next"] = 7
            for t in d["teams"]:
                t["moves_used_next"] = used
            for p in d["players"]:
                if p.get("team_id") is not None:
                    p.update(injury=None, sat_last=False)
            ctx, page, errors = open_page(browser, server, data=d, clock=clock)
            counts = []
            for t in d["teams"]:
                page.select_option("#team", str(t["id"]))
                best = _card(page, "Best")
                counts.append(0 if "is held for each night" in best else len([l for l in best.split("\n")[1:] if l.strip()]))
            assert max(counts) == most, (clock, used, counts)
            assert errors == []
            ctx.close()
    # a player who sat Sunday's game gets a sub for next week, though no free agent has a game left this week
    d = _sunday(data)
    d["move_limit_next"] = 7
    team = d["teams"][0]
    sk = next(p for p in _mine(d, team) if p["pos"] != "G" and not p.get("ir"))
    sk.update(sat_last=True, dfo_out=None, games_left=1, injury=None)
    ctx, page, errors = open_page(browser, server, data=d, clock=SUN_6PM)
    page.select_option("#team", str(team["id"]))
    swaps = _card(page, "Mid-week swaps")
    assert "→" in swaps and "Season" in swaps, swaps
    assert errors == []
    ctx.close()


def test_phone_hides_the_season_percentile(browser, server, data):
    """Phones show no percentile badge next to Season, with or without All columns (handoff 417)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    ctx, page, errors = open_page(browser, server, phone=True)
    page.select_option("#team", str(data["teams"][0]["id"]))
    shown = """(key => { const i = [...document.querySelectorAll('#head th')].findIndex(th => th.dataset.k === key);
      return [...document.querySelectorAll('#rows tr[data-id]')].map(r => r.children[i]?.querySelector('.pct')).filter(b => b && b.offsetParent).length; })"""
    assert page.evaluate(f"{shown}('cr_season')") == 0
    assert page.evaluate(f"{shown}('cr')") > 0  # Week keeps its badge
    page.check("#allcols")
    assert page.evaluate(f"{shown}('cr_season')") == 0
    assert page.evaluate(f"{shown}('cr')") > 0
    assert errors == []
    ctx.close()


def test_trade_check_takes_one_partner(browser, server, data):
    """ESPN trades are between two teams: once a player to get is picked, only his team's players are offered (handoff 418)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    ctx, page, errors = open_page(browser, server)
    page.select_option("#team", str(data["teams"][0]["id"]))
    assert page.locator("#tget optgroup").count() > 1
    page.select_option("#tget", page.locator("#tget option").nth(1).get_attribute("value"))
    assert page.locator("#tget optgroup").count() == 1
    assert "Add another player from" in page.locator("#tget option").first.inner_text()
    # a saved trade with players from two teams keeps only the first one's
    other = page.evaluate("""(() => { const first = state.data.players.find(p => p.id === state.tget[0]);
      return state.data.players.find(p => p.team_id != null && p.team_id !== first.team_id && String(p.team_id) !== state.team
        && p.owner !== 'freeagent' && p.owner !== 'waivers').id; })()""")
    page.evaluate(f"state.tget.push({other}); renderTrade(); 0")
    assert page.evaluate("state.tget.length") == 1
    page.locator("#trade button[data-rm^='get:']").first.click()
    assert page.locator("#tget optgroup").count() > 1
    assert errors == []
    ctx.close()


def test_goalie_tonight_news_only_for_tonight(browser, server, data):
    """A Daily Faceoff starter tag and 'counted as certain' show only on today's data for a goalie whose team plays tonight,
    and a waiver goalie isn't 'counted as certain' (handoff 419)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = _weekday(data, 2)  # Wed Oct 7, 10 AM run, first puck 7 PM
    tonight = d["nights"][2]["sp"]
    plays, idle = [p for p in d["players"] if p["pos"] == "G" and p["owner"] in ("freeagent", "waivers")][:2]
    for g in (plays, idle):
        g.update(owner="freeagent", today_start=None)
        g["cr_why"] = dict(g.get("cr_why") or {}, tonight={"status": "Confirmed", "starting": True})
    plays["nights"] = sorted(set(plays.get("nights") or []) | {tonight})
    idle["nights"] = [n for n in idle.get("nights") or [] if n != tonight]
    js = "(id => { const p = state.data.players.find(p => p.id === id); return [cells(p).name, why(p)]; })"
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(10, 7, 12))
    name, why = page.evaluate(f"{js}({plays['id']})")
    assert "starts tonight" in name and "confirmed, counted as certain" in why
    name, why = page.evaluate(f"{js}({idle['id']})")
    assert "starts tonight" not in name and "Daily Faceoff:" not in why
    page.evaluate(f"state.data.players.find(p => p.id === {plays['id']}).owner = 'waivers'; 0")
    assert "counted as certain" not in page.evaluate(f"{js}({plays['id']})")[1]
    assert errors == []
    ctx.close()
    # the next morning before the 6 AM run: that news was about last night
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(10, 8, 3))
    name, why = page.evaluate(f"{js}({plays['id']})")
    assert "starts tonight" not in name and "Daily Faceoff:" not in why
    assert errors == []
    ctx.close()


def test_details_wording_for_news_cuts_and_small_samples(browser, server, data):
    """News that cut a player's Season share isn't 'no injury discount' (handoff 420); a last-season rate from a few games
    gives the count, and the form shift is in percentage points (handoff 422)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = json.loads(json.dumps(data))
    p = next(p for p in d["players"] if p["pos"] != "G" and p.get("cr_season") is not None and p.get("cr_why"))
    p["cr_why"].update(season_share=None, season_news=["surgery"], lineup_slack=1.4, last_fpg=3.3, last_season=None,
                       form={"why": ["slump"], "shift": -0.07})
    p["last_gp"] = 1
    ctx, page, errors = open_page(browser, server, data=d)
    why = page.evaluate(f"why(state.data.players.find(p => p.id === {p['id']}))")
    assert "no injury discount" not in why and "share cut by the Daily Faceoff news below" in why
    assert "only partly offsets the news below" in why
    assert "last season 3.3 pts/game (1 GP)" in why
    assert "(−7 percentage points on his chance to play" in why
    assert errors == []
    ctx.close()


def with_winners(data):
    """The committed data plus a Predicted winners section: a finished game with a pick, one without, one to come with a
    betting line, and two fantasy matchups (the first data run after the tab shipped writes the real one)."""
    d = json.loads(json.dumps(data))
    day = d["updated_date"]
    ids = [t["id"] for t in d["teams"]]
    names = {t["id"]: t["name"] for t in d["teams"]}
    game = lambda home, away, p, **kw: {"date": day, "start": f"{day}T23:00:00Z", "home": home, "away": away, "home_name": None,
                                        "away_name": "Bruins", "p_home": p, "inputs": {"pts": [8.5, 8.0], "gd": [0.2, 0.1], "sv": [0.9, 0.9], "b2b": [0, 1]},
                                        "state": "FUT", "started": False, **kw}
    d["winners"] = {
        "games": [game("TOR", "BOS", 0.61, started=True, state="OFF", home_score=2, away_score=3, last="OT", home_won=False, frozen=True),
                  game("MTL", "OTT", None, started=True, state="OFF", home_score=4, away_score=1, last="REG", home_won=True),
                  game("NYR", "NJD", 0.45, start="2099-01-01T00:00:00Z", p_line=0.48)],
        "record": {"games": 1, "right": 0, "home_right": 0},
        "matchups": [{"home": {"id": a, "name": names[a], "now": 40.0, "rest": 60.2, "total": 100.2},
                      "away": {"id": b, "name": names[b], "now": 30.0, "rest": 62.0, "total": 92.0}, "p_home": 0.7, "sd": 14.0}
                     for a, b in zip(ids[:4:2], ids[1:4:2])],
    }
    return d


@pytest.mark.parametrize("phone", [False, True])
def test_predicted_winners_tab(browser, server, data, phone):
    """Jack's Oct 14 ask: a Predicted winners tab. It hides the free agent cards, shows each game's pick and result and the
    league's matchups, is remembered across reloads, and fits a phone."""
    ctx, page, errors = open_page(browser, server, with_winners(data), phone=phone)
    assert page.locator("#winview").is_hidden() and page.locator("#upgcard").is_visible()
    page.select_option("#team", str(data["teams"][0]["id"]))
    page.click("#tab-win")
    assert page.locator("#upgcard").is_hidden() and page.locator("#faview").is_hidden()
    assert page.locator("#nhlgames .game").count() == 3
    text = page.locator("#nhlgames").inner_text()
    assert "pick wrong" in text and "no pick" in text and "betting line 52% for the pick" in text
    assert "0 of 1 right" in page.locator("#nhlhead").inner_text()
    assert page.locator("#fantasy .matchup").count() == 2
    assert page.locator("#fantasy .matchup").first.get_attribute("class").endswith("mine")  # your matchup first
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.reload()
    page.wait_for_function("document.getElementById('updated').textContent.startsWith('Matchup')")
    assert page.locator("#winview").is_visible() and page.get_attribute("#tab-win", "aria-selected") == "true"
    page.click("#tab-fa")
    assert page.locator("#upgcard").is_visible() and page.locator("#winview").is_hidden()
    assert errors == []
    ctx.close()


def test_predicted_winners_before_the_first_run(browser, server, data):
    """Data from before the tab shipped has no winners: the tab says when they come, with no error."""
    d = json.loads(json.dumps(data))
    d.pop("winners", None)
    ctx, page, errors = open_page(browser, server, d)
    page.click("#tab-win")
    assert "next data update" in page.locator("#nhlhead").inner_text()
    assert errors == []
    ctx.close()


def test_trade_check_goalie_rules(browser, server, data):
    """A forced drop never leaves fewer than two healthy goalies (handoff 457), and a goalie shortfall you already have
    isn't charged to the trade (handoff 459)."""
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = json.loads(json.dumps(data))
    team, other = d["teams"][0], d["teams"][1]
    mine = _mine(d, team)
    for p in mine:
        p.update(ir=False, injury=None)
    gs = [p for p in mine if p["pos"] == "G"]
    sk = [p for p in mine if p["pos"] != "G"]
    if len(gs) < 2 or len(sk) < 2:
        pytest.skip("needs two goalies")
    for g in gs:  # goalies have the lowest keep value, so they'd be dropped first without the rule
        g.update(cr_season=0.0, cr_fpg=0.0, cr=0.0)
    d["players"] = [p for p in d["players"] if not (str(p.get("team_id")) == str(team["id"]) and p["pos"] == "G" and p not in gs[:3])]
    gs = gs[:3]
    mine = _mine(d, team)
    d["ir_slots"], d["roster_max"] = 0, len(mine)
    theirs = [p for p in _mine(d, other) if p["pos"] != "G" and not p["ir"]]
    ctx, page, errors = open_page(browser, server, data=d)
    page.select_option("#team", str(team["id"]))
    # give one goalie for two skaters: one drop, and it can't be a goalie when only two would be left
    text = _trade(page, [gs[0]["id"]], [theirs[0]["id"], theirs[1]["id"]])
    assert "You'd need to drop" in text, text
    assert not any(f"drop {g['name']}" in text for g in gs[1:]), text
    # one goalie hurt before the trade, a skater swap: no goalie added to the trade's numbers
    page.evaluate(f"state.data.players.filter(p => {json.dumps([g['id'] for g in gs[1:]])}.includes(p.id)).forEach(p => p.injury = 'DAY_TO_DAY')")
    text = _trade(page, [sk[0]["id"]], [theirs[0]["id"]])
    assert "with or without this trade" in text and "add " not in text, text
    assert errors == []
    ctx.close()
