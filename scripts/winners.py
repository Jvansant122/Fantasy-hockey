"""Predicted winners tab: each NHL game's win chance rolled up from the player projections (findings 450), and each
fantasy matchup's win chance from the Week ratings (findings 451). Standard library only."""
import math
from datetime import date, datetime, timedelta, timezone

# Logistic on home minus away, fit on 6,560 games 2021-26 (findings 450, winners/backtest.py): sum of the expected lineup's
# points per game, goal differential per game (team_strength), the starter's save% x 30 shots, back-to-backs, home ice.
# Out of sample 60.0% right, log loss 0.662 (home team always 53.7%, 0.691; goal differential alone 59.0%, 0.667; closing
# moneylines on 1,651 games 63.1%, 0.648 vs this model's 63.2%, 0.649).
WIN_COEF = {"home": 0.1184, "pts": 0.1781, "gd": 0.3508, "sv": 0.0273, "b2b_home": -0.2955, "b2b_away": 0.3489}
PTS_SHRINK = 40  # games of last season's points per game in each skater's rate (K 10 / 20 / 40: 0.6632 / 0.6623 / 0.6616)
PREV_MIN_GP = 10
LINEUP = {"F": 12, "D": 6}
SHOTS = 30
# chance to dress for a rostered skater the site doesn't rate (depth players ESPN doesn't list): by his latest game
DEPTH_DRESS = (0.85, 0.4, 0.1)  # played the team's last game / one of its last 5 / neither
# Fantasy matchups: game-to-game spread of fantasy points (2021-26 player-games around each player's own mean, findings 451)
SKATER_SD = (0.4, 0.5)  # sd = 0.4 + 0.5 x his FP per dressed game
GOALIE_SD = 3.33  # per start
FANTASY_SLOTS = {"F": 9, "D": 5, "UTL": 1, "G": 2}
FINAL_STATES = ("OFF", "FINAL")


def _group(pos):
    return "D" if pos == "D" else "G" if pos == "G" else "F"


def skater_rates(by_player, prev):
    """{nhl id: points per game}, this season shrunk PTS_SHRINK games toward last season's (a depth skater's rate, the
    25th percentile by position, without 10 games last season). Also returns the depth rate by position group."""
    pool = {"F": [], "D": []}
    for t in prev.values():
        if t.get("gp", 0) >= PREV_MIN_GP and _group(t.get("pos")) in pool:
            pool[_group(t["pos"])].append(t.get("points", 0.0))
    depth = {g: (sorted(v)[len(v) // 4] if v else 0.2) for g, v in pool.items()}
    out = {}
    for pid in set(by_player) | set(prev):
        gl = by_player.get(pid) or []
        t = prev.get(pid) or {}
        pos = (gl[-1]["pos"] if gl else t.get("pos"))
        if _group(pos) == "G":
            continue
        prior = t["points"] if t.get("gp", 0) >= PREV_MIN_GP else depth[_group(pos)]
        pts = sum(g.get("points", g.get("goals", 0) + g.get("assists", 0)) for g in gl)
        out[pid] = (pts + PTS_SHRINK * prior) / (len(gl) + PTS_SHRINK)
    return out, depth


def lineup_points(skaters, rate, dress, depth):
    """Expected points per game of a team's dressed skaters: [(nhl id, group)] on its roster, each weighted by his chance to
    dress, scaled to 12 F and 6 D (a short lineup is filled with depth skaters)."""
    total = 0.0
    for grp, n in LINEUP.items():
        rows = [(dress.get(pid, 0.0), rate.get(pid, depth[grp])) for pid, g in skaters if g == grp]
        w = sum(d for d, _ in rows)
        scale = n / w if w > n else 1.0
        total += sum(d * r for d, r in rows) * scale + max(0.0, n - w) * depth[grp]
    return total


def win_prob(home, away):
    """Home team's win chance from each side's {pts, gd, sv, b2b}."""
    c = WIN_COEF
    z = (c["home"] + c["pts"] * (home["pts"] - away["pts"]) + c["gd"] * (home["gd"] - away["gd"])
         + c["sv"] * SHOTS * (home["sv"] - away["sv"]) + c["b2b_home"] * home["b2b"] + c["b2b_away"] * away["b2b"])
    return 1 / (1 + math.exp(-z))


def depth_dress(gl, team_games):
    """DEPTH_DRESS for a skater the site doesn't rate, from whether he played his team's latest games."""
    if not gl or not team_games:
        return DEPTH_DRESS[2]
    mine = {g["game"] for g in gl}
    if team_games[-1] in mine:
        return DEPTH_DRESS[0]
    return DEPTH_DRESS[1] if mine & set(team_games[-5:]) else DEPTH_DRESS[2]


def nhl_games(week_games, starts, results, names, strength, play_dates, lineups, goalie_sv, lines, now=None):
    """The week's NHL games, one per game, home side first, with the model's home win chance.

    week_games: {team: [(date, opp, home)]}; starts: {(team, date): UTC ISO}; results: {(home, date): {...}} from
    rating.week_schedule; lineups: {team: expected lineup points}; goalie_sv: {team: {date: expected starter save%}};
    play_dates: {team: set of ISO dates it plays}; lines: tonight's de-vigged moneyline {team: p}, shown for reference."""
    now = now or datetime.now(timezone.utc)
    out = []
    for home, gl in sorted(week_games.items()):
        for day, away, is_home in gl:
            if not is_home:
                continue
            res = results.get((home, day)) or {}
            start = starts.get((home, day))
            prev_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            side = lambda t: {"pts": lineups.get(t, 0.0), "gd": strength.get(t, 0.0),
                              "sv": (goalie_sv.get(t) or {}).get(day) or goalie_sv.get("_league", 0.9),
                              "b2b": 1.0 if prev_day in play_dates.get(t, ()) else 0.0}
            h, a = side(home), side(away)
            p = win_prob(h, a) if home in lineups and away in lineups else None
            started = bool(start) and datetime.fromisoformat(start.replace("Z", "+00:00")) <= now
            g = {"date": day, "start": start, "home": home, "away": away,
                 "home_name": names.get(home), "away_name": names.get(away),
                 "p_home": None if p is None else round(p, 3),
                 "inputs": {"pts": [round(h["pts"], 2), round(a["pts"], 2)], "gd": [round(h["gd"], 2), round(a["gd"], 2)],
                            "sv": [round(h["sv"], 3), round(a["sv"], 3)], "b2b": [int(h["b2b"]), int(a["b2b"])]},
                 "state": res.get("state") or ("LIVE" if started else "FUT"), "started": started or res.get("state") in FINAL_STATES}
            if home in lines and not g["started"]:
                g["p_line"] = round(lines[home], 3)
            if res.get("state") in FINAL_STATES and res.get("home_score") is not None:
                g.update(home_score=res["home_score"], away_score=res["away_score"], last=res.get("last"),
                         home_won=res["home_score"] > res["away_score"])
            out.append(g)
    return sorted(out, key=lambda g: (g["date"], g["start"] or "", g["home"]))


def freeze(games, previous, week_start=None):
    """Once a game starts, keep the last win chance published before puck drop (a game that started before this tab
    existed has none, and is shown without a pick). A started game this week that the new list lacks (a run whose NHL
    games failed) is carried over as last published (handoff 436)."""
    old = {(g.get("home"), g.get("date")): g for g in previous or [] if isinstance(g, dict)}
    have = {(g["home"], g["date"]) for g in games}
    keep = [o for k, o in old.items() if k not in have and o.get("started") and o.get("date")
            and (week_start is None or o["date"] >= week_start)]
    for g in games:
        if not g["started"]:
            continue
        o = old.get((g["home"], g["date"]))
        if o and o.get("p_home") is not None and (not o.get("started") or o.get("frozen")):
            g["p_home"], g["inputs"] = o["p_home"], o.get("inputs", g["inputs"])
            if o.get("p_line") is not None:
                g["p_line"] = o["p_line"]
        else:
            g["p_home"] = None
        g["frozen"] = g["p_home"] is not None
    return sorted(games + keep, key=lambda g: (g["date"], g.get("start") or "", g["home"])) if keep else games


def record(games):
    """Right / wrong / home-team-right over this week's finished games with a pre-game pick."""
    done = [g for g in games if g.get("home_won") is not None and g.get("p_home") is not None]
    right = sum((g["p_home"] >= 0.5) == g["home_won"] for g in done)
    return {"games": len(done), "right": right, "home_right": sum(g["home_won"] for g in done)}


# ---- fantasy matchups ----

def _skater_sd(m):
    return SKATER_SD[0] + SKATER_SD[1] * m


def night_values(p, today_sp, started_today):
    """{scoring period: (expected FP, variance)} for each night still to come for a rated player."""
    nights = [sp for sp in p.get("nights") or [] if sp > today_sp or (sp == today_sp and not started_today)]
    if not nights or not p.get("cr"):
        return {}
    if p["pos"] == "G":
        starts = {int(k): v for k, v in (p.get("cr_g_nights") or {}).items()}
        per = p["cr"] / p["cr_games"] if p.get("cr_games") else 0.0
        return {sp: (starts.get(sp, 0.0) * per, starts.get(sp, 0.0) * GOALIE_SD ** 2 + starts.get(sp, 0.0) * (1 - starts.get(sp, 0.0)) * per ** 2)
                for sp in nights if starts.get(sp)}
    d = p.get("cr_dress") or 0.0
    m = p.get("cr_fpg") or 0.0
    e = p["cr"] / len(nights)
    return {sp: (e, d * _skater_sd(m) ** 2 + d * (1 - d) * m ** 2) for sp in nights}


def team_projection(roster, today_sp, sps, now_iso):
    """Expected fantasy points (and variance) still to come for one fantasy team: each night the best expected lineup,
    9 F, 5 D, 1 UTL (any skater) and 2 G, from players not on IR."""
    vals = []
    for p in roster:
        if p.get("ir"):
            continue
        started = bool(p.get("today_start")) and p["today_start"] <= now_iso
        vals.append((p, night_values(p, today_sp, started)))
    mean = var = 0.0
    for sp in sps:
        cands = [(v[sp], _group(p["pos"]), p) for p, v in vals if sp in v]
        cands.sort(key=lambda x: -x[0][0])
        left = dict(FANTASY_SLOTS)
        for (e, vr), grp, p in cands:
            if left.get(grp, 0) > 0:
                left[grp] -= 1
            elif grp != "G" and left["UTL"] > 0:
                left["UTL"] -= 1
            else:
                continue
            mean += e
            var += vr
    return mean, var


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fantasy_matchups(schedule, period, players, team_names, today_sp, sps, now_iso):
    """This matchup's games in the league: points so far from ESPN, the rest projected from the Week ratings, and each
    side's win chance assuming the night-to-night spreads above are independent (findings 451)."""
    by_team = {}
    for p in players:
        if p.get("team_id") is not None:
            by_team.setdefault(p["team_id"], []).append(p)
    rest = [sp for sp in sps if sp >= today_sp]
    out = []
    for m in schedule or []:
        if m.get("matchupPeriodId") != period or not m.get("home") or not m.get("away"):
            continue
        sides = []
        for key in ("home", "away"):
            s = m[key]
            tid = s.get("teamId")
            now = s.get("totalPointsLive")
            if now is None:
                by_sp = s.get("pointsByScoringPeriod") or {}
                now = sum(by_sp.values()) if by_sp else s.get("totalPoints") or 0.0
            mean, var = team_projection(by_team.get(tid, []), today_sp, rest, now_iso)
            sides.append({"id": tid, "name": team_names.get(tid), "now": round(now, 1), "rest": round(mean, 1),
                          "total": round(now + mean, 1), "var": var})
        h, a = sides
        sd = math.sqrt(h["var"] + a["var"])
        diff = h["total"] - a["total"]
        p = _phi(diff / sd) if sd > 0 else (1.0 if diff > 0 else 0.0 if diff < 0 else 0.5)
        for s in sides:
            s.pop("var")
        out.append({"home": h, "away": a, "p_home": round(p, 3), "sd": round(sd, 1)})
    return out
