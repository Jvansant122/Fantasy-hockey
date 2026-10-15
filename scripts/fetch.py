"""Pull roster, free agents and schedule from ESPN fantasy hockey and write data/players.json.

Env: LEAGUE_ID (required), ESPN_S2 and SWID (private leagues), SEASON (default from the date: Aug-Dec 2026 is 2027 = 2026-27),
TEAM_ID (optional; otherwise the team owned by SWID).

Fantasy points come from ESPN's `appliedTotal`, which ESPN computes with the league's own
scoring settings, so no stat-ID mapping is needed here.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import leads
import winners as winners_model
from rating import ESPN_TO_NHL, add_ratings

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl/seasons/{season}"
ET = ZoneInfo("America/New_York")
POSITIONS = {1: "C", 2: "LW", 3: "RW", 4: "D", 5: "G"}
# Lineup slot ids in ESPN hockey: 0 C, 1 LW, 2 RW, 3 F, 4 D, 5 G, 6 UTL, 7 bench, 8 IR
SLOT_NAMES = {0: "C", 1: "LW", 2: "RW", 3: "F", 4: "D", 5: "G"}
ESPN_ROLLOVER = timedelta(hours=3, minutes=25)  # ESPN rolls its day at about 3:10-3:40 AM ET (findings 180)
LIGHT_NIGHT_MAX_GAMES = 5  # nights with this many NHL games or fewer count as light
FREE_AGENT_LIMIT = 400
FREE_AGENT_GOALIE_LIMIT = 100
# free agents with the most fantasy points this season, however few own them: a 0%-owned skater dressing on a top power
# play sat past the ownership cut (Judd Caulfield, handoff 455)
FREE_AGENT_SCORING_LIMIT = 200
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


def matchup_days(matchup_periods, period, today_sp, weekday):
    """Scoring periods (days) of the current matchup. scheduleSettings.matchupPeriods maps each matchup id to the
    Monday-Sunday weeks it spans; week 1 is the 6-day opener (days 1-6) and week k >= 2 is days 7k-7 to 7k-1.
    Falls back to this Monday-Sunday week when ESPN's map is missing or doesn't contain today."""
    week = [sp for sp in range(today_sp - weekday, today_sp - weekday + 7) if sp >= 1]
    weeks = (matchup_periods or {}).get(str(period))
    if not weeks or not all(isinstance(w, int) and w >= 1 for w in weeks):
        return week
    days = sorted({sp for w in weeks for sp in week_sps(w)})
    # a stretched week (an NHL break) would break the 7-day arithmetic: then trust this week only
    monday_ok = days[0] == 1 or (today_sp - days[0]) % 7 == weekday
    return days if today_sp in days and monday_ok else week


def next_matchup_days(matchup_periods, period):
    """Days in the matchup after this one: 7 a week from ESPN's map (14 in a two-week playoff round), 7 without it."""
    weeks = (matchup_periods or {}).get(str(period + 1))
    if not weeks or not all(isinstance(w, int) and w >= 1 for w in weeks):
        return 7
    return sum(6 if w == 1 else 7 for w in weeks)


def roster_limit(slot_counts):
    """Players a team can hold outside IR: every lineup and bench slot, not the IR slot '8' (22 here, findings 231).
    None when ESPN sends no slot counts."""
    total = sum(v for k, v in (slot_counts or {}).items() if str(k) != "8" and isinstance(v, (int, float)) and v > 0)
    return int(total) or None


def season_last_day(matchup_periods):
    """Scoring period of the fantasy season's last day: the end of the latest week in any matchup (week 1 is days 1-6,
    week k >= 2 is days 7k-7 to 7k-1). None when ESPN's map is missing."""
    weeks = [w for v in (matchup_periods or {}).values() if isinstance(v, list) for w in v if isinstance(w, int) and w >= 1]
    if not weeks:
        return None
    w = max(weeks)
    return 6 if w == 1 else 7 * w - 1


def is_final_matchup(matchup_periods, period):
    """True in the league's last matchup (the final), when nothing after it counts."""
    ids = [int(k) for k in (matchup_periods or {}) if str(k).isdigit()]
    return bool(ids) and period >= max(ids)


WAIVER_HOURS, WAIVER_RUN_HOUR = 24, 3  # a drop sits on waivers 24 h and clears at ESPN's next ~3 AM ET run (findings 122)


def waiver_first_days(transactions, today):
    """Day each player dropped in these transactions can first play for his new team: ESPN clears waivers at its first
    overnight run (about 3 AM ET) at least 24 hours after the drop. {ESPN player id: date in ET}; the latest drop wins."""
    out = {}
    for t in sorted(transactions or [], key=lambda t: t.get("proposedDate") or 0):
        when = t.get("proposedDate")
        if not when or t.get("status") not in (None, "EXECUTED"):
            continue
        if t.get("type") == "WAIVER":  # a drop made by the overnight waiver run clears at the next night's run (findings 290 (2))
            day = (datetime.fromtimestamp((t.get("processDate") or when) / 1000, ET) + timedelta(days=1)).date()
        else:
            clear = datetime.fromtimestamp(when / 1000, ET) + timedelta(hours=WAIVER_HOURS)
            day = clear.date() if clear.hour < WAIVER_RUN_HOUR else clear.date() + timedelta(days=1)
        for item in t.get("items") or []:
            if item.get("type") == "DROP" and item.get("playerId") is not None:
                out[item["playerId"]] = max(day, today + timedelta(days=1))  # on waivers now, so not tonight
    return out


def first_puck(teams, sp):
    """The day's first game start (UTC ISO) across every team, or None on an off day."""
    return min((t["starts"][sp] for t in teams.values() if sp in t["periods"] and t.get("starts", {}).get(sp)), default=None)


def counts_until(own_start, owner, first_start):
    """When a player's game today stops counting: his own start for a rostered player, but ESPN holds an add made
    after the day's first puck until its overnight rollover, so a free agent's game today ends then (findings 179)."""
    if own_start and first_start and owner in ("freeagent", "waivers"):
        return min(own_start, first_start)
    return own_start


def pending_adds(transactions, rostered, today_sp):
    """Players a team added today whom ESPN's roster doesn't list yet: an add after the day's first puck waits for ESPN's
    overnight rollover (findings 236). {ESPN player id: team id}, from today's executed free-agent and waiver adds,
    unless a later move today dropped him again."""
    last = {}
    for t in sorted(transactions or [], key=lambda t: t.get("proposedDate") or 0):
        if t.get("scoringPeriodId") != today_sp or t.get("status") not in (None, "EXECUTED"):
            continue
        for item in t.get("items") or []:
            pid = item.get("playerId")
            if pid is None:
                continue
            if item.get("type") == "ADD" and t.get("type") in ("FREEAGENT", "WAIVER"):
                last[pid] = item.get("toTeamId") if item.get("toTeamId") not in (None, -1) else t.get("teamId")
            elif item.get("type") == "DROP":
                last[pid] = None
    return {pid: tid for pid, tid in last.items() if tid is not None and pid not in rostered}


def pending_drops(transactions, rostered, today_sp):
    """Players a team dropped today whom ESPN's roster still lists: a drop after the day's first puck leaves him on the
    team until ESPN's overnight rollover (findings 241 (3)). {ESPN player id: team id}, from today's executed free-agent
    and waiver drops, unless a later move today added him again, and only while the roster that lists him is the
    dropping team's."""
    last = {}
    for t in sorted(transactions or [], key=lambda t: t.get("proposedDate") or 0):
        if t.get("scoringPeriodId") != today_sp or t.get("status") not in (None, "EXECUTED") \
                or t.get("type") not in ("FREEAGENT", "WAIVER"):
            continue
        for item in t.get("items") or []:
            pid = item.get("playerId")
            if pid is None:
                continue
            if item.get("type") == "DROP":
                last[pid] = item.get("fromTeamId") if item.get("fromTeamId") not in (None, -1) else t.get("teamId")
            elif item.get("type") == "ADD":
                last[pid] = None
    return {pid: tid for pid, tid in last.items() if tid is not None and pid in rostered and rostered[pid][0] == tid}


def schedule_fields(team, sched, first_sp, today_start, run_utc, last_sp=None):
    """A player's games this matchup from his NHL team's ESPN schedule. sched: {matchup_sps, remaining, light, next_sps,
    today_sp}. first_sp: the first day he can play (waivers); today_start: when today's game stops counting; last_sp: the
    last day he counts for his fantasy team (dropped today, findings 241 (3)), or None."""
    end = last_sp if last_sp is not None else float("inf")
    week_games = [sp for sp in sched["matchup_sps"] if sp in team["periods"] and first_sp <= sp <= end]
    rem_games = [sp for sp in sched["remaining"] if sp in team["periods"] and first_sp <= sp <= end
                 and not (sp == sched["today_sp"] and today_start and today_start <= run_utc)]
    return {"team": team["abbrev"].upper(), "games": len(week_games), "games_left": len(rem_games),
            "light": len([sp for sp in week_games if sp in sched["light"]]),
            "light_left": len([sp for sp in rem_games if sp in sched["light"]]),
            "nights": week_games, "games_next2": len([sp for sp in sched["next_sps"] if sp in team["periods"] and sp <= end]),
            "today_start": today_start}


GAME_HOURS = 4  # a game that started less than this long ago may still be on (handoff 491)


def last_finished_starts(teams, run_utc):
    """{NHL abbrev: UTC ISO start of the team's last finished game}. A game in progress doesn't count: its lineup was
    posted before its puck drop, and the rating's "sat last game" still means the game before it (handoff 491)."""
    cutoff = utc_iso(datetime.fromisoformat(run_utc.replace("Z", "+00:00")) - timedelta(hours=GAME_HOURS))
    out = {}
    for t in teams.values():
        done = [v for v in t.get("starts", {}).values() if v and v <= cutoff]
        if done:
            out[ESPN_TO_NHL.get(t["abbrev"].upper(), t["abbrev"].upper())] = max(done)
    return out


def nights_by_sp(by_date, today_et, today_sp):
    """{date ISO: value} -> {scoring period: value}; one scoring period is one day."""
    out = {}
    for d, v in (by_date or {}).items():
        try:
            out[today_sp + (datetime.fromisoformat(d).date() - today_et).days] = v
        except (TypeError, ValueError):
            continue
    return out


def week_sps(week):
    """Scoring periods of fantasy week k: week 1 is the 6-day opener (days 1-6), week k >= 2 is days 7k-7 to 7k-1."""
    return list(range(1, 7)) if week == 1 else list(range(7 * week - 7, 7 * week))


def season_weeks(night_counts, night_dates, today_sp, today_et, last_sp=None):
    """Every Monday-Sunday week from this one to the season's last, for the page's Nights week picker (findings 300):
    [{week, sps, label 'Oct 19-25', dates ['Mon 19', ...], games {sp: NHL games}, light [sps with 1-5 games]}].
    Only weeks the schedule covers: the fantasy view has the whole season, the scoreboard fallback only its window."""
    if not night_dates:
        return []
    hi = max(night_dates)  # weeks start at this one, which the schedule always covers, so only the end can fall short
    if last_sp is not None:
        hi = min(hi, last_sp)
    out = []
    for week in range((today_sp + 7) // 7, (hi + 7) // 7 + 1):
        sps = week_sps(week)
        if sps[-1] > hi:
            break
        days = [today_et + timedelta(days=sp - today_sp) for sp in sps]
        first, last = days[0], days[-1]
        label = f"{first:%b} {first.day}-{last.day}" if first.month == last.month else f"{first:%b} {first.day}-{last:%b} {last.day}"
        games = {sp: night_counts.get(sp, 0) for sp in sps}
        out.append({"week": week, "sps": sps, "label": label, "dates": [f"{d:%a} {d.day}" for d in days], "games": games,
                    "light": [sp for sp in sps if 0 < games[sp] <= LIGHT_NIGHT_MAX_GAMES]})
    return out


def team_nights(teams, today_sp, weeks):
    """{team abbrev as players carry it: [every scoring period it plays from today in the published weeks]}."""
    shown = {sp for w in weeks for sp in w["sps"] if sp >= today_sp}
    out = {}
    for t in teams.values():
        abbrev = (t.get("abbrev") or "").upper()
        if abbrev:
            out[abbrev] = sorted(sp for sp in t.get("periods", ()) if sp in shown)
    return out


def utc_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "players.json"


PRESEASON_SEED = Path(__file__).resolve().parent.parent / "model" / "espn_preseason.json"
PRESEASON_FILE = DATA_PATH.parent / "espn_preseason.json"


def preseason_projections(players, season, seed=PRESEASON_SEED, path=PRESEASON_FILE):
    """ESPN revises its "preseason" projection in season, folding in his games, but the Season blend (handoff 331) was
    fit on the stored preseason numbers: keep the first projection seen for each player this season (the committed seed,
    then data/espn_preseason.json for players seen later) as proj_ppg_pre (handoff 445)."""
    first = {}
    for f in (seed, path):
        try:
            d = json.loads(Path(f).read_text())
        except (OSError, ValueError):
            continue
        if d.get("season") == season:
            first.update({k: v for k, v in (d.get("proj") or {}).items() if k not in first})
    added = {}
    for p in players:
        k = str(p["id"])
        if p.get("proj_ppg") is not None and k not in first:
            first[k] = added[k] = p["proj_ppg"]
        if k in first:
            p["proj_ppg_pre"] = first[k]
    if added or not Path(path).exists():
        try:
            old = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            old = {}
        keep = (old.get("proj") or {}) if old.get("season") == season else {}
        Path(path).write_text(json.dumps({"season": season, "proj": {**keep, **added}}, separators=(",", ":"), sort_keys=True))


def previous_winners(path=DATA_PATH):
    """The last published NHL games, for the win chances frozen at puck drop."""
    try:
        return (json.loads(path.read_text()).get("winners") or {}).get("games") or []
    except (OSError, ValueError, AttributeError):
        return []


def predicted_winners(winners, s, league_url, period, players, team_names, today_sp, matchup_sps, run_utc, week_start=None):
    """The Predicted winners tab: this week's NHL games (the rating's, each pick frozen at puck drop) and the league's
    fantasy matchups. Either part can fail alone and publishes empty."""
    games = winners_model.freeze(winners.get("games") or [], previous_winners(), week_start)
    matchups = []
    try:  # points so far this matchup, from ESPN's scoreboard view
        sched = get(s, league_url, ["mMatchupScore", "mScoreboard"], scoringPeriodId=today_sp).get("schedule") or []
        now = [m for m in sched if m.get("matchupPeriodId") == period]
        if now:
            print("ESPN matchup score keys:", sorted(now[0].get("home", {}).keys()))
        matchups = winners_model.fantasy_matchups(sched, period, players, team_names, today_sp, matchup_sps, run_utc)
    except Exception as e:  # noqa: BLE001 - never let the tab block the update
        print(f"Fantasy matchups failed, publishing without them: {e!r}", file=sys.stderr)
    print(f"Predicted winners: {len(games)} NHL games ({sum(g.get('p_home') is not None for g in games)} with a pick), "
          f"{len(matchups)} fantasy matchups")
    return {"games": games, "record": winners_model.record(games), "matchups": matchups}


def sp1_date(night_dates):
    """ESPN's scoring period 1 as a date, from the season schedule's game dates (each period is one day): the most common
    date - (period - 1). None from a short window such as the scoreboard fallback (findings 242)."""
    if len(night_dates) < 30:
        return None
    votes = {}
    for sp, d in night_dates.items():
        day = (d.date() if hasattr(d, "date") else d) - timedelta(days=sp - 1)
        votes[day] = votes.get(day, 0) + 1
    return max(votes, key=votes.get).isoformat()


def default_season(day):
    """ESPN's season for a date: August to December belong to the season that ends the next year (2026-27 is 2027)."""
    return day.year + 1 if day.month >= 8 else day.year


def season_over_published(path):
    """True when the published data already says the fantasy season is over (findings 230)."""
    try:
        return bool(json.loads(path.read_text()).get("season_over"))
    except (OSError, ValueError, AttributeError):
        return False


def espn_today(night_dates, today_sp, clock_date):
    """The calendar date of ESPN's current scoring period. ESPN rolls its day at about 3:10-3:40 AM ET, so between
    midnight and then the ET clock is a day ahead of ESPN (findings 180). Read it off the schedule's dated periods."""
    if not night_dates:
        return clock_date
    sp = min(night_dates, key=lambda k: abs(k - today_sp))
    return night_dates[sp].date() - timedelta(days=sp - today_sp)


def pro_schedule(s, base, league_url, matchup_sps, today_sp):
    """Return ({proTeamId: {abbrev, periods, starts}}, {sp: game count}, {sp: date}). starts: {sp: UTC start, ISO}.

    Tries ESPN fantasy's proTeamSchedules_wl view (season root, then the league endpoint),
    and falls back to ESPN's public NHL scoreboard if neither has settings.proTeams.
    """
    for url in (base, league_url):
        try:
            data = get(s, url, ["proTeamSchedules_wl"])  # "proTeams..." returns no proTeams (findings 230)
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
        teams[pt["id"]] = {"abbrev": pt.get("abbrev", ""), "periods": {int(k) for k in games},
                           "starts": {int(k): utc_iso(datetime.fromtimestamp(gl[0]["date"] / 1000, tz=timezone.utc))
                                      for k, gl in games.items() if gl and gl[0].get("date")}}
        for sp, gl in games.items():
            sp = int(sp)
            night_ids.setdefault(sp, set()).update(g.get("id") for g in gl)
            if gl and gl[0].get("date"):
                night_dates[sp] = datetime.fromtimestamp(gl[0]["date"] / 1000, tz=timezone.utc).astimezone(ET)
    # nights with no NHL games (Christmas, the Olympic break) have no date in the view: one period is one day
    if night_dates:
        for sp in matchup_sps:
            if sp not in night_dates:
                k = min(night_dates, key=lambda k: abs(k - sp))
                night_dates[sp] = night_dates[k] + timedelta(days=sp - k)
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
                teams[int(t["id"])] = {"abbrev": t.get("abbreviation", ""), "periods": set(), "starts": {}}
    # today's period is ESPN's day, which lags the ET clock from midnight until its overnight rollover
    today = (datetime.now(ET) - ESPN_ROLLOVER).replace(hour=12, minute=0, second=0, microsecond=0)
    night_counts, night_dates = {}, {}
    for sp in matchup_sps:
        day = today + timedelta(days=sp - today_sp)
        night_dates[sp] = day
        events = None
        for wait in (0, 2, 5):
            time.sleep(wait)
            try:
                events = s.get(f"{site}/scoreboard", params={"dates": day.strftime("%Y%m%d")}, timeout=30).json().get("events", [])
                break
            except Exception as e:
                print(f"Scoreboard for {day:%Y-%m-%d} failed: {e!r}", file=sys.stderr)
        if events is None:  # a missing day would show its games as nights off: fail the run so the recheck retries
            raise RuntimeError(f"ESPN scoreboard for {day:%Y-%m-%d} failed after retries")
        night_counts[sp] = len(events)
        for ev in events:
            for comp in (ev.get("competitions") or [{}])[0].get("competitors", []):
                tid = int(comp["team"]["id"])
                team = teams.setdefault(tid, {"abbrev": comp["team"].get("abbreviation", ""), "periods": set(), "starts": {}})
                team["periods"].add(sp)
                if ev.get("date"):
                    team.setdefault("starts", {})[sp] = utc_iso(datetime.fromisoformat(ev["date"].replace("Z", "+00:00")))
    return teams, night_counts, night_dates


def main():
    league_id = os.environ.get("LEAGUE_ID")
    if not league_id:
        sys.exit("LEAGUE_ID is not set")
    # the season from the date, so next October doesn't quietly publish last season (findings 230)
    season = int(os.environ.get("SEASON") or default_season(datetime.now(ET).date()))
    base = BASE.format(season=season)
    league_url = f"{base}/segments/0/leagues/{league_id}"
    s = session()

    league = get(s, league_url, ["mTeam", "mRoster", "mSettings", "mStatus"])
    status = league["status"]
    period = status.get("currentMatchupPeriod") or 1
    today_sp = league.get("scoringPeriodId") or status.get("latestScoringPeriod") or 1
    # Pro team schedule: games per scoring period (one period = one day), from last week through the next three, so
    # today's date comes from ESPN's scoring period rather than the clock (findings 180)
    window = list(range(max(1, today_sp - 7), today_sp + 22))
    teams, night_counts, night_dates = pro_schedule(s, base, league_url, window, today_sp)
    today_et = espn_today(night_dates, today_sp, datetime.now(ET).date())
    # One scoring period is one day. A matchup is one or more Monday-Sunday weeks (playoff rounds are 2, findings 109).
    weekday = today_et.weekday()
    sched_settings = (league.get("settings") or {}).get("scheduleSettings") or {}
    matchup_sps = matchup_days(sched_settings.get("matchupPeriods"), period, today_sp, weekday)
    final_matchup = is_final_matchup(sched_settings.get("matchupPeriods"), period)
    # after the final's last day ESPN may keep showing the final as a live matchup (findings 232)
    last_sp = season_last_day(sched_settings.get("matchupPeriods"))
    season_over = last_sp is not None and today_sp > last_sp
    season_end = (today_et + timedelta(days=last_sp - today_sp)).strftime("%a %b %-d") if last_sp is not None else None
    # off-season: the first run after the final publishes "season over"; later ones change nothing, so skip them (no commit)
    if season_over and season_over_published(DATA_PATH):
        print(f"ESPN season {season} is over and the published data says so: nothing to update")
        return

    # Which team is mine
    team_id = os.environ.get("TEAM_ID")
    swid = (os.environ.get("SWID") or "").upper()
    my_team = None
    for t in league["teams"]:
        owners = [o.upper() for o in (t.get("owners") or [])] + [str(t.get("primaryOwner", "")).upper()]
        if (team_id and str(t["id"]) == str(team_id)) or (not team_id and swid and swid in owners):
            my_team = t
    team_names = {t["id"]: (t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}").strip() for t in league["teams"]}

    # the two matchups after this one too, for keep value's games in weeks 2-3 (findings section 79)
    next_sps = list(range(matchup_sps[-1] + 1, matchup_sps[-1] + 15))
    missing = [sp for sp in matchup_sps + next_sps if sp not in window]  # a two-week playoff round reaches past the window
    if missing and not night_dates.keys() >= set(missing):
        more = pro_schedule(s, base, league_url, missing, today_sp)
        for tid, t in more[0].items():
            old = teams.setdefault(tid, {"abbrev": t["abbrev"], "periods": set(), "starts": {}})
            old["periods"] |= t["periods"]
            old.setdefault("starts", {}).update(t.get("starts", {}))
        night_counts = {**more[1], **night_counts}
        night_dates = {**more[2], **night_dates}
    # a night with no NHL games (Christmas, the Olympic break) isn't a light night: nobody plays (findings 232)
    light = {sp for sp in matchup_sps if 0 < night_counts.get(sp, 0) <= LIGHT_NIGHT_MAX_GAMES}
    remaining = [sp for sp in matchup_sps if sp >= today_sp]

    # the league's adds, drops, waivers, trades and lineup moves for today and yesterday (research idea 71);
    # the drops also say when a player on waivers can first play (findings 122)
    league_moves, tx, tx_error = None, [], None
    try:
        for sp in sorted({max(1, today_sp - 1), today_sp}):
            tx += get(s, league_url, ["mTransactions2"], {"transactions": {"filterType": {"value": TX_TYPES}}},
                      scoringPeriodId=sp).get("transactions") or []
        league_moves = leads.league_log(tx, league["teams"], today_sp)
        print(f"Logged {len(league_moves['transactions'])} league transactions")
    except Exception as e:  # noqa: BLE001 - the log is best-effort
        tx_error = repr(e)
    run_utc = utc_iso(datetime.now(timezone.utc))
    # ESPN holds an add made after the day's first puck until its overnight rollover (findings 179)
    first_start = first_puck(teams, today_sp)
    first_day = waiver_first_days(tx, today_et)

    # Players: my roster plus free agents and waivers, with season, last-season and projected stat lines
    stat_filter = {"value": 3, "additionalValue": [f"00{season}", f"10{season}", f"00{season - 1}"]}
    rostered = {}
    for t in league["teams"]:
        for e in (t.get("roster") or {}).get("entries", []):
            rostered[e["playerId"]] = (t["id"], e.get("lineupSlotId"))
    # added after the day's first puck: not on ESPN's roster until its rollover, nor in the free-agent pool (findings 236);
    # on the bench, counting from tomorrow
    pending = pending_adds(tx, rostered, today_sp)
    for pid, tid in pending.items():
        rostered[pid] = (tid, 7)
    if pending:
        print(f"{len(pending)} players added after the first puck, not yet on ESPN's rosters")
    # dropped after the first puck: still on ESPN's roster until its rollover, but his team's only through today (findings 241)
    dropped = {}
    try:
        dropped = pending_drops(tx, rostered, today_sp)
        if dropped:
            print(f"{len(dropped)} players dropped after the first puck, still on ESPN's rosters until the rollover")
    except Exception as e:  # noqa: BLE001 - never let this block the update
        print(f"Pending drops failed: {e!r}", file=sys.stderr)
        dropped = {}
    my_ids = [pid for pid, (tid, _) in rostered.items() if my_team and tid == my_team["id"]]

    fa_filter = {"players": {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
        "limit": FREE_AGENT_LIMIT,
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        "filterStatsForTopScoringPeriodIds": stat_filter,
    }}
    pool = get(s, league_url, ["kona_player_info"], fa_filter, scoringPeriodId=today_sp)["players"]
    # every free-agent goalie, however few own him: a team's backup can start (Murray, findings 236)
    goalie_filter = {"players": {**fa_filter["players"], "filterSlotIds": {"value": [5]}, "limit": FREE_AGENT_GOALIE_LIMIT}}
    try:
        have = {(e.get("player") or {}).get("id") for e in pool}
        pool += [e for e in get(s, league_url, ["kona_player_info"], goalie_filter, scoringPeriodId=today_sp)["players"]
                 if (e.get("player") or {}).get("id") not in have]
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"Free-agent goalie lookup failed: {e!r}", file=sys.stderr)
    scoring_filter = {"players": {**fa_filter["players"], "limit": FREE_AGENT_SCORING_LIMIT,
                                  "sortAppliedStatTotal": {"sortAsc": False, "sortPriority": 1, "value": f"00{season}"},
                                  "sortPercOwned": {"sortPriority": 2, "sortAsc": False}}}
    try:
        have = {(e.get("player") or {}).get("id") for e in pool}
        extra = [e for e in get(s, league_url, ["kona_player_info"], scoring_filter, scoringPeriodId=today_sp)["players"]
                 if (e.get("player") or {}).get("id") not in have]
        pool += extra
        print(f"{len(extra)} more free agents by points this season")
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"Free-agent scoring lookup failed: {e!r}", file=sys.stderr)
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

    sched_info = {"matchup_sps": matchup_sps, "remaining": remaining, "light": light, "next_sps": next_sps, "today_sp": today_sp}
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
        team = teams.get(p.get("proTeamId"), {"abbrev": "FA", "periods": set(), "starts": {}})
        owner_id, slot = rostered.get(pid, (None, None))
        if my_team and owner_id == my_team["id"]:
            owner = "mine"
        elif owner_id is not None:
            owner = "other"
        else:
            owner = (entry.get("status") or "FREEAGENT").lower()
        # on waivers: only the games after he clears count; no drop found means tomorrow at the earliest
        clears = first_day.get(pid, today_et + timedelta(days=1)) if owner == "waivers" else None
        first_sp = today_sp + (clears - today_et).days if clears else 0
        # a game today that already started isn't left to play (findings 178)
        # (a waiver player's game the day before he clears was never his, findings 232)
        today_start = team.get("starts", {}).get(today_sp) if today_sp in team["periods"] and today_sp >= first_sp else None
        today_start = counts_until(today_start, owner, first_start)
        if pid in pending and today_start and first_start:  # added after the first puck: counts from tomorrow (findings 236)
            today_start = first_start
        players.append({
            "id": pid,
            "name": p.get("fullName"),
            **schedule_fields(team, sched_info, first_sp, today_start, run_utc, today_sp if pid in dropped else None),
            "pos": pos,
            "slots": sorted({SLOT_NAMES[x] for x in p.get("eligibleSlots", []) if x in SLOT_NAMES}),
            "owner": owner,
            "team_id": owner_id,
            "ir": slot == 8,
            "bench": slot == 7,  # ESPN's bench: a started player there holds no lineup slot (findings 233)
            "injury": p.get("injuryStatus") if p.get("injured") or p.get("injuryStatus") not in (None, "ACTIVE") else None,
            "owned": round((p.get("ownership") or {}).get("percentOwned", 0), 1),
            "gp": gp,
            "ppg": round(ppg, 2) if ppg is not None else None,
            "cur_ppg": cur,
            "last_ppg": last,
            "last_gp": last_gp,  # trade ideas need a full NHL season last year (findings 103)
            "proj_ppg": proj,
            "clears": clears.strftime("%a %-d") if clears else None,
            # schedule_fields' today_start: when today's game stops counting, UTC: his game, or the first puck for a free agent
            **({"pending_add": True} if pid in pending else {}),
            # dropped after the first puck: ESPN lists him until its rollover; the page leaves him out of roster counts
            **({"pending_drop": True} if pid in dropped else {}),
        })

    def reschedule(p, nhl_team):
        """His NHL team's games: ESPN lists him as 'FA' but an NHL roster has him and he has played (findings 230), or the
        NHL moved him and he has played for the new team before ESPN caught up (handoff 501). His Daily Faceoff lineup
        is the new team's."""
        tm = next((t for t in teams.values() if ESPN_TO_NHL.get(t["abbrev"].upper(), t["abbrev"].upper()) == nhl_team), None)
        if tm is None:
            return
        was = p.get("team")
        own = tm.get("starts", {}).get(today_sp) if today_sp in tm["periods"] else None
        p.update(schedule_fields(tm, sched_info, 0, counts_until(own, p.get("owner"), first_start), run_utc))
        if p["pos"] != "G":
            leads.mark_lineups([p], lineups, ESPN_TO_NHL, last_start)
        print(f"{p['name']} is {was!r} on ESPN but plays for {nhl_team}: given its games")

    # Claude Rating: projected points for the rest of the matchup. A failure here keeps the old numbers.
    now = today_et  # ESPN's day, not the clock's, until its overnight rollover (findings 180)
    web = requests.Session()
    web.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    starters, lead_errors = [], {}
    if tx_error:
        lead_errors["league"] = tx_error
    for day in (now, now + timedelta(days=1)):
        if today_sp + (day - now).days in matchup_sps:  # stay inside this matchup (two weeks in a playoff round)
            try:
                starters.append(leads.fetch_starters(web, day))
            except Exception as e:  # noqa: BLE001 - starters are a bonus, never block the update
                lead_errors[f"starters {day}"] = repr(e)
    lineups = None
    try:  # Daily Faceoff lineups: the page's "Tonight" note benches skaters left out (findings section 65)
        lineups = leads.fetch_lineups(web)
    except Exception as e:  # noqa: BLE001
        lead_errors["lineups"] = repr(e)
        lineups = leads.last_logged("lineups", datetime.now(ET).replace(tzinfo=None))  # last good copy (findings 230)
        if lineups is not None:
            lead_errors["lineups"] += " (used the last logged copy)"
    last_start = last_finished_starts(teams, run_utc)
    leads.mark_lineups(players, lineups, ESPN_TO_NHL, last_start)
    injuries = None
    try:  # ESPN injury notes: long absences go to 0 in the weekly rating (findings section 58)
        injuries = leads.fetch_injuries(web)
    except Exception as e:  # noqa: BLE001
        lead_errors["injuries"] = repr(e)
        # without the notes, a goalie out for the season rates as a starter again (findings 230): reuse the last good copy
        injuries = leads.last_logged("injuries", datetime.now(ET).replace(tzinfo=None))
        if injuries is not None:
            lead_errors["injuries"] += " (used the last logged copy)"
    news = leads.load_news()
    try:  # Daily Faceoff injury and roster-move news for the Season rating (findings 360 (1)); the stored copy if it fails
        news = leads.fetch_news(web, datetime.now(timezone.utc), news)
        leads.save_news(news)
    except Exception as e:  # noqa: BLE001
        lead_errors["news"] = repr(e)
    odds = None
    try:
        odds = leads.fetch_odds(web, now, ESPN_TO_NHL)
    except Exception as e:  # noqa: BLE001
        lead_errors["odds"] = repr(e)
    try:  # each player's first-seen ESPN projection, for the rating's blends (handoff 445)
        preseason_projections(players, season)
    except Exception as e:  # noqa: BLE001 - the rating falls back to today's projection
        print(f"Preseason projections failed, using today's: {e!r}", file=sys.stderr)
    winners = {}
    try:
        # every remaining Monday-Sunday week of the matchup (2 in a playoff round's first week)
        weeks_left = -(-(now.weekday() + matchup_sps[-1] - today_sp + 1) // 7)
        add_ratings(players, season, now, now - timedelta(days=now.weekday()), starters, odds, injuries, weeks=max(1, weeks_left),
                    reschedule=reschedule, dfo_news=news, winners=winners)
        for p in players:  # goalie start chances by night, keyed like nights (findings 236)
            if p["pos"] == "G" and "cr_g_nights" in p:
                # only nights he counts for: a waiver goalie's nights before he clears aren't his (handoff 492)
                p["cr_g_nights"] = {sp: q for sp, q in nights_by_sp(p["cr_g_nights"], today_et, today_sp).items()
                                    if sp in p.get("nights", ())}
        rated = True
    except Exception as e:  # noqa: BLE001 - never let the rating break the daily update
        print(f"Claude Rating failed, publishing without it: {e!r}", file=sys.stderr)
        rated = False

    nights = [{
        "sp": sp,
        # one scoring period is one day, so every night's label comes from ESPN's today (findings 232: no "Day 86")
        "date": (today_et + timedelta(days=sp - today_sp)).strftime("%a %-d"),
        "games": night_counts.get(sp, 0),
        "light": sp in light,
        "past": sp < today_sp,
    } for sp in matchup_sps]
    # every later Monday-Sunday week's nights, for the table's Nights week picker (findings 300); optional: a failure here
    # publishes empty lists and the page keeps to this matchup
    weeks, nights_by_team = [], {}
    try:
        weeks = season_weeks(night_counts, night_dates, today_sp, today_et, last_sp)
        nights_by_team = team_nights(teams, today_sp, weeks)
        print(f"Season weeks: {len(weeks)} ({weeks[0]['label'] if weeks else '-'} to {weeks[-1]['label'] if weeks else '-'})")
    except Exception as e:  # noqa: BLE001 - never let the week picker block the update
        print(f"Season weeks failed, publishing without them: {e!r}", file=sys.stderr)
        weeks, nights_by_team = [], {}

    # moves used this matchup and the league's limit, and any per-slot start cap, for the goalie-stream advice (findings section 52)
    settings = league.get("settings") or {}
    stat_limits = (settings.get("rosterSettings") or {}).get("lineupSlotStatLimits") or {}
    goalie_cap = next((v.get("limitValue") for v in stat_limits.values() if isinstance(v, dict) and v.get("limitValue")), None)
    # a team with no moves yet has no entry for this matchup; None only when ESPN sends no counter at all
    moves_used = {t["id"]: (t["transactionCounter"].get("matchupAcquisitionTotals") or {}).get(str(period), 0)
                  if t.get("transactionCounter") else None for t in league["teams"]}
    acq = settings.get("acquisitionSettings") or {}
    move_limit = matchup_move_limit(acq, len(matchup_sps), moves_used.values())
    # adds after Sunday's first puck are charged to the next matchup (findings 290 (1)): its counter and limit
    moves_used_next = {t["id"]: (t["transactionCounter"].get("matchupAcquisitionTotals") or {}).get(str(period + 1), 0)
                       if t.get("transactionCounter") else None for t in league["teams"]}
    move_limit_next = None if final_matchup else matchup_move_limit(
        acq, next_matchup_days(sched_settings.get("matchupPeriods"), period), moves_used_next.values())
    print("ESPN acquisitionSettings:", json.dumps(acq))
    print("ESPN lineupSlotCounts:", json.dumps((settings.get("rosterSettings") or {}).get("lineupSlotCounts")))
    print("ESPN lineupSlotStatLimits:", json.dumps(stat_limits))
    print("ESPN scheduleSettings:", json.dumps(settings.get("scheduleSettings")))
    ir_slots = ((settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}).get("8", 0)
    roster_max = roster_limit((settings.get("rosterSettings") or {}).get("lineupSlotCounts"))
    # settings the site doesn't use yet, logged so research can check them (findings 231)
    trade_settings = settings.get("tradeSettings") or {}
    print("ESPN tradeSettings:", json.dumps(trade_settings))
    print("ESPN positionLimits:", json.dumps((settings.get("rosterSettings") or {}).get("positionLimits")))
    print("ESPN scoringItems:", json.dumps((settings.get("scoringSettings") or {}).get("scoringItems")))
    print("ESPN draftSettings:", json.dumps(settings.get("draftSettings")))
    # ms since 1970; 0 or missing means no deadline
    trade_deadline = trade_settings.get("deadlineDate") if isinstance(trade_settings.get("deadlineDate"), (int, float)) and trade_settings.get("deadlineDate") > 0 else None
    print(f"Move limit: {move_limit}, start cap: {goalie_cap}, roster max: {roster_max}, trade deadline: {trade_deadline}, "
          f"season over: {season_over}, moves used known for "
          f"{sum(v is not None for v in moves_used.values())} teams")

    predicted = predicted_winners(winners, s, league_url, period, players, team_names, today_sp, matchup_sps, run_utc,
                                  (today_et - timedelta(days=today_et.weekday())).isoformat())

    out = {
        "updated": datetime.now(ET).strftime("%a %b %-d, %-I:%M %p ET"),
        "updated_date": today_et.isoformat(),
        "first_start": first_start,  # today's first puck, UTC: adds after it count from tomorrow (findings 179)
        "updated_utc": run_utc,  # the page compares it with each player's today_start (findings 178)
        "team": team_names.get(my_team["id"]) if my_team else None,
        "matchup_period": period,
        "matchup_weeks": -(-len(matchup_sps) // 7),
        "final_matchup": final_matchup,
        "nights": nights,
        "season_weeks": weeks,  # this Monday-Sunday week through the season's last (findings 300)
        "team_nights": nights_by_team,  # {NHL team: scoring periods it plays from today}, for later weeks' Nights
        "rated": rated,
        "goalie_cap": goalie_cap,
        "move_limit": move_limit,
        "move_limit_next": move_limit_next,  # the next matchup's, for adds after Sunday's first puck (findings 290 (1))
        "ir_slots": ir_slots,
        "roster_max": roster_max,  # players allowed outside IR (findings 231)
        "trade_deadline": trade_deadline,  # ESPN's trade deadline, ms since 1970, or None
        "season_over": season_over,  # today is after the fantasy final's last day (findings 232)
        "season_end": season_end,  # the final's last day, e.g. "Sun Apr 4"
        "sp1_date": sp1_date(night_dates),  # ESPN's period 1, for the publish check's day-shift rule (findings 242)
        "teams": [{"id": t["id"], "name": team_names[t["id"]], "mine": bool(my_team and t["id"] == my_team["id"]),
                   "moves_used": moves_used[t["id"]],
                   "moves_used_next": moves_used_next[t["id"]],
                   # ESPN waiver order, 1 = first claim (findings 212: losing claims cost no move)
                   "waiver_rank": t.get("waiverRank")} for t in league["teams"]],
        "winners": predicted,  # the Predicted winners tab (findings 450-451)
        "players": sorted(players, key=lambda x: -x["cr"] if rated else -(x["ppg"] or 0) * x["games"]),
    }
    path = DATA_PATH
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
