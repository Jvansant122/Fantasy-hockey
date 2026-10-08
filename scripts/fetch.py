"""Pull roster, free agents and schedule from ESPN fantasy hockey and write data/players.json.

Env: LEAGUE_ID (required), ESPN_S2 and SWID (private leagues), SEASON (default 2027 = 2026-27),
TEAM_ID (optional; otherwise the team owned by SWID).

Fantasy points come from ESPN's `appliedTotal`, which ESPN computes with the league's own
scoring settings, so no stat-ID mapping is needed here.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl/seasons/{season}"
ET = ZoneInfo("America/New_York")
POSITIONS = {1: "C", 2: "LW", 3: "RW", 4: "D", 5: "G"}
# Lineup slot ids in ESPN hockey: 0 C, 1 LW, 2 RW, 3 F, 4 D, 5 G, 6 UTL, 7 bench, 8 IR
SLOT_NAMES = {0: "C", 1: "LW", 2: "RW", 3: "F", 4: "D", 5: "G"}
LIGHT_NIGHT_MAX_GAMES = 5  # nights with this many NHL games or fewer count as light
FREE_AGENT_LIMIT = 400


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
    matchup_sps = [int(x) for x in league["settings"]["scheduleSettings"]["matchupPeriods"].get(str(period), [today_sp])]

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
    pro = get(s, base, ["proTeamsSchedules_wl"])
    teams = {}
    night_counts = {}
    night_dates = {}
    for pt in pro["settings"]["proTeams"]:
        games = pt.get("proGamesByScoringPeriod") or {}
        teams[pt["id"]] = {"abbrev": pt.get("abbrev", ""), "periods": {int(k) for k in games}}
        for sp, gl in games.items():
            sp = int(sp)
            night_counts.setdefault(sp, set()).update(g.get("id") for g in gl)
            if gl and gl[0].get("date"):
                night_dates[sp] = datetime.fromtimestamp(gl[0]["date"] / 1000, tz=timezone.utc).astimezone(ET)
    # Both teams list each game, so count unique game ids per night
    night_counts = {sp: len(ids) for sp, ids in night_counts.items()}
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
    if my_ids:
        mine_filter = {"players": {"filterIds": {"value": my_ids}, "limit": len(my_ids),
                                   "filterStatsForTopScoringPeriodIds": stat_filter}}
        pool += get(s, league_url, ["kona_player_info"], mine_filter, scoringPeriodId=today_sp)["players"]

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
        if owner == "other":
            continue
        players.append({
            "id": pid,
            "name": p.get("fullName"),
            "team": team["abbrev"].upper(),
            "pos": pos,
            "slots": sorted({SLOT_NAMES[x] for x in p.get("eligibleSlots", []) if x in SLOT_NAMES}),
            "owner": owner,
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

    nights = [{
        "sp": sp,
        "date": night_dates[sp].strftime("%a %-d") if sp in night_dates else f"Day {sp}",
        "games": night_counts.get(sp, 0),
        "light": sp in light,
        "past": sp < today_sp,
    } for sp in matchup_sps]

    out = {
        "updated": datetime.now(ET).strftime("%a %b %-d, %-I:%M %p ET"),
        "team": team_names.get(my_team["id"]) if my_team else None,
        "matchup_period": period,
        "nights": nights,
        "players": sorted(players, key=lambda x: -(x["ppg"] or 0) * x["games"]),
    }
    path = Path(__file__).resolve().parent.parent / "data" / "players.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"Wrote {len(players)} players ({len(my_ids)} mine) for matchup {period} to {path}")
    if not my_team:
        print("Warning: could not find your team. Set SWID or TEAM_ID.", file=sys.stderr)


if __name__ == "__main__":
    main()
