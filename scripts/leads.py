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
from datetime import datetime
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

