"""Check data/players.json before the update workflow publishes it.

    python scripts/check_data.py                  # exit 1 if the page couldn't render this data or it breaks a rule below
    python scripts/check_data.py --against-head   # ... also compared with the published copy (git HEAD), warnings included
    python scripts/check_data.py --require-rated  # also exit 1 if the Claude Rating is missing

Standard library only, like the other daily scripts. tests/test_site_contract.py runs the same checks."""
import json
import statistics
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP_FIELDS = {"updated", "nights", "rated", "teams", "players", "goalie_cap", "move_limit", "move_limit_next", "ir_slots", "matchup_weeks", "final_matchup", "updated_date", "updated_utc", "first_start",
              "season_weeks", "team_nights"}  # the last two feed the table's Nights week picker (findings 300); empty is allowed
NIGHT_FIELDS = {"sp", "date", "games", "light", "past"}
PLAYER_FIELDS = {"id", "name", "team", "pos", "slots", "owner", "team_id", "ir", "injury", "owned", "ppg", "cur_ppg", "last_ppg",
                 "games", "games_left", "light", "light_left", "nights", "dfo_out", "last_gp", "clears", "today_start"}
RATED_FIELDS = {"cr", "cr_fpg", "cr_games", "cr_dress", "cr_dress_next", "cr_matched", "cr_why", "cr_pct", "cr_season", "cr_season_pct", "sat_last"}
RATED_GOALIE_FIELDS = {"cr_g_nights"}  # goalies only: start chance by night, for the open G slots (findings 236)
MIN_PLAYERS = 100  # rosters alone are ~200 players; far fewer means ESPN sent a partial answer
MIN_FREE_AGENTS, MIN_INJURED = 100, 10
EPS = 0.02  # rounding slack: values are rounded to 2 decimals
SEASON_GAMES = 84  # NHL games a team plays
# ESPN scoring period 1 of the 2027 season (sp 7 = Mon Oct 5, 2026): the day-0 the nights' sp count from, used only when
# the data has no sp1_date (fetch.py publishes ESPN's own); the check is skipped once the data is 300+ days past it
SP1 = date(2026, 9, 29)


def problems(data):
    """Reasons the page would break or mislead with this data; empty when it's fine."""
    out = []
    missing = TOP_FIELDS - set(data)
    if missing:
        return [f"missing top-level fields: {sorted(missing)}"]
    # 7 a week, 14 in a two-week playoff round, 6 in the short opening matchup (findings 230)
    want = {7 * (data.get("matchup_weeks") or 1)} | ({6} if data.get("matchup_period") == 1 else set())
    if len(data["nights"]) not in want:
        out.append(f"{len(data['nights'])} nights, expected {sorted(want)}")
    # the page counts next week's moves after Sunday's first puck (handoff 263)
    if any("moves_used_next" not in t for t in data["teams"]):
        out.append("teams missing moves_used_next")
    for n in data["nights"]:
        if NIGHT_FIELDS - set(n):
            out.append(f"night {n.get('date')} missing {sorted(NIGHT_FIELDS - set(n))}")
    # nights run on consecutive days and end on a Sunday, and today isn't already marked past (findings 180)
    labels = [n.get("date", "") for n in data["nights"]]
    if not labels:
        out.append("no nights")
        return out
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if all(l[:3] in days for l in labels) and [l[:3] for l in labels] != (days * 3)[days.index(labels[0][:3]):][:len(labels)] \
            or labels and labels[-1][:3] != "Sun":
        out.append(f"nights don't run on consecutive days to a Sunday: {labels}")
    nums = [int(l.split()[-1]) for l in labels if l.split()[-1].isdigit()]
    if len(nums) == len(labels) and any(b != a + 1 and b != 1 for a, b in zip(nums, nums[1:])):
        out.append(f"night dates aren't consecutive: {labels}")
    today = data.get("updated_date")
    label = date.fromisoformat(today).strftime("%a ") + str(date.fromisoformat(today).day) if today else None
    if label in labels:
        k = labels.index(label)  # today's night: not past, and every earlier night past
        if [bool(n.get("past")) for n in data["nights"]] != [i < k for i in range(len(labels))]:
            out.append(f"past nights don't match today ({today}): {[(n.get('date'), n.get('past')) for n in data['nights']]}")
    players = data["players"]
    if len(players) < MIN_PLAYERS:
        out.append(f"only {len(players)} players")
    # a run that lost the free-agent list or the injury feed passes every other rule (findings 230): runs have ~400 and ~60
    if len([p for p in players if p.get("owner") in ("freeagent", "waivers")]) < MIN_FREE_AGENTS:
        out.append(f"only {len([p for p in players if p.get('owner') in ('freeagent', 'waivers')])} free agents")
    if len([p for p in players if p.get("injury")]) < MIN_INJURED:
        out.append(f"only {len([p for p in players if p.get('injury')])} players with an injury status: injury feed missing?")
    need = PLAYER_FIELDS | (RATED_FIELDS if data["rated"] else set())
    need_g = need | (RATED_GOALIE_FIELDS if data["rated"] else set())
    bad = [(p.get("name"), sorted((need_g if p.get("pos") == "G" else need) - set(p))) for p in players
           if (need_g if p.get("pos") == "G" else need) - set(p)]
    if bad:
        out.append(f"{len(bad)} players missing fields, e.g. {bad[0]}")
    # with no game left this week (Sunday night, the Olympic break) every Week rating is 0 by right (findings 232)
    if data["rated"] and any(p.get("games_left") for p in players) and all(not p.get("cr") for p in players):
        out.append("rated is true but every Claude Rating is 0")
    out += winners_problems(data.get("winners"))
    if not out:
        out += rules(data)
    return out


GAME_FIELDS = {"date", "home", "away", "p_home", "started", "inputs"}
SIDE_FIELDS = {"id", "name", "now", "rest", "total"}


def winners_problems(w):
    """The Predicted winners tab (findings 450-451): optional (data from before it shipped has none), but when present every
    game and matchup has its fields, win chances are probabilities, and no game still to come is missing its pick."""
    if w is None:
        return []
    out = []
    games, ms = w.get("games"), w.get("matchups")
    if not isinstance(games, list) or not isinstance(ms, list) or not isinstance(w.get("record"), dict):
        return ["winners missing games, matchups or record"]
    bad = [g for g in games if GAME_FIELDS - set(g)]
    if bad:
        out.append(f"{len(bad)} predicted games missing fields, e.g. {sorted(GAME_FIELDS - set(bad[0]))}")
    elif [g for g in games if g["p_home"] is not None and not 0 < g["p_home"] < 1]:
        out.append("a predicted game's win chance is outside 0-1")
    elif games and all(g["p_home"] is None for g in games if not g["started"]) and any(not g["started"] for g in games):
        out.append("no NHL game still to come has a predicted winner")
    if [m for m in ms if not (isinstance(m.get("home"), dict) and isinstance(m.get("away"), dict)
                              and SIDE_FIELDS <= set(m["home"]) and SIDE_FIELDS <= set(m["away"]) and 0 <= m.get("p_home", -1) <= 1)]:
        out.append("a fantasy matchup is missing fields or its win chance is outside 0-1")
    return out


def _night_dates(data):
    """{sp: date} from the 'Mon 5' labels: the nearest date to updated_date (within 15 days) with that day of the month, so
    a 14-night playoff round's 'Sun 21' on Mar 8 is Mar 21, not Feb 21 (findings 241)."""
    t = date.fromisoformat(data["updated_date"])
    out = {}
    for n in data["nights"]:
        dom = n["date"].split()[-1]
        if not dom.isdigit():
            return {}
        out[n["sp"]] = next((t + timedelta(days=k) for k in sorted(range(-15, 16), key=abs) if (t + timedelta(days=k)).day == int(dom)), None)
    return out


def rules(data):
    """Rules every published version keeps (findings 215: across all 61 versions to Oct 11 these catch the overnight
    shift, a made-up game, starts for a goalie with no games and the first-puck bugs, with no false alarms)."""
    out, nights, players, rated = [], data["nights"], data["players"], data["rated"]
    sps = [n["sp"] for n in nights]
    ahead = [n["sp"] for n in nights if not n["past"]]
    today_sp = ahead[0] if ahead else None
    # N1 scoring periods consecutive, each on its real date (the overnight shift, findings 180)
    if sps != list(range(sps[0], sps[0] + len(sps))):
        out.append(f"N1 night scoring periods not consecutive: {sps}")
    nd = _night_dates(data)
    # ESPN's period 1 from the data (next season too), else this season's (findings 242)
    try:
        sp1 = date.fromisoformat(data["sp1_date"]) if data.get("sp1_date") else SP1
    except (TypeError, ValueError):
        sp1 = SP1
    if nd and 0 <= (min(d for d in nd.values() if d) - sp1).days <= 300:
        bad = [(sp, str(d)) for sp, d in nd.items() if d and (d - sp1).days + 1 != sp]
        if bad:
            out.append(f"N1 scoring period doesn't match its date (ESPN day not rolled over?): {bad[:3]}")
    # N2 no more teams playing a night than 2 per game
    team_nights = {}
    for p in players:
        team_nights.setdefault(p["team"], set()).update(p.get("nights") or [])
    for n in nights:
        teams = sum(n["sp"] in v for k, v in team_nights.items() if k != "FA")
        if teams > 2 * n["games"]:
            out.append(f"N2 {n['date']}: {teams} teams play but {n['games']} games")
    bad = {k: [] for k in ("P2", "P3", "P4", "P5", "P6", "G1", "S1", "S2", "P7", "G4")}
    for p in players:
        nm = f"{p['name']} ({p['team']})"
        pn = p.get("nights") or []
        # P2 games left = his nights from today, less tonight once his game has started (findings 178); waiver players at most that
        left = sum(sp in ahead for sp in pn)
        ts = p.get("today_start")
        want = left - (1 if today_sp in pn and ts and data.get("updated_utc") and ts <= data["updated_utc"] else 0)
        if p.get("owner") == "waivers" and p["games_left"] > left or p.get("owner") != "waivers" and p["games_left"] != want:
            bad["P2"].append(f"{nm} games_left {p['games_left']}, want {want}")
        if not rated:
            continue
        cr, fpg, g = p.get("cr") or 0, p.get("cr_fpg") or 0, p.get("cr_games") or 0
        # P3 expected games between 0 and games left (a made-up game, findings 182)
        if g > p["games_left"] + EPS or g < 0:
            bad["P3"].append(f"{nm} cr_games {g} with {p['games_left']} left")
        # P4 Week = points a game x expected games
        if abs(cr - fpg * g) > 0.006 + 0.005 * (g + abs(fpg)):
            bad["P4"].append(f"{nm} cr {cr} != {fpg} x {g}")
        # P5 a skater's Season (per team game) never above his points a game: the Season rating's own points a game when
        # it has one (age, ESPN's projection and the boosted model move it off the Week's, findings 360), else the Week's
        top = (p.get("cr_why") or {}).get("season_pts", fpg) if p["pos"] != "G" else fpg
        if p["pos"] != "G" and p.get("cr_season") is not None and p["cr_season"] > top + EPS:
            bad["P5"].append(f"{nm} cr_season {p['cr_season']} > {'season_pts' if top is not fpg else 'cr_fpg'} {top}")
        # S1 a Season rating is never negative, and a skater's share (Season / its own points a game) is at most 1 (handoff 444)
        if p.get("cr_season") is not None and p["cr_season"] < -EPS:
            bad["S1"].append(f"{nm} cr_season {p['cr_season']}")
        # S2 Proj GP between his games so far and 84 (handoff 443-444)
        if p.get("proj_gp") is not None and not (_gp(p) - 1 <= p["proj_gp"] <= SEASON_GAMES + 1 if p["pos"] != "G" else 0 <= p["proj_gp"] <= SEASON_GAMES + 1):
            bad["S2"].append(f"{nm} proj_gp {p['proj_gp']} with {_gp(p)} GP")
        # P6 dress chances are probabilities
        for k in ("cr_dress", "cr_dress_next"):
            if p.get(k) is not None and not 0 <= p[k] <= 1:
                bad["P6"].append(f"{nm} {k} {p[k]}")
        # P7 a name with no NHL match and no ESPN record (no games this season or last, no projection) isn't rated:
        # a retired or minor-league player ESPN still lists on a team (handoff 453)
        if not p.get("cr_matched") and not p.get("nhl_id") and (cr > 0 or (p.get("cr_season") or 0) > 0) \
                and not (p.get("cur_ppg") or p.get("last_ppg") or p.get("proj_ppg") or p.get("gp") or p.get("last_gp")):
            bad["P7"].append(f"{nm} unmatched, no record, cr {cr} Season {p.get('cr_season')}")
        # G4 a healthy, matched goalie the Week expects to start keeps a Season rating above 0 (handoff 446: a save% term
        # clipped a backup's share to 0)
        why = p.get("cr_why") or {}
        if p["pos"] == "G" and p["team"] != "FA" and not p.get("injury") and p.get("cr_matched") \
                and (why.get("exp_starts") or 0) >= 0.05 and p.get("cr_season") is not None and not p["cr_season"] > 0:
            bad["G4"].append(f"{nm} exp_starts {why.get('exp_starts')} but Season {p['cr_season']}")
        # G1 a goalie's expected starts never above his games left or his team's games
        es = why.get("exp_starts") if p["pos"] == "G" else None
        if es is not None and (es > p["games_left"] + EPS or why.get("team_games", p["games_left"]) < es - EPS):
            bad["G1"].append(f"{nm} exp_starts {es} of {why.get('team_games')} team games, {p['games_left']} left")
    out += [f"{k} {len(v)} players, e.g. {v[:3]}" for k, v in bad.items() if v]
    out += week_rules(data, ahead)
    if rated:
        # G2 a team's goalies' expected starts sum to at most its games left
        by_team = {}
        for p in players:
            es = (p.get("cr_why") or {}).get("exp_starts") if p["pos"] == "G" else None
            if es is not None:
                t = by_team.setdefault(p["team"], [0.0, 0])
                t[0], t[1] = t[0] + es, max(t[1], p["games_left"])
        over = [(t, round(v, 2), gl) for t, (v, gl) in by_team.items() if v > gl + 0.05]
        if over:
            out.append(f"G2 goalie starts above team games: {over[:4]}")
        # G3 every NHL team with a healthy, matched goalie in the data has a goalie with a Season rating: a team with no
        # game this week, or a roster that couldn't be read, took its goalies to 0 (findings 230). A team whose goalies
        # here are all hurt or unmatched can fairly have none.
        teams = {}
        for p in players:
            if p["pos"] == "G" and p["team"] != "FA":
                t = teams.setdefault(p["team"], [False, False])
                t[0] |= bool(p.get("cr_matched")) and not p.get("injury")
                t[1] |= (p.get("cr_season") or 0) > 0
        zero = sorted(t for t, (healthy, rated_) in teams.items() if healthy and not rated_)
        if zero:
            out.append(f"G3 teams whose goalies all have Season 0: {zero[:6]}")
    return out


def week_rules(data, ahead):
    """S1 each published week is a Monday-Sunday fantasy week (week 1 periods 1-6, week k 7k-7 to 7k-1), in order, its
    games, light nights and dates inside it; S2 each team plays at most once a night (so at most 7 a week), from today,
    inside the published weeks (findings 300)."""
    out, weeks, tn = [], data.get("season_weeks") or [], data.get("team_nights") or {}
    for i, w in enumerate(weeks):
        k, sps = w.get("week"), w.get("sps") or []
        want = (list(range(1, 7)) if k == 1 else list(range(7 * k - 7, 7 * k))) if isinstance(k, int) else None
        if sps != want or (i and k != (weeks[i - 1].get("week") or 0) + 1):
            out.append(f"S1 week {k} has periods {sps[:2]}...{sps[-1:]} or is out of order")
        elif not {int(x) for x in (w.get("games") or {})} <= set(sps) or not set(w.get("light") or []) <= set(sps) \
                or len(w.get("dates") or sps) != len(sps):
            out.append(f"S1 week {k} lists games, light nights or dates outside its periods")
    allowed = {sp for w in weeks for sp in w.get("sps") or []}
    today_sp = ahead[0] if ahead else None
    for team, sps in tn.items():
        if len(set(sps)) != len(sps) or sps != sorted(sps) or (today_sp and any(sp < today_sp for sp in sps)) \
                or any(sp not in allowed for sp in sps):
            out.append(f"S2 {team}'s nights aren't unique, in order, from today and inside the published weeks: {sps[:8]}")
    return out


def _gp(p):
    return (p.get("cr_why") or {}).get("gp") or 0


def problems_vs_prev(data, prev):
    """Rules between this version and the published one."""
    out = []
    if not (data.get("rated") and prev and prev.get("rated")):
        return out
    # X1 a scoring period keeps its date within a matchup (findings 180)
    if data.get("matchup_period") == prev.get("matchup_period"):
        was = {n["sp"]: n["date"] for n in prev["nights"]}
        moved = [(n["sp"], was[n["sp"]], n["date"]) for n in data["nights"] if n["sp"] in was and was[n["sp"]] != n["date"]]
        if moved:
            out.append(f"X1 scoring periods moved date since the last version: {moved[:3]}")
    # X2 games played never go back (an overnight run dropping last night's games, findings 183)
    old = {p["id"]: p for p in prev["players"]}
    same = [(p, old[p["id"]]) for p in data["players"] if p["id"] in old and p["pos"] != "G"]
    now, was = sum(_gp(p) for p, _ in same), sum(_gp(o) for _, o in same)
    if now < was - 20:
        out.append(f"X2 skater games played fell {was} -> {now}: last night's games missing")
    return out


def warnings(data, prev):
    """Signs of a model change gone wrong: printed, never blocking (findings 215)."""
    out = []
    if not data.get("rated"):
        return out
    players = data["players"]
    # W5 the Predicted winners tab came out empty (each part fails alone and publishes empty, findings 450)
    w = data.get("winners") or {}
    if not w.get("games"):
        out.append("W5 no predicted NHL games this run (see 'Predicted winners' in the fetch log)")
    if not w.get("matchups"):
        out.append("W5 no fantasy matchups this run (see 'Fantasy matchups' in the fetch log)")
    # W1 ESPN's goalie games below his NHL starts: This yr/G inflated (findings 182)
    lag = [p["name"] for p in players if p["pos"] == "G" and (p.get("cr_why") or {}).get("gs") and p.get("gp") is not None
           and p["gp"] < p["cr_why"]["gs"]]
    if lag:
        out.append(f"W1 {len(lag)} goalies' ESPN games below NHL starts: {lag[:4]}")
    # W2 the same schedule twice (findings 300): a team's nights this matchup in team_nights match its players' nights
    tn, ahead = data.get("team_nights") or {}, {n["sp"] for n in data["nights"] if not n["past"]}
    mismatch = sorted({p["team"] for p in players if p["team"] in tn and p.get("owner") != "waivers" and not p.get("pending_drop")
                       and {sp for sp in p.get("nights") or [] if sp in ahead} != {sp for sp in tn[p["team"]] if sp in ahead}})
    if mismatch:
        out.append(f"W2 teams whose nights differ from their players' nights this matchup: {mismatch[:6]}")
    # G5 each NHL team's goalies' start chances on each night still to come sum to about 1: a confirmed starter matched
    # to nobody zeroes the whole team's night (handoff 454); a double count goes over. 170 versions to Oct 15: 0.69-1.00
    g_night = {}
    for p in players:
        if p["pos"] == "G" and p["team"] != "FA":
            for sp, q in (p.get("cr_g_nights") or {}).items():
                if int(sp) in ahead:
                    g_night[(p["team"], sp)] = g_night.get((p["team"], sp), 0.0) + q
    off = sorted((t, sp, round(v, 2)) for (t, sp), v in g_night.items() if not 0.6 <= v <= 1.02)
    if off:
        out.append(f"G5 team nights whose goalie start chances don't sum to about 1: {off[:5]}")
    # S3 a team's summed skater Season shares far above its lineup spots (12 F, 6 D; handoff 444)
    sums = {}
    for p in players:
        sp = (p.get("cr_why") or {}).get("season_pts")
        if p["pos"] != "G" and p["team"] != "FA" and sp and p.get("cr_season") is not None:
            k = (p["team"], p["pos"] == "D")
            sums[k] = sums.get(k, 0.0) + p["cr_season"] / sp
    high = sorted((t, "D" if d else "F", round(v, 1)) for (t, d), v in sums.items() if v > (7 if d else 13))
    if high:
        out.append(f"S3 team Season shares above the lineup: {high[:5]}")
    # S4 Daily Faceoff says season-ending, yet the share stays above 0.3
    big = [p["name"] for p in players if "season-ending" in ((p.get("cr_why") or {}).get("season_news") or ())
           and (p.get("cr_why") or {}).get("season_pts") and p.get("cr_season", 0) / p["cr_why"]["season_pts"] > 0.3]
    if big:
        out.append(f"S4 {len(big)} skaters with season-ending news keep a Season share above 30%: {big[:5]}")
    if not (prev and prev.get("rated")):
        return out
    old = {p["id"]: p for p in prev["players"]}
    same = [(p, old[p["id"]]) for p in players if p["id"] in old and p["pos"] != "G"]
    # X3 skaters with no new game shouldn't move much: PR #86 moved them median 0.030, p90 0.090; 25 other model PRs stayed under
    quiet = [(p, o) for p, o in same if p.get("cr_matched") and o.get("cr_fpg") is not None and p.get("cr_fpg") is not None
             and _gp(p) == _gp(o)]
    if len(quiet) >= 50:
        d = sorted(abs(p["cr_fpg"] - o["cr_fpg"]) for p, o in quiet)
        med, p90 = statistics.median(d), d[int(0.9 * len(d))]
        if med > 0.02 or p90 > 0.05:
            out.append(f"X3 {len(quiet)} skaters without a new game moved: median |change in pts/game| {med:.3f}, 90th percentile {p90:.3f}")
        zero = [abs(p["cr_fpg"] - o["cr_fpg"]) for p, o in quiet if not _gp(p)]
        if len(zero) >= 10 and statistics.median(zero) > 0.02:
            out.append(f"X3b {len(zero)} skaters with no NHL games moved: median {statistics.median(zero):.3f}")
    # X3s the same for the Season: skaters with no new game since the last version (handoff 444)
    quiet_s = [abs(p["cr_season"] - o["cr_season"]) for p, o in same if p.get("cr_matched") and _gp(p) == _gp(o)
               and p.get("cr_season") is not None and o.get("cr_season") is not None]
    if len(quiet_s) >= 50:
        d = sorted(quiet_s)
        med, p90 = statistics.median(d), d[int(0.9 * len(d))]
        if med > 0.02 or p90 > 0.05:
            out.append(f"X3s {len(d)} skaters without a new game moved Season: median {med:.3f}, 90th percentile {p90:.3f}")
    # X4 next-game dress up more than 0.15 with no game and no status change (a misfiring boost, findings 183 and 213)
    jumps = [p["name"] for p, o in same if p.get("cr_dress_next") is not None and o.get("cr_dress_next") is not None
             and p["cr_dress_next"] - o["cr_dress_next"] > 0.15 and p.get("injury") == o.get("injury")
             and p.get("dfo_out") == o.get("dfo_out") and _gp(p) == _gp(o)]
    if jumps:
        out.append(f"X4 {len(jumps)} skaters' next-game dress rose by over 0.15 with no game or status change: {jumps[:5]}")
    return out


def published():
    """The copy of players.json at git HEAD, or None."""
    try:
        return json.loads(subprocess.run(["git", "show", "HEAD:data/players.json"], cwd=ROOT, capture_output=True, check=True).stdout)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


def main(argv):
    path = ROOT / "data" / "players.json"
    data = json.loads(path.read_text())
    errs = problems(data)
    if "--against-head" in argv:
        prev = published()
        errs += problems_vs_prev(data, prev) if not errs else []
        for w in warnings(data, prev):
            print(f"::warning::{w}")
    if "--require-rated" in argv and not data.get("rated"):
        errs.append("published without the Claude Rating: the rating step failed (see the 'python scripts/fetch.py' step's log)")
    for e in errs:
        print(f"::error::{e}")
    if not errs:
        print(f"{path.name} OK: {len(data['players'])} players, rated={data['rated']}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
