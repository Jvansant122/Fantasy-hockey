"""Hunt 14 mutate2: page lines from PRs #210-#226 no test pinned (copy into tests/). Passes on main af32a42; kills the mutants
named in each docstring (hunt14 mutate2 RESULTS.md)."""
import json
import re
from datetime import datetime

import pytest

from test_page import browser, data, open_page, server  # noqa: F401 - pytest fixtures


def test_goalie_details_name_the_daily_faceoff_news_cut(browser, server, data):  # noqa: F811
    """Handoffs 695/697: a goalie whose starts the Daily Faceoff injury news cut says so in his details, with the news day;
    one without the field says nothing about it. Kills: B09."""
    d = json.loads(json.dumps(data))
    g = next((p for p in d["players"] if p["pos"] == "G" and p.get("cr_why") is not None), None)
    if g is None:
        pytest.skip("needs a rated goalie")
    g["cr_why"].pop("dfo_inj", None)
    plain = dict(g, id=-1, cr_why=dict(g["cr_why"]))
    g["cr_why"]["dfo_inj"] = d["updated_utc"][:10]
    d["players"].append(plain)
    ctx, page, errors = open_page(browser, server, data=d, clock=datetime.fromisoformat(d["updated_utc"].replace("Z", "+00:00")))
    text = page.evaluate(f"why(state.data.players.find(p => p.id === {json.dumps(g['id'])}))")
    other = page.evaluate("why(state.data.players.find(p => p.id === -1))")
    assert "Daily Faceoff injury news" in text and "Daily Faceoff injury news" not in other, text
    assert errors == []
    ctx.close()


def test_activate_drop_gets_no_mid_week_sub(browser, server, data):  # noqa: F811
    """Handoff 724: when Activate from IR says "drop X first", the IR player takes X's spot for no move, so X's Mid-week swaps
    line offers no sub and says where his spot goes. Each team's weakest skater sat its last game (so he is both the
    Activate drop and on Mid-week swaps). Kills: F02 (sub offered), F03 (spot note dropped)."""
    from test_audit_invariants import _ir_full
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d = _ir_full(data)
    for t in d["teams"]:
        sk = sorted([p for p in d["players"] if str(p.get("team_id")) == str(t["id"]) and p["pos"] != "G" and not p["ir"]
                     and not p.get("injury")], key=lambda p: (p.get("cr") or 0) + 7 * (p.get("cr_season") or 0))
        if sk:
            sk[0].update(sat_last=True, dfo_out=None, games_left=max(sk[0].get("games_left") or 0, 1), cr=0.0, cr_season=0.0,
                         cr_games=0.0)
    ctx, page, errors = open_page(browser, server, data=d)
    seen, bad = 0, []
    for t in d["teams"]:
        page.select_option("#team", str(t["id"]))
        r = page.evaluate("""() => {
          const cards = [...document.querySelectorAll('#upgrades .upg')];
          const act = cards.find(c => /^Activate from IR/.test(c.innerText));
          const m = act && act.innerText.match(/drop (.+?) first/);
          const sw = cards.find(c => /^Mid-week swaps/.test(c.innerText));
          if (!m ||!sw) return null;
          const line = [...sw.querySelectorAll(':scope > div')].map(l => l.innerText).find(l => l.startsWith(m[1]));
          return line ? {name: m[1], line} : null;
        }""")
        if r:
            seen += 1
            if "→" in r["line"] or "Activate from IR" not in r["line"]:
                bad.append((t["id"], r))
    assert errors == []
    ctx.close()
    assert seen, "no team's Activate drop was on Mid-week swaps"
    assert not bad, bad[:3]


def _stream_drops(page, teams):
    out = {}
    for t in teams:
        page.select_option("#team", str(t["id"]))
        st = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].map(c => c.textContent.replace(/\\s+/g, ' '))"
                           ".find(c => c.startsWith('Stream')) ||''")
        m = re.search(r"Your roster is full: drop (.+?)( \(or trade him: see Trade ideas\))? to add him", st)
        if m:
            out[t["id"]] = (m.group(1), bool(m.group(2)))
    return out


def test_stream_drop_that_is_a_trade_chip_says_so(browser, server, data):  # noqa: F811
    """Handoff 726 (findings 233): every card that drops a Trade-ideas chip says so, the Stream card too. The Stream card's
    skater drop is made a healthy chip (another team's skater ESPN projects the same, with a Season rating 1 higher); where
    he is still the Stream drop and Trade ideas offers him, the card must add "(or trade him: see Trade ideas)". Kills: F06."""
    from test_audit_invariants import _et, _thursday
    if not data.get("rated"):
        pytest.skip("needs rated data")
    d, tonight = _thursday(data)
    fa_g = next((p for p in d["players"] if p["pos"] == "G" and p["owner"] == "freeagent" and p.get("cr_matched")
                 and tonight in (p.get("nights") or [])), None)
    if fa_g is None:
        pytest.skip("needs a free-agent goalie playing tonight")
    fa_g.update(injury=None, games_left=max(fa_g["games_left"], 1), today_start=d["first_start"])
    fa_g["cr_why"]["tonight"] = {"starting": True, "status": "Confirmed"}
    for p in d["players"]:
        if p["pos"] == "G" and p is not fa_g and p.get("cr_why"):
            p["cr_why"].pop("tonight", None)
            p["cr_why"]["p_today"] = 0
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
    first = _stream_drops(page, d["teams"])
    ctx.close()
    byname = {(str(p.get("team_id")), p["name"]): p for p in d["players"]}
    made = 0
    for tid, (name, _) in first.items():
        x = byname.get((str(tid), name))
        if not x or x["pos"] == "G" or x.get("ir") or x.get("cr_season") is None:
            continue
        x.update(injury=None, sat_last=False, last_gp=max(x.get("last_gp") or 0, 40), proj_ppg=x.get("proj_ppg") or 1.0)
        o = next((p for p in d["players"] if p["pos"] == x["pos"] and p.get("team_id") is not None and p["owner"] != "freeagent"
                  and str(p["team_id"]) != str(tid) and not p.get("ir") and not p.get("injury") and not p.get("sat_last")
                  and not p.get("pending_drop")), None)
        if o is None:
            continue
        o.update(proj_ppg=x["proj_ppg"], cur_ppg=x.get("cur_ppg"), gp=x.get("gp"), last_ppg=x.get("last_ppg"), last_gp=40,
                 cr_season=x["cr_season"] + 1.0)
        made += 1
    if not made:
        pytest.skip("no Stream card drops a skater in this data")
    ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 16))
    again = {}
    for t in d["teams"]:
        if t["id"] not in first:
            continue
        page.select_option("#team", str(t["id"]))
        chips = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].map(c => c.innerText).filter(c => /^Trade ideas/.test(c))"
                              ".join('\\n')")
        again.update({k: v + (chips,) for k, v in _stream_drops(page, [t]).items()})
    assert errors == []
    ctx.close()
    chipped = {tid: v for tid, v in again.items() if re.search(rf"Offer {re.escape(v[0])}\b", v[2])}
    assert chipped, "no Stream drop became a trade chip"
    assert all(note for _, note, _ in chipped.values()), chipped



def test_one_update_offers_at_most_a_days_adds(browser, server, data):  # noqa: F811
    """Handoffs 488/727: one update offers at most MOVES_PER_DAY adds across Best moves, the Stream card, the IR stash (2
    moves with a full roster) and the Drop list's 'replace with' (the Drop list takes only what the others leave), and never
    more than the moves left after Mid-week swaps. Checked Thursday with 0-6 moves used, with and without games on Friday and
    Saturday (fewer nights to hold for), with and without a free-agent goalie confirmed tonight. Kills: F08b (Drop list unlimited), F09 (Best moves ignored), F10 (IR stash ignored),
    F11 (Stream ignored), F16 (the 4-a-day cap ignored)."""
    from test_audit_invariants import _adds_offered, _et, _ir_full, _plans, _thursday
    if not data.get("rated"):
        pytest.skip("needs rated data")
    bad, seen = [], 0
    for quiet, conf in ((False, False), (True, False), (False, True), (True, True)):
        for used in (0, 2, 4, 6):
            d, tonight = _thursday(data)
            d = _ir_full(d)
            if conf:  # the Stream card's starter takes a move (streamNeed)
                fa_g = next((p for p in d["players"] if p["pos"] == "G" and p["owner"] == "freeagent" and p.get("cr_matched")
                             and tonight in (p.get("nights") or [])), None)
                if fa_g is None:
                    continue
                fa_g.update(injury=None, games_left=max(fa_g["games_left"], 1), today_start=d["first_start"])
                fa_g["cr_why"]["tonight"] = {"starting": True, "status": "Confirmed"}
            if quiet:  # no games Friday or Saturday: one night to hold a move for
                for n in d["nights"][-3:-1]:
                    n["games"] = 0
            for t in d["teams"]:
                t["moves_used"] = used
            ctx, page, errors = open_page(browser, server, data=d, clock=_et(15, 10))
            per_day = page.evaluate("MOVES_PER_DAY")
            for t in d["teams"]:
                page.select_option("#team", str(t["id"]))
                n, r = _adds_offered(page), _plans(page)
                stash = page.evaluate("[...document.querySelectorAll('#upgrades .upg')].some(c => /^IR stash/.test(c.innerText) && /2 moves/.test(c.innerText))")
                replace = sum("replace with" in l for l in r["dropList"])
                today = n["best"] + n["stream"] + n["stash"] * (2 if stash else 1) + replace
                seen += replace
                if today > per_day or today + n["swaps"] > 7 - used:
                    bad.append((quiet, conf, used, t["id"], n, replace))
            assert errors == []
            ctx.close()
    assert seen, "no Drop list replacement offered in any scenario"
    assert not bad, bad[:4]
