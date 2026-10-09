"""Pull roster, free agents and schedule from ESPN fantasy hockey and write data/players.json.

Env: LEAGUE_ID (required), ESPN_S2 and SWID (private leagues), SEASON (default 2027 = 2026-27),
TEAM_ID (optional; otherwise the team owned by SWID).

Fantasy points come from ESPN's `appliedTotal`, which ESPN computes with the league's own
scoring settings, so no stat-ID mapping is needed here.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import leads
from rating import ESPN_TO_NHL, add_ratings

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl/seasons/{season}"
ET = ZoneInfo("America/New_York")
POSITIONS = {1: "C", 2: "LW", 3: "RW", 4: "D", 5: "G"}
# Lineup slot ids in ESPN hockey: 0 C, 1 LW, 2 RW, 3 F, 4 D, 5 G, 6 UTL, 7 bench, 8 IR
SLOT_NAMES = {0: "C", 1: "LW", 2: "RW", 3: "F", 4: "D", 5: "G"}
LIGHT_NIGHT_MAX_GAMES = 5  # nights with this many NHL games or fewer count as light
FREE_AGENT_LIMIT = 400
TX_TYPES = ["FREEAGENT", "WAIVER", "WAIVER_ERROR", "ROSTER", "TRADE_ACCEPT", "TRADE_DECLINE", "TRADE_PROPOSAL",
            "TRADE_UPHOLD", "TRADE_VETO", "FUTURE_ROSTER", "RETRO_ROSTER"]


def session():
    s = requests.Session()
    s.headers["User-Agent"] = "fantasy-hockey-lookup"
    if os.environ.get("ESPN_S2") and os.environ.get("SWID"):
        s.cookies.set("espn_s2", os.environ["ESPN_S2"])
        s.cookies.set("SWID", os.environ["SWID"])
    return s


def get(s, url, views, filt=None, **params):
    headers = {"X-Fantasy-Filter": json.dumps(filt)} if filt else {}
    r = s.get(url, params=[("view", v) for v in views] + list(params.items()), headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def stat_split(player, source, split, season):
    """source 0 = actual, 1 = projected; split 0 = season total."""
    for st in player.get("stats") or []:
        if st.get("statSourceId") == source and st.get("statSplitTypeId") == split and st.get("seasonId") == season:
            return st
    return None


def per_game(st, is_goalie):
    """Fantasy points per game from a season stat line. GP is stat 34 for skaters; goalies use games started (stat 0)."""
    if not st:
        return None, 0
    stats = st.get("stats") or {}
    games = stats.get("0") if is_goalie else stats.get("34")
    games = games or stats.get("34") or 0
    total = st.get("appliedTotal")
    if not games or total is None:
        return None, int(games or 0)
    return round(total / games, 2), int(games)


def matchup_move_limit(acq, periods, used):
    """Adds allowed this matchup. With matchupLimitPerScoringPeriod ESPN's limit is per day, pooled over the matchup:
    1 x 6 days = 6 in the first matchup, 1 x 7 = 7 now, and the transaction log shows 3 adds in one morning, so it
    is not a daily cap. -1, 0 or missing means none, and a limit some team has already passed is wrong."""
    limit = acq.get("matchupAcquisitionLimit")
    if not isinstance(limit, (int, float)) or limit <= 0:
        return None
    limit = round(limit * periods) if acq.get("matchupLimitPerScoringPeriod") else round(limit)
    return None if limit < max([u or 0 for u in used] or [0]) else limit


def pro_schedule(s, base, league_url, matchup_sps, today_sp):
    """Return ({proTeamId: {abbrev, periods}}, {sp: game count}, {sp: date}).

    Tries ESPN fantasy's proTeamsSchedules_wl view (season root, then the league endpoint),
    and falls back to ESPN's public NHL scoreboard if neither has settings.proTeams.
    """
    for url in (base, league_url):
        try:
            data = get(s, url, ["proTeamsSchedules_wl"])
        except requests.RequestException as e:
            print(f"Schedule view failed at {url}: {e}", file=sys.stderr)
            continue
        pro_teams = (data.get("settings") or {}).get("proTeams") if isinstance(data, dict) else None
        if pro_teams:
            break
        print(f"No settings.proTeams at {url}; got keys {list(data)[:10] if isinstance(data, dict) else type(data).__name__}",
              file=sys.stderr)
    else:
        return scoreboard_schedule(s, matchup_sps, today_sp)

    teams, night_ids, night_dates = {}, {}, {}
    for pt in pro_teams:
        games = pt.get("proGamesByScoringPeriod") or {}
        teams[pt["id"]] = {"abbrev": pt.get("abbrev", ""), "periods": {int(k) for k in games}}
        for sp, gl in games.items():
            sp = int(sp)
            night_ids.setdefault(sp, set()).update(g.get("id") for g in gl)
            if gl and gl[0].get("date"):
                night_dates[sp] = datetime.fromtimestamp(gl[0]["date"] / 1000, tz=timezone.utc).astimezone(ET)
    # Both teams list each game, so count unique game ids per night
    return teams, {sp: len(ids) for sp, ids in night_ids.items()}, night_dates


def scoreboard_schedule(s, matchup_sps, today_sp):
    """Build the schedule from ESPN's public NHL scoreboard. Scoring periods are consecutive days,
    so today's period anchors the dates. ESPN team ids match fantasy proTeamIds."""
    print("Using the public NHL scoreboard for the schedule", file=sys.stderr)
    site = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"
    teams = {}
    for sport in s.get(f"{site}/teams", timeout=30).json().get("sports", []):
        for league in sport.get("leagues", []):
            for t in league.get("teams", []):
                t = t.get("team", t)
                teams[int(t["id"])] = {"abbrev": t.get("abbreviation", ""), "periods": set()}
    today = datetime.now(ET).replace(hour=12, minute=0, second=0, microsecond=0)
    night_counts, night_dates = {}, {}
    for sp in matchup_sps:
        day = today + timedelta(days=sp - today_sp)
        night_dates[sp] = day
        events = s.get(f"{site}/scoreboard", params={"dates": day.strftime("%Y%m%d")}, timeout=30).json().get("events", [])
        night_counts[sp] = len(events)
        for ev in events:
            for comp in (ev.get("competitions") or [{}])[0].get("competitors", []):
                tid = int(comp["team"]["id"])
                teams.setdefault(tid, {"abbrev": comp["team"].get("abbreviation", ""), "periods": set()})["periods"].add(sp)
    return teams, night_counts, night_dates


def main():
    league_id = os.environ.get("LEAGUE_ID")
    if not league_id:
        sys.exit("LEAGUE_ID is not set")
    season = int(os.environ.get("SEASON", "2027"))
    base = BASE.format(season=season)
    league_url = f"{base}/segments/0/leagues/{league_id}"
    s = session()

    league = get(s, league_url, ["mTeam", "mRoster", "mSettings", "mStatus"])
    status = league["status"]
    period = status.get("currentMatchupPeriod") or 1
    today_sp = league.get("scoringPeriodId") or status.get("latestScoringPeriod") or 1
    # scheduleSettings.matchupPeriods lists matchup ids, not days, so take this Monday-Sunday week.
    # One scoring period is one day, anchored on today's period.
    weekday = datetime.now(ET).weekday()
    matchup_sps = [sp for sp in range(today_sp - weekday, today_sp - weekday + 7) if sp >= 1]

    # Which team is mine
    team_id = os.environ.get("TEAM_ID")
    swid = (os.environ.get("SWID") or "").upper()
    my_team = None
    for t in league["teams"]:
        owners = [o.upper() for o in (t.get("owners") or [])] + [str(t.get("primaryOwner", "")).upper()]
        if (team_id and str(t["id"]) == str(team_id)) or (not team_id and swid and swid in owners):
            my_team = t
    team_names = {t["id"]: (t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}").strip() for t in league["teams"]}

    # Pro team schedule: games per scoring period (one period = one day)
    teams, night_counts, night_dates = pro_schedule(s, base, league_url, matchup_sps, today_sp)
    light = {sp for sp in matchup_sps if night_counts.get(sp, 0) <= LIGHT_NIGHT_MAX_GAMES}
    remaining = [sp for sp in matchup_sps if sp >= today_sp]

    # Players: my roster plus free agents and waivers, with season, last-season and projected stat lines
    stat_filter = {"value": 3, "additionalValue": [f"00{season}", f"10{season}", f"00{season - 1}"]}
    rostered = {}
    for t in league["teams"]:
        for e in (t.get("roster") or {}).get("entries", []):
            rostered[e["playerId"]] = (t["id"], e.get("lineupSlotId"))
    my_ids = [pid for pid, (tid, _) in rostered.items() if my_team and tid == my_team["id"]]

    fa_filter = {"players": {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
        "limit": FREE_AGENT_LIMIT,
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        "filterStatsForTopScoringPeriodIds": stat_filter,
    }}
    pool = get(s, league_url, ["kona_player_info"], fa_filter, scoringPeriodId=today_sp)["players"]
    # Every team's roster, so the page can show any team. Player cards come in chunks to keep requests small.
    roster_players = {e["playerId"]: e["playerPoolEntry"]["player"] for t in league["teams"]
                      for e in (t.get("roster") or {}).get("entries", []) if (e.get("playerPoolEntry") or {}).get("player")}
    ids = list(rostered)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        card_filter = {"players": {"filterIds": {"value": chunk}, "filterStatsForTopScoringPeriodIds": stat_filter}}
        try:
            pool += get(s, league_url, ["kona_playercard"], card_filter, scoringPeriodId=today_sp)["players"]
        except requests.HTTPError as e:
            # Fall back to the player data that comes with the roster (fewer stat lines)
            print(f"Player card lookup failed ({e}); using roster data for {len(chunk)} players", file=sys.stderr)
            pool += [{"player": roster_players[pid]} for pid in chunk if pid in roster_players]

    players = []
    seen = set()
    for entry in pool:
        p = entry.get("player") or {}
        pid = p.get("id")
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        pos = POSITIONS.get(p.get("defaultPositionId"), "?")
        is_goalie = pos == "G"
        cur, gp = per_game(stat_split(p, 0, 0, season), is_goalie)
        last, last_gp = per_game(stat_split(p, 0, 0, season - 1), is_goalie)
        proj, _ = per_game(stat_split(p, 1, 0, season), is_goalie)
        # Blend: lean on this season once there is a real sample, else projection, else last season
        base_ppg = proj if proj is not None else last
        if cur is not None and base_ppg is not None:
            w = min(gp, 20) / 20 * 0.6
            ppg = cur * w + base_ppg * (1 - w)
        else:
            ppg = cur if base_ppg is None else base_ppg
        team = teams.get(p.get("proTeamId"), {"abbrev": "FA", "periods": set()})
        week_games = [sp for sp in matchup_sps if sp in team["periods"]]
        rem_games = [sp for sp in remaining if sp in team["periods"]]
        owner_id, slot = rostered.get(pid, (None, None))
        if my_team and owner_id == my_team["id"]:
            owner = "mine"
        elif owner_id is not None:
            owner = "other"
        else:
            owner = (entry.get("status") or "FREEAGENT").lower()
        players.append({
            "id": pid,
            "name": p.get("fullName"),
            "team": team["abbrev"].upper(),
            "pos": pos,
            "slots": sorted({SLOT_NAMES[x] for x in p.get("eligibleSlots", []) if x in SLOT_NAMES}),
            "owner": owner,
            "team_id": owner_id,
            "ir": slot == 8,
            "injury": p.get("injuryStatus") if p.get("injured") or p.get("injuryStatus") not in (None, "ACTIVE") else None,
            "owned": round((p.get("ownership") or {}).get("percentOwned", 0), 1),
            "gp": gp,
            "ppg": round(ppg, 2) if ppg is not None else None,
            "cur_ppg": cur,
            "last_ppg": last,
            "proj_ppg": proj,
            "games": len(week_games),
            "games_left": len(rem_games),
            "light": len([sp for sp in week_games if sp in light]),
            "light_left": len([sp for sp in rem_games if sp in light]),
            "nights": week_games,
        })

    # Claude Rating: projected points for the rest of the matchup. A failure here keeps the old numbers.
    now = datetime.now(ET).date()
    web = requests.Session()
    web.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    starters, lead_errors = [], {}
    for day in (now, now + timedelta(days=1)):
        if day.weekday() >= now.weekday():  # stay inside this Monday-Sunday matchup
            try:
                starters.append(leads.fetch_starters(web, day))
            except Exception as e:  # noqa: BLE001 - starters are a bonus, never block the update
                lead_errors[f"starters {day}"] = repr(e)
    league_moves = None
    try:  # the league's adds, drops, waivers, trades and lineup moves for today and yesterday (research idea 71)
        tx = []
        for sp in sorted({max(1, today_sp - 1), today_sp}):
            tx += get(s, league_url, ["mTransactions2"], {"transactions": {"filterType": {"value": TX_TYPES}}},
                      scoringPeriodId=sp).get("transactions") or []
        league_moves = leads.league_log(tx, league["teams"], today_sp)
        print(f"Logged {len(league_moves['transactions'])} league transactions")
    except Exception as e:  # noqa: BLE001 - the log is best-effort
        lead_errors["league"] = repr(e)
    lineups = None
    try:  # Daily Faceoff lineups: the page's "Tonight" note benches skaters left out (findings section 65)
        lineups = leads.fetch_lineups(web)
    except Exception as e:  # noqa: BLE001
        lead_errors["lineups"] = repr(e)
    leads.mark_lineups(players, lineups, ESPN_TO_NHL)
    injuries = None
    try:  # ESPN injury notes: long absences go to 0 in the weekly rating (findings section 58)
        injuries = leads.fetch_injuries(web)
    except Exception as e:  # noqa: BLE001
        lead_errors["injuries"] = repr(e)
    odds = None
    try:
        odds = leads.fetch_odds(web)
    except Exception as e:  # noqa: BLE001
        lead_errors["odds"] = repr(e)
    try:
        add_ratings(players, season, now, now - timedelta(days=now.weekday()), starters, odds, injuries)
        rated = True
    except Exception as e:  # noqa: BLE001 - never let the rating break the daily update
        print(f"Claude Rating failed, publishing without it: {e!r}", file=sys.stderr)
        rated = False

    nights = [{
        "sp": sp,
        "date": night_dates[sp].strftime("%a %-d") if sp in night_dates else f"Day {sp}",
        "games": night_counts.get(sp, 0),
        "light": sp in light,
        "past": sp < today_sp,
    } for sp in matchup_sps]

    # moves used this matchup and the league's limit, and any per-slot start cap, for the goalie-stream advice (findings section 52)
    settings = league.get("settings") or {}
    stat_limits = (settings.get("rosterSettings") or {}).get("lineupSlotStatLimits") or {}
    goalie_cap = next((v.get("limitValue") for v in stat_limits.values() if isinstance(v, dict) and v.get("limitValue")), None)
    # a team with no moves yet has no entry for this matchup; None only when ESPN sends no counter at all
    moves_used = {t["id"]: (t["transactionCounter"].get("matchupAcquisitionTotals") or {}).get(str(period), 0)
                  if t.get("transactionCounter") else None for t in league["teams"]}
    acq = settings.get("acquisitionSettings") or {}
    move_limit = matchup_move_limit(acq, len(matchup_sps), moves_used.values())
    print("ESPN acquisitionSettings:", json.dumps(acq))
    print("ESPN lineupSlotCounts:", json.dumps((settings.get("rosterSettings") or {}).get("lineupSlotCounts")))
    print("ESPN lineupSlotStatLimits:", json.dumps(stat_limits))
    print("ESPN scheduleSettings:", json.dumps(settings.get("scheduleSettings")))
    ir_slots = ((settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}).get("8", 0)
    print(f"Move limit: {move_limit}, start cap: {goalie_cap}, moves used known for "
          f"{sum(v is not None for v in moves_used.values())} teams")

    out = {
        "updated": datetime.now(ET).strftime("%a %b %-d, %-I:%M %p ET"),
        "team": team_names.get(my_team["id"]) if my_team else None,
        "matchup_period": period,
        "nights": nights,
        "rated": rated,
        "goalie_cap": goalie_cap,
        "move_limit": move_limit,
        "ir_slots": ir_slots,
        "teams": [{"id": t["id"], "name": team_names[t["id"]], "mine": bool(my_team and t["id"] == my_team["id"]),
                   "moves_used": moves_used[t["id"]]} for t in league["teams"]],
        "players": sorted(players, key=lambda x: -x["cr"] if rated else -(x["ppg"] or 0) * x["games"]),
    }
    path = Path(__file__).resolve().parent.parent / "data" / "players.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"Wrote {len(players)} players ({len(my_ids)} mine) for matchup {period} to {path}")
    try:
        leads.write_log(datetime.now(ET), players, starters, lead_errors, web, odds, injuries, league_moves, lineups)
    except Exception as e:  # noqa: BLE001
        print(f"Lead log failed: {e!r}", file=sys.stderr)
    if not my_team:
        print("Warning: could not find your team. Set SWID or TEAM_ID.", file=sys.stderr)


if __name__ == "__main__":
    main()
