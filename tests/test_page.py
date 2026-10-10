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
