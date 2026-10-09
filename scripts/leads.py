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
import time
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
    out = {}
    for t in load_teams(s):
        abbrev = NHL_TEAM[t["name"]]
        try:
            c = page_data(s, f"{DF}/teams/{t['slug']}/line-combinations").get("combinations") or {}
        except Exception as e:  # noqa: BLE001 - one team failing shouldn't lose the rest
            out[abbrev] = {"error": repr(e)}
            continue
        out[abbrev] = {"updated": c.get("updatedAt"), "source": c.get("sourceName"),
                       "players": [[p.get("name"), p.get("groupIdentifier"), p.get("injuryStatus"), p.get("gameTimeDecision")]
                                   for p in c.get("players", [])]}
        time.sleep(0.5)
    return out


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


def fetch_odds(s):
    r = s.get("https://api-web.nhle.com/v1/partner-game/US/now", timeout=30)
    r.raise_for_status()
    d = r.json()
    return {"odds_date": d.get("currentOddsDate"), "updated": d.get("lastUpdatedUTC"), "games": [
        {"game_id": g.get("gameId"), "start_utc": g.get("startTimeUTC"),
         **{side: {"team": g[f"{side}Team"]["abbrev"], **{o["description"] + (f' {o["qualifier"]}' if o.get("qualifier") and "LINE" not in o["description"] else ""): o["value"]
                                                       for o in g[f"{side}Team"].get("odds", [])},
                   "puck_line": next((o["qualifier"] for o in g[f"{side}Team"].get("odds", []) if o["description"] == "PUCK_LINE"), None)}
            for side in ("home", "away")}}
        for g in d.get("games", [])]}


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


def write_log(now, players, starters, errors, s, odds=None, injuries=None, league=None):
    """One gzipped JSON per run in data/log, named by ET date and time."""
    rec = {"run_at": now.isoformat(timespec="minutes"), "starters": starters, "errors": dict(errors)}
    if odds is not None:
        rec["odds"] = odds
    if injuries is not None:
        rec["injuries"] = injuries
    if league is not None:
        rec["league"] = league
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

