"""Day-of feeds for the rating, and a log of them to test after a few weeks.

- Daily Faceoff starting goalies: confirmed starters feed the goalie rating (research findings section 7).
- Logged only, to be scored later (research web-leads.md leads 1-4, findings section 25): Daily Faceoff
  starters at every label, Daily Faceoff projected lineups, ESPN's injury notes, NHL/DraftKings moneylines,
  and a snapshot of each player's rating and ESPN projection.

Daily Faceoff's robots.txt allows these pages (not its /api/), so we read the pages at low volume.
"""
import gzip
import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "log"
DF = "https://www.dailyfaceoff.com"
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
# Daily Faceoff team abbreviations that differ from the NHL's
DF_TO_NHL = {"NAS": "NSH", "VEG": "VGK", "TB": "TBL", "NJ": "NJD", "SJ": "SJS", "LA": "LAK", "UTAH": "UTA", "WAS": "WSH", "MON": "MTL", "CLB": "CBJ"}
NHL_TEAM = {}  # Daily Faceoff team name -> NHL abbrev, filled from the lineup page's team list


def page_data(s, url):
    r = s.get(url, timeout=30)
    r.raise_for_status()
    m = NEXT_DATA.search(r.text)
    if not m:
        raise ValueError(f"no page data in {url}")
    return json.loads(m.group(1))["props"]["pageProps"]


def fetch_starters(s, day):
    """Tonight's goalies from Daily Faceoff for one date: {date, games: [{team, opp, home, goalie, status, ...}]}.

    status is Daily Faceoff's label: Confirmed (a team source or beat writer named him), Likely, Unconfirmed."""
    if not NHL_TEAM:
        load_teams(s)
    pp = page_data(s, f"{DF}/starting-goalies/{day.isoformat()}")
    out = []
    for g in pp.get("data", []):
        for side, other in (("home", "away"), ("away", "home")):
            out.append({
                "team": NHL_TEAM.get(g.get(f"{side}TeamName")), "opp": NHL_TEAM.get(g.get(f"{other}TeamName")), "home": side == "home",
                "goalie": g.get(f"{side}GoalieName"), "status": g.get(f"{side}NewsStrengthName"),
                "news_at": g.get(f"{side}NewsCreatedAt"), "news": g.get(f"{side}NewsDetails"),
                "source": g.get(f"{side}NewsSourceName"), "start_utc": g.get("dateGmt"),
            })
    return {"date": pp.get("date"), "games": out}


def load_teams(s):
    """Daily Faceoff's team list (name, slug, abbrev); also fills NHL_TEAM."""
    teams = page_data(s, f"{DF}/teams/buffalo-sabres/line-combinations").get("sortedTeams", [])
    for t in teams:
        NHL_TEAM[t["name"]] = DF_TO_NHL.get(t["shortName"], t["shortName"])
    return teams


def fetch_lineups(s):
    """Every team's projected lineup: {NHL abbrev: {updated, players: [[name, group, injury, game-time decision]]}}."""
    def one(t):
        try:
            c = page_data(s, f"{DF}/teams/{t['slug']}/line-combinations").get("combinations") or {}
        except Exception as e:  # noqa: BLE001 - one team failing shouldn't lose the rest
            return {"error": repr(e)}
        time.sleep(0.5)  # each of the 4 readers pauses between pages, so Daily Faceoff sees a few pages a second at most
        return {"updated": c.get("updatedAt"), "source": c.get("sourceName"),
                "players": [[p.get("name"), p.get("groupIdentifier"), p.get("injuryStatus"), p.get("gameTimeDecision")]
                            for p in c.get("players", [])]}
    teams = load_teams(s)
    # 4 at a time: one by one, the 32 pages and pauses took about 20 s of a 40 s update (speed audit, Oct 12 2026)
    with ThreadPoolExecutor(4) as ex:
        return {NHL_TEAM[t["name"]]: v for t, v in zip(teams, ex.map(one, teams))}


DRESSED = {"f1", "f2", "f3", "f4", "d1", "d2", "d3", "d4"}


def plain(name):
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower())


def name_words(name):
    """ASCII lowercase words of two or more letters ("Elias N. Pettersson" -> ["elias", "pettersson"])."""
    return [w for w in re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
                              .lower().replace("-", " ")).split() if len(w) > 1]


def older(updated, start):
    """The lineup's update time is before the game start (both ISO); unknown times are never older."""
    try:
        return bool(updated and start) and datetime.fromisoformat(updated.replace("Z", "+00:00")) < datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return False


def mark_lineups(players, lineups, to_nhl, last_start=None):
    """p["dfo_out"]: True when Daily Faceoff has his team's lineup and he isn't in a forward or defense line (scratched,
    injured or not listed), False when he is, None when the lineup is missing. Skaters only (findings section 65).
    A name that isn't in the lineup exactly can match one on his team with the same last name and first two letters of
    the first name (Gabe/Gabriel, Elias N./Elias Nils; findings 175), skipping names another skater matches exactly;
    with several, the one whose line fits his position. A lineup posted before his team's last game started
    ({NHL abbrev: UTC ISO} in last_start) was for that game, not the next one, so it counts as missing (findings 182)."""
    groups = {team: [(n, group) for n, group, *_ in t.get("players", []) if group in DRESSED]
              for team, t in (lineups or {}).items() if t.get("players") and not older(t.get("updated"), (last_start or {}).get(team))}
    exact = {}
    for p in players:
        if p["pos"] != "G":
            exact.setdefault(to_nhl.get(p["team"], p["team"]), set()).add(plain(p["name"]))
    for p in players:
        team = to_nhl.get(p["team"], p["team"])
        lineup = groups.get(team) if p["pos"] != "G" else None
        if lineup is None:
            p["dfo_out"] = None
            continue
        if plain(p["name"]) in {plain(n) for n, _ in lineup}:
            p["dfo_out"] = False
            continue
        w = name_words(p["name"])
        cands = [(n, group) for n, group in lineup if len(w) >= 2 and plain(n) not in exact.get(team, ())
                 and (nw := name_words(n)) and len(nw) >= 2 and nw[-1] == w[-1] and nw[0][:2] == w[0][:2]]
        fits = [c for c in cands if (c[1][0] == "d") == (p["pos"] == "D")]
        p["dfo_out"] = not (len(cands) == 1 or fits)


def fetch_injuries(s):
    r = s.get("https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries", timeout=30)
    r.raise_for_status()
    def athlete_id(i):  # ESPN's athlete id (the fantasy player id) is only in the player card link
        m = re.search(r"/id/(\d+)", " ".join(l.get("href", "") for l in (i.get("athlete") or {}).get("links", [])))
        return int(m.group(1)) if m else None
    return [{"team": t.get("displayName"), "name": (i.get("athlete") or {}).get("displayName"), "espn_id": i.get("id"),
             "athlete_id": athlete_id(i),
             "status": i.get("status"), "date": i.get("date"), "short": i.get("shortComment"), "long": i.get("longComment")}
            for t in r.json().get("injuries", []) for i in t.get("injuries", [])]


# Daily Faceoff player news for the Season rating (findings 360 (1)): each player's latest injury item and latest roster move
# (call-up / send-down), kept in NEWS_FILE so a run only reads the pages posted since the last one
NEWS_FILE = Path(__file__).resolve().parent.parent / "data" / "dfo_news.json"
NEWS_DAYS, NEWS_KEEP_DAYS, NEWS_MAX_PAGES = 60, 365, 60
# season-level wording (findings 360, 371): hunt 11b's flag, or a season phrase tied to an absence (handoff 401, idea 1114,
# hunt13/n_fix.py): "fully healthy for the playoffs" and "ready for the season opener" no longer count
NEWS_SEASON_907 = re.compile(r"(out for|miss|remainder of|rest of) (the|this|his) (regular )?season|season[- ]ending|out for the year", re.I)
NEWS_SEASON = re.compile(r"(out|miss|sidelined|shut down|done|sit out)[^.]{0,40}(for (the|this|his) (rest of (the|this|his) |remainder of (the|this|his) )?(\d{4}-\d{2} )?(regular )?(season|campaign)|for the year|rest of (the|this|his) (\d{4}-\d{2} )?(regular )?(season|campaign)|remainder of (the|this|his) (\d{4}-\d{2} )?(regular )?(season|campaign))"
                         r"|until (at least )?the playoffs|(?<!healthy )(?<!ready )(?<!back )(?<!available )(?<!return )(?<!in time )(?<!time )(before|for) the (start of the )?playoffs\b(?![^.]{0,30}(healthy|ready))|(not|won.t|will not|isn.t expected to) (return|play|be back)[^.]{0,30}(this season|this year|regular season|before the playoffs)"
                         r"|season[- ]ending|season is over|end of the (regular )?season", re.I)
NEWS_INDEFINITE = re.compile(r"indefinit|long[- ]term|extended period|foreseeable|significant (amount of )?time|considerable time|several months|multiple months", re.I)
NEWS_LTIR = re.compile(r"long[- ]term injured reserve|LTIR", re.I)
# a negation just before an indefinite phrase ("not expected to be a long-term issue") or a past season around either
# ("missed significant time last season") skips that match
NEWS_NEG = re.compile(r"\bnot\b|n't|\bno longer\b|\bunlikely\b|\bnot expected\b|\bshouldn't\b", re.I)
NEWS_PAST = re.compile(r"last (season|year|campaign)|previous (season|year)|in \d{4}|a year ago", re.I)
# a season phrase just after "healthy", "ready", "back" ... is a return, not an absence (handoff 410)
NEWS_RETURN = re.compile(r"(healthy|ready|back|available|return|in time|cleared|good to go)[^.]{0,25}$", re.I)
NEWS_VERSION = 9  # bumped when the wording rules change: stored items are read again from scratch with the new ones
NEWS_SURGERY = re.compile(r"surgery|surgical|operation", re.I)
NEWS_SURGERY_PAST = re.compile(r"offseason|off-season|this summer|last summer|\d{4}-\d{2} season", re.I)
# "no surgery required", "won't need surgery", "avoided surgery" (handoff 404)
NEWS_SURGERY_NEG = re.compile(r"\b(no|not|without|avoid(ed|s)?)\b|n't", re.I)
NEWS_SURGERY_NOT_NEEDED = re.compile(r"^\W*(is |was |will be )?(not|n't) (required|needed|necessary)", re.I)
NEWS_DOWN = re.compile(r"(assigned|sent|reassigned|loaned|returned) (down )?to (the )?[A-Za-z .]*\(AHL\)|sent down|assigned to (the )?AHL"
                       r"|to the AHL|conditioning loan", re.I)
NEWS_UP = re.compile(r"recalled|called up|call[- ]up|summoned", re.I)


# the Week dress's wording (handoffs 406-408, hunt13/p_stage.py, p_week.py and hunt11b/news.py): each recent item keeps the
# flags it matched, for NEWS_RECENT_DAYS
NEWS_RECENT_DAYS = 45
NEWS_WEEK = {k: re.compile(v, re.I) for k, v in {
    "skate": r"skat(ed|ing|es) on (his|their) own|individual(ly)? skat|skated (before|after|prior)|on[- ]ice workout|back on the ice|returned to the ice",
    "limited": r"no[- ]contact|non[- ]contact|limited (participant|practice|role)|red jersey|yellow jersey|regular jersey",
    "full": r"full participant|practiced (in full|fully)|full[- ]contact|full practice|took part in (a )?(full )?practice|returned to practice|practiced (with|on) (the|his)",
    "gtd": r"game[- ]time decision|questionable|probable|could return|may return|might return|possible return|hopeful",
    "ret": r"will (return|play|be in the lineup|suit up|make his return|rejoin|draw back)|is expected to (return|play|rejoin)|activated"
           r"|returns? to (the )?lineup|has been cleared|cleared to (play|return)|back in the lineup"
           r"|full participant|practiced (in full|fully)|full[- ]contact|took part in practice|returned to practice|no[- ]contact jersey|non-contact",
    "dtd": r"day[- ]to[- ]day", "wtw": r"week[- ]to[- ]week", "mtm": r"month[- ]to[- ]month", "ltir": r"\bLTIR\b|long[- ]term injured reserve",
    "out": r"will not|(will|'ll|to|expected to) miss|won.t play|will be out|is out|ruled out|sidelined|remains out|will sit|not expected to play",
}.items()}
# the 16-season Week dress model's inputs (handoff 413): hunt11b/news.py's flags for the latest injury item, as it read
# the archive (plain wording, no negation checks: the model learned from these)
NEWS_MODEL = {k: re.compile(v, re.I) for k, v in {
    "season": r"(out for|miss|remainder of|rest of) the (regular )?season|season[- ]ending|out for the year",
    "surgery": r"surgery|surgical|operation", "ir": r"\bIR\b|injured reserve", "ltir": r"\bLTIR\b|long[- ]term injured reserve",
    "wtw": r"week[- ]to[- ]week", "dtd": r"day[- ]to[- ]day",
    "ret": r"will (return|play|be in the lineup|suit up|make his return|rejoin|draw back)|is expected to (return|play|rejoin)|activated"
           r"|returns? to (the )?lineup|has been cleared|cleared to (play|return)|back in the lineup",
    "out": r"will not|(will|'ll|to|expected to) miss|won.t play|will be out|is out|ruled out|sidelined|remains out|will sit|not expected to play",
    "practice": r"full participant|practiced (in full|fully)|full[- ]contact|took part in practice|returned to practice|no[- ]contact jersey|non-contact",
    "gtd": r"game[- ]time decision", "illness": r"illness|flu\b|sick", "concussion": r"concussion",
}.items()}
NEWS_DETAILS_ONLY = ("skate", "limited", "full", "gtd")
NEWS_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
            "a couple": 2, "a few": 3, "several": 4, "a": 1}
_N = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|a couple|a few|several|a)"
NEWS_TIMELINE = re.compile(_N + r"(?:\s*(?:-|to|or)\s*" + _N + r")?\s*(?:more\s+|additional\s+)?(day|week|month|game)s?", re.I)


# handoff 428 (idea 1180, hunt13/p_week_tl.py): counts after these words are stat lines, and a game count needs an absence
# word in the 40 characters before it
NEWS_TL_PRE = re.compile(r"\b(in|over|through|across|last|past|first|previous|his|all|of|with)\s+(the\s+|his\s+)?$", re.I)
NEWS_TL_ABSENCE = re.compile(r"miss|\bout\b|sideline|\bsit|absence|suspen|return|week-to-week|expected|shelved|month", re.I)


def news_timeline(text):
    """The longest stated absence in days ("4-6 weeks" = 35, a game = 2.2 days), or None (hunt11b/news.py parse)."""
    num = lambda w: float(w) if w.isdigit() else NEWS_NUM.get(w.lower())
    days = None
    for m in NEWS_TIMELINE.finditer(text):
        a, b = num(m.group(1)), num(m.group(2)) if m.group(2) else None
        if a is None:
            continue
        u, pre = m.group(3).lower(), text[max(0, m.start() - 40):m.start()]
        if NEWS_TL_PRE.search(pre):  # a stat line ("52 points in 58 games", "over his last five games"), handoff 428
            continue
        if u == "game" and (re.search(r"(straight|consecutive|\d+th|missed)", text[max(0, m.start() - 20):m.end() + 15].lower())
                            or not NEWS_TL_ABSENCE.search(pre)):
            continue
        v = (a if b is None else (a + b) / 2) * {"day": 1, "week": 7, "month": 30, "game": 2.2}[u]
        days = v if days is None else max(days, v)
    return days


def news_wording(text, pat, neg=True):
    """True if a sentence of text matches pat outside a negation (neg) or a past-season context (hunt13/n_fix.py)."""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        for m in pat.finditer(sent):
            if pat is NEWS_SEASON and NEWS_RETURN.search(sent[max(0, m.start() - 40):m.start()]):
                continue
            if pat is NEWS_INDEFINITE and NEWS_LTIR.search(sent):
                continue
            if neg and NEWS_NEG.search(sent[max(0, m.start() - 30):m.start()]):
                continue
            # season wording: only a past phrase in or just before the match vetoes it ("remainder of this season"
            # after "missed most of last season" still counts, handoff 441)
            near = sent[max(0, m.start() - 20):m.end()] if pat is NEWS_SEASON else sent[max(0, m.start() - 60):m.end() + 40]
            if NEWS_PAST.search(near):
                continue
            return True
    return False


def news_surgery(text):
    """A surgery mention that isn't negated: nothing like "no" / "won't need" / "avoided" in the 25 characters before it,
    and no "not required" / "not needed" after it (handoff 404); and not a past one: no past season or offseason
    around it in its sentence (handoff 440, Barkov's 2025-26 surgery in the background paragraph)."""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        for m in NEWS_SURGERY.finditer(sent):
            if NEWS_SURGERY_NEG.search(sent[max(0, m.start() - 25):m.start()]) or NEWS_SURGERY_NOT_NEEDED.search(sent[m.end():m.end() + 30]):
                continue
            if NEWS_PAST.search(sent[max(0, m.start() - 60):m.end() + 40]) or NEWS_SURGERY_PAST.search(sent):
                continue
            return True
    return False


def news_return(text):
    """The Week "set to return" wording, except "will return to the sidelines / injured reserve / IR" and "eligible to be
    activated" (handoff 426; "activated off IR" still counts)."""
    for m in NEWS_WEEK["ret"].finditer(text):
        if re.match(r".{0,20}?\bto (the )?(sideline|injured reserve|IR\b)", text[m.end():m.end() + 40], re.I | re.S):
            continue
        if m.group(0).lower() == "activated" and re.search(r"eligible to be\s*$", text[max(0, m.start() - 20):m.start()], re.I):
            continue
        return True
    return False


def news_flags(item):
    """One news item as the research parsed it (hunt11b/news.py): season-ending / surgery for injuries, down / up for moves."""
    text = re.sub(r"\s+", " ", f'{item.get("details") or ""} {item.get("fantasyDetails") or ""}')
    details = re.sub(r"\s+", " ", item.get("details") or "")
    cat = item.get("newsCategoryName")
    return {"season": bool(NEWS_SEASON_907.search(text)) or news_wording(text, NEWS_SEASON, neg=False),
            "surgery": news_surgery(text), "indefinite": news_wording(text, NEWS_INDEFINITE),
            "down": cat == "Send Down" or bool(NEWS_DOWN.search(text)), "up": cat == "Call Up" or bool(NEWS_UP.search(text)),
            # the return-ladder flags were fit on the news line only, not the background paragraph (handoff 424)
            **{k: bool(r.search(details if k in NEWS_DETAILS_ONLY else text)) for k, r in NEWS_WEEK.items()},
            "ret": news_return(text), "tl": news_timeline(text),
            "nf": sorted(k for k, r in NEWS_MODEL.items() if r.search(text))}


def recent_item(slug, ts, f):
    """A stored recent item: its time, category and the flags it matched (the Week dress, handoffs 406-408)."""
    keys = ("season", "indefinite", *NEWS_WEEK) if slug == "injuries" else ("down", "up")
    item = {"ts": ts, "cat": "inj" if slug == "injuries" else "move", "f": sorted(k for k in keys if f[k])}
    if slug == "injuries" and f["tl"] is not None:
        item["tl"] = round(f["tl"], 1)
    return item


def load_news():
    try:
        data = json.loads(NEWS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def fetch_news(s, now_utc, stored=None):
    """Daily Faceoff's injury and roster-move news, newest page back to the newest item already stored (NEWS_DAYS on a
    first run). Returns {"injuries": {nhl id: {ts, season, surgery, indefinite, season_ts, indefinite_ts, nf, tl}}, "roster-moves":
    {nhl id: {ts, down, up}}, "recent": {nhl id: [{ts, cat, f, tl}]}, "fetched"} with each player's latest item, pruned to NEWS_KEEP_DAYS. season_ts / indefinite_ts:
    his latest item with that wording, so a later, milder item ("placed on LTIR") doesn't hide it (findings 371). recent:
    every item of the last NEWS_RECENT_DAYS with the Week dress's flags (recent_item; handoffs 406-408).
    Raises if a page can't be read."""
    stored = stored if stored is not None else load_news()
    if stored.get("version") != NEWS_VERSION:
        stored = {}
    out = {"fetched": now_utc.strftime("%Y-%m-%dT%H:%MZ"), "version": NEWS_VERSION}
    recent = {pid: list(v) for pid, v in (stored.get("recent") or {}).items()}
    for slug, keys in (("injuries", ("season", "surgery", "indefinite")), ("roster-moves", ("down", "up"))):
        have = dict(stored.get(slug) or {})
        floor = (now_utc - timedelta(days=NEWS_DAYS)).strftime("%Y-%m-%dT%H:%M")
        newest = max((v["ts"] for v in have.values()), default="")
        stop = max(floor, newest[:16])  # an hour of overlap is enough: items are keyed by player and time
        for page in range(1, NEWS_MAX_PAGES + 1):
            data = page_data(s, f"{DF}/hockey-player-news/{slug}/{page}")["data"]
            items = data.get("data") or []
            for it in items:
                pid, ts = it.get("playerFiveVFiveId"), (it.get("createdAt") or "")[:19]
                if not pid or not ts:
                    continue
                f, old = news_flags(it), have.get(str(int(pid))) or {}
                mine = recent.setdefault(str(int(pid)), [])
                if not any(r["ts"] == ts and r["cat"] == ("inj" if slug == "injuries" else "move") for r in mine):
                    mine.append(recent_item(slug, ts, f))
                new = {"ts": ts, **{k: f[k] for k in keys}} if ts > old.get("ts", "") else dict(old)
                if slug == "injuries" and ts > old.get("ts", ""):  # the Week dress model's inputs (handoff 413)
                    new["nf"] = f["nf"]
                    if f["tl"] is not None:
                        new["tl"] = round(f["tl"], 1)
                if slug == "injuries":
                    for k in ("season", "indefinite"):
                        t = max(old.get(f"{k}_ts", ""), ts if f[k] else "")
                        if t:
                            new[f"{k}_ts"] = t
                have[str(int(pid))] = new
            if not items or page >= (data.get("lastPage") or page) or (items[-1].get("createdAt") or "") < stop:
                break
            time.sleep(0.2)
        keep = (now_utc - timedelta(days=NEWS_KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M")
        out[slug] = {k: v for k, v in sorted(have.items()) if v["ts"] >= keep}
    keep = (now_utc - timedelta(days=NEWS_RECENT_DAYS)).strftime("%Y-%m-%dT%H:%M")
    out["recent"] = {pid: sorted((r for r in v if r["ts"] >= keep), key=lambda r: (r["ts"], r["cat"]))
                     for pid, v in sorted(recent.items()) if any(r["ts"] >= keep for r in v)}
    return out


def save_news(news):
    try:
        NEWS_FILE.write_text(json.dumps(news, indent=0, sort_keys=True) + "\n")
    except OSError as e:
        print(f"Daily Faceoff news not saved: {e!r}", file=sys.stderr)


def fetch_odds(s, today=None, team_map=None):
    """Today's betting lines. The NHL partner feed only rolls to the new day around noon ET, so morning runs fall back to
    ESPN's scoreboard, which posts lines a day ahead (findings 232)."""
    r = s.get("https://api-web.nhle.com/v1/partner-game/US/now", timeout=30)
    r.raise_for_status()
    d = r.json()
    if today is not None and (d.get("currentOddsDate") or "") < today.isoformat():
        try:
            espn = espn_odds(s, today, team_map or {})
            if espn["games"]:
                return espn
        except Exception as e:  # noqa: BLE001 - keep the NHL feed's games when ESPN fails
            print(f"ESPN odds fallback failed: {e!r}", file=sys.stderr)
    return {"odds_date": d.get("currentOddsDate"), "updated": d.get("lastUpdatedUTC"), "games": [
        {"game_id": g.get("gameId"), "start_utc": g.get("startTimeUTC"),
         **{side: {"team": g[f"{side}Team"]["abbrev"], **{o["description"] + (f' {o["qualifier"]}' if o.get("qualifier") and "LINE" not in o["description"] else ""): o["value"]
                                                       for o in g[f"{side}Team"].get("odds", [])},
                   "puck_line": next((o["qualifier"] for o in g[f"{side}Team"].get("odds", []) if o["description"] == "PUCK_LINE"), None)}
            for side in ("home", "away")}}
        for g in d.get("games", [])]}



def espn_odds(s, today, team_map):
    """Today's games and DraftKings moneylines from ESPN's NHL scoreboard, shaped like fetch_odds' games."""
    r = s.get("https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
              params={"dates": today.strftime("%Y%m%d")}, timeout=30)
    r.raise_for_status()
    games = []
    for e in r.json().get("events", []):
        try:
            c = e["competitions"][0]
            ml = (c.get("odds") or [{}])[0].get("moneyline") or {}
            sides = {t["homeAway"]: t["team"]["abbreviation"].upper() for t in c["competitors"]}
            price = {side: float(str(ml[side]["close"]["odds"]).upper().replace("EVEN", "100")) for side in ("home", "away")}
        except (KeyError, IndexError, TypeError, ValueError):
            continue  # no line posted for this game yet
        start = e.get("date", "")
        if len(start) == 17:  # ESPN omits the seconds: 2026-10-12T23:00Z
            start = start[:-1] + ":00Z"
        games.append({"game_id": e.get("id"), "start_utc": start,
                      **{side: {"team": team_map.get(sides[side], sides[side]), "MONEY_LINE_2_WAY": price[side]}
                         for side in ("home", "away")}})
    return {"odds_date": today.isoformat(), "updated": None, "source": "espn", "games": games}

# League moves for the manager simulation (research ideas-batch3 item 71): league data only, never owner or account fields
TX_KEYS = ("id", "type", "status", "teamId", "scoringPeriodId", "proposedDate", "processDate", "bidAmount", "executionType",
           "isPending", "relatedTransactionId", "tradeId")
TX_ITEM_KEYS = ("type", "playerId", "fromTeamId", "toTeamId", "fromLineupSlotId", "toLineupSlotId", "isKeeper")
ROSTER_KEYS = ("playerId", "lineupSlotId", "acquisitionType", "acquisitionDate", "injuryStatus")


def league_log(transactions, teams, scoring_period):
    """Adds, drops, waiver claims, trades and lineup moves, plus every team's roster and slot right now, so each
    manager's roster can be rebuilt over time. Transactions overlap between runs; dedupe by id."""
    return {
        "scoring_period": scoring_period,
        "transactions": [{**{k: t.get(k) for k in TX_KEYS},
                          "items": [{k: i.get(k) for k in TX_ITEM_KEYS} for i in t.get("items") or []]}
                         for t in transactions],
        "rosters": {str(t["id"]): [{k: e.get(k) for k in ROSTER_KEYS} for e in (t.get("roster") or {}).get("entries", [])]
                    for t in teams},
        "moves_used": {str(t["id"]): (t.get("transactionCounter") or {}).get("matchupAcquisitionTotals") for t in teams},
    }


def last_logged(key, now, hours=24):
    """The newest logged copy of `key` ("injuries", "lineups") from a run within `hours` of `now` (naive ET), else None.
    A feed outage then reuses the last good copy instead of rating without it (findings 230)."""
    for path in sorted(LOG_DIR.glob("*.json.gz"), reverse=True):
        try:
            at = datetime.strptime(path.name[:15], "%Y-%m-%d-%H%M")
        except ValueError:
            continue
        if (now - at).total_seconds() > hours * 3600 or at > now:
            continue
        try:
            with gzip.open(path, "rt") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if rec.get(key) and key not in (rec.get("errors") or {}):
            return rec[key]
    return None


def write_log(now, players, starters, errors, s, odds=None, injuries=None, league=None, lineups=None):
    """One gzipped JSON per run in data/log, named by ET date and time."""
    rec = {"run_at": now.isoformat(timespec="minutes"), "starters": starters, "errors": dict(errors)}
    if odds is not None:
        rec["odds"] = odds
    if injuries is not None:
        rec["injuries"] = injuries
    if league is not None:
        rec["league"] = league
    if lineups is not None:
        rec["lineups"] = lineups
    for key, fn in (("lineups", fetch_lineups), ("injuries", fetch_injuries), ("odds", fetch_odds)):
        if key in rec:
            continue
        try:
            rec[key] = fn(s)
        except Exception as e:  # noqa: BLE001 - the log is best-effort
            rec["errors"][key] = repr(e)
    # findings section 25: the rating vs. ESPN's projection, scored against actual points later
    rec["players"] = [{k: p.get(k) for k in ("id", "nhl_id", "name", "team", "pos", "owner", "owned", "injury", "proj_ppg",
                                             "games", "games_left", "cr", "cr_fpg", "cr_dress", "cr_games")}
                      for p in players]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{now.strftime('%Y-%m-%d-%H%M')}.json.gz"
    with gzip.open(path, "wt") as f:
        json.dump(rec, f, separators=(",", ":"))
    print(f"Logged {path.name} ({path.stat().st_size // 1024} KB), errors: {list(rec['errors']) or 'none'}")

