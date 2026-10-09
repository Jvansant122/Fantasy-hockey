"""Claude Rating: projected fantasy points for the rest of this matchup.

Skaters: projected FP/G (per-stat ridge on recent ice time, power-play time, season and last-season output, xG)
  x games left x P(dresses), with ESPN injury status on top.
Goalies: expected starts (logistic start model on recent start share and back-to-backs) x points per start.

Model weights live in model/rating_model.json (fit by model/train.py on 2021-26 data). Inputs come from the
NHL stats API and MoneyPuck. Research and validation: the project's research/claude-rating folder.

News hook: data/news.json (optional) can carry up to NEWS_CAP flagged players. Each entry shows its flag and
source in the tooltip; it only changes the numbers when "apply" is true, and then only through games, starts
or a capped FP/G multiplier, never the rating directly.
"""
import json
import math
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
NHL_STATS = "https://api.nhle.com/stats/rest/en"
NHL_WEB = "https://api-web.nhle.com/v1"
MONEYPUCK = "https://moneypuck.com/moneypuck/playerData/seasonSummary/{year}/regular/skaters.csv"
UA = {"User-Agent": "Mozilla/5.0 (fantasy-hockey-lookup)"}
WEIGHTS = {"goals": 2, "assists": 1, "ppPoints": 0.5, "shPoints": 0.5, "shots": 0.1, "hits": 0.1, "blockedShots": 0.5}
GAME_RATES = ["fp", "toi", "pptoi", "shots", "iCF", "hits", "blockedShots", "goals", "assists", "ppPoints", "points"]
# ESPN status caps the chance to dress (research findings section 15); recheck day-to-day after ~3 weeks of logs
INJURY_DRESS = {"OUT": 0.0, "INJURY_RESERVE": 0.0, "SUSPENSION": 0.0, "DAY_TO_DAY": 0.5}
INJURY_LOG = ROOT / "data" / "injury_log.jsonl"
INJURY_LOG_DAYS = 28
NOT_PLAYING_DRESS = 0.1  # no NHL game in 14+ days while the team kept playing (outside what the research measured)
ET = ZoneInfo("America/New_York")
LINE_PPS_SLOPE = 4.1  # points per start per unit of win probability implied by the moneyline (findings section 30)
# expected fantasy points (xG research, sections 3-5): assist-share priors (all situations, power play) and the hot/cold note
XFP_SHARE_F, XFP_SHARE_D, XFP_PRIOR_GOALS = (0.51, 0.46), (0.32, 0.57), 15
LUCK_MIN_GP, LUCK_GAP = 15, 0.15
PPS_PRIOR = 15  # starts of league-average points blended into each goalie's own points per start
NEWS_CAP = 25
# ESPN team abbreviations that differ from the NHL's
ESPN_TO_NHL = {"TB": "TBL", "NJ": "NJD", "SJ": "SJS", "LA": "LAK", "UTAH": "UTA", "WAS": "WSH", "MON": "MTL", "CLB": "CBJ"}


def nhl_get(s, url, **params):
    for attempt in range(3):
        try:
            r = s.get(url, params=params, headers=UA, timeout=60)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == 2:
                raise
            print(f"Retrying {url}: {e}", file=sys.stderr)


def stats_report(s, report, season, start=None, end=None):
    """One NHL stats API report. With dates: one row per player-game; without: season totals per player."""
    exp = f"seasonId={season} and gameTypeId=2"
    if start:
        exp += f' and gameDate>="{start}" and gameDate<="{end}"'
    return nhl_get(s, f"{NHL_STATS}/{report}", isAggregate="false" if start else "true", isGame="true" if start else "false",
                   start=0, limit=-1, cayenneExp=exp).json()["data"]


def weekly(first, last):
    d = first
    while d <= last:
        yield d.isoformat(), min(d + timedelta(days=6), last).isoformat()
        d += timedelta(days=7)


def norm(name):
    """Lowercase ASCII first and last name, without punctuation or middle initials."""
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    words = [w for w in re.sub(r"[^a-z ]", "", name.replace("-", " ")).split() if len(w) > 1]
    return " ".join(words)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def season_ids(espn_season):
    """ESPN's 2027 season is the NHL's 20262027."""
    y = espn_season - 1
    return y * 10000 + y + 1, (y - 1) * 10000 + y


def load_skater_games(s, season, today):
    """This season's skater games with league fantasy points, keyed by (playerId, gameId)."""
    first = date(season // 10000, 9, 20)
    rows = {}
    for a, b in weekly(first, today - timedelta(days=1)):
        for r in stats_report(s, "skater/summary", season, a, b):
            rows[(r["playerId"], r["gameId"])] = {
                "id": r["playerId"], "game": r["gameId"], "date": r["gameDate"], "team": r["teamAbbrev"],
                "name": r["skaterFullName"], "pos": r["positionCode"], "toi": (r.get("timeOnIcePerGame") or 0) / 60,
                **{k: r.get(k) or 0 for k in ("goals", "assists", "ppPoints", "shPoints", "shots")}}
        for r in stats_report(s, "skater/realtime", season, a, b):
            row = rows.get((r["playerId"], r["gameId"]))
            if row:
                row.update(hits=r.get("hits") or 0, blockedShots=r.get("blockedShots") or 0, iCF=r.get("totalShotAttempts") or 0)
        for r in stats_report(s, "skater/timeonice", season, a, b):
            row = rows.get((r["playerId"], r["gameId"]))
            if row:
                row["pptoi"] = (r.get("ppTimeOnIce") or 0) / 60
    for row in rows.values():
        for k in ("hits", "blockedShots", "iCF", "pptoi"):
            row.setdefault(k, 0)
        row["points"] = row["goals"] + row["assists"]
        row["fp"] = sum(row[k] * w for k, w in WEIGHTS.items())
    return list(rows.values())


def load_goalie_games(s, season, today):
    first = date(season // 10000, 9, 20)
    out = []
    for a, b in weekly(first, today - timedelta(days=1)):
        out += [{"id": r["playerId"], "game": r["gameId"], "date": r["gameDate"], "team": r["teamAbbrev"],
                 "name": r["goalieFullName"], "started": r.get("gamesStarted") or 0,
                 "fp": 2 * (r.get("wins") or 0) - (r.get("losses") or 0) + (r.get("otLosses") or 0) - (r.get("goalsAgainst") or 0)
                       + 0.2 * (r.get("saves") or 0) + 3 * (r.get("shutouts") or 0)}
                for r in stats_report(s, "goalie/summary", season, a, b)]
    return out


def load_prev_season(s, season):
    """Last season's per-game rates per skater, from season totals."""
    out = {}
    for r in stats_report(s, "skater/summary", season):
        gp = r.get("gamesPlayed") or 0
        if gp:
            out[r["playerId"]] = {"gp": gp, "name": r["skaterFullName"], "pos": r["positionCode"],
                                  "toi": (r.get("timeOnIcePerGame") or 0) / 60,
                                  **{k: (r.get(k) or 0) for k in ("goals", "assists", "ppPoints", "shPoints", "shots")}}
    for r in stats_report(s, "skater/realtime", season):
        if r["playerId"] in out:
            out[r["playerId"]].update(hits=r.get("hits") or 0, blockedShots=r.get("blockedShots") or 0, iCF=r.get("totalShotAttempts") or 0)
    for r in stats_report(s, "skater/timeonice", season):
        if r["playerId"] in out:
            out[r["playerId"]]["pptoi_tot"] = (r.get("ppTimeOnIce") or 0) / 60
    rates = {}
    for pid, t in out.items():
        gp = t["gp"]
        fp = sum((t.get(k) or 0) * w for k, w in WEIGHTS.items())
        rates[pid] = {"gp": gp, "name": t["name"], "pos": t["pos"], "fp": fp / gp, "toi": t["toi"],
                      "pptoi": t.get("pptoi_tot", 0) / gp, "points": (t["goals"] + t["assists"]) / gp,
                      **{k: (t.get(k) or 0) / gp for k in ("shots", "iCF", "hits", "blockedShots", "goals", "assists", "ppPoints")}}
    return rates


def load_prev_goalie_starts(s, season):
    return {r["playerId"]: (r.get("gamesStarted") or 0) for r in stats_report(s, "goalie/summary", season)}


def load_xg(s, year):
    """MoneyPuck season file: xG per game (all situations) for the model, plus the season counts behind
    expected fantasy points (all situations, power play and short-handed). Empty if the file isn't there yet."""
    try:
        text = nhl_get(s, MONEYPUCK.format(year=year)).text
    except requests.RequestException as e:
        print(f"MoneyPuck {year} unavailable: {e}", file=sys.stderr)
        return {}
    lines = text.splitlines()
    head = lines[0].split(",")
    ix = {c: head.index(c) for c in head}
    num = lambda f, c: float(f[ix[c]] or 0) if c in ix else 0.0
    sits = {"all": "", "5on4": "pp_", "4on5": "sh_"}
    raw = {}
    for line in lines[1:]:
        f = line.split(",")
        if len(f) < len(head) or f[ix["situation"]] not in sits:
            continue
        pre, row = sits[f[ix["situation"]]], raw.setdefault(int(f[ix["playerId"]]), {})
        row.update({pre + "ixg": num(f, "I_F_xGoals"), pre + "goals": num(f, "I_F_goals"),
                    pre + "assists": num(f, "I_F_primaryAssists") + num(f, "I_F_secondaryAssists"),
                    pre + "tm_xg": num(f, "OnIce_F_xGoals") - num(f, "I_F_xGoals"),
                    pre + "tm_goals": num(f, "OnIce_F_goals") - num(f, "I_F_goals")})
        if not pre:
            row.update(gp=num(f, "games_played"), onice_xgf=num(f, "OnIce_F_xGoals"), shots=num(f, "I_F_shotsOnGoal"),
                       hits=num(f, "I_F_hits"), blocks=num(f, "shotsBlockedByPlayer"), pos=f[ix["position"]] if "position" in ix else "")
    out = {}
    for pid, r in raw.items():
        if r.get("gp"):
            out[pid] = {**r, "ixg": r["ixg"] / r["gp"], "onice_xgf": r["onice_xgf"] / r["gp"]}
    return out


def expected_fp(now, prev, is_d):
    """Season fantasy points per game, actual and expected from chances (xG research, sections 3-5).

    Goals become 2 x his xG; assists become teammates' on-ice xG x his share of the teammate goals he assists on
    (this season + last, shrunk to the position average over 15 goals); PP and SH points the same way.
    Shots, hits and blocks count as they are."""
    if not now or not now.get("gp"):
        return None
    prev = prev or {}
    p_all, p_pp = (XFP_SHARE_D if is_d else XFP_SHARE_F)
    share = lambda a, g, p: (now.get(a, 0) + prev.get(a, 0) + XFP_PRIOR_GOALS * p) / (now.get(g, 0) + prev.get(g, 0) + XFP_PRIOR_GOALS)
    s_all, s_pp = share("assists", "tm_goals", p_all), share("pp_assists", "pp_tm_goals", p_pp)
    stable = 0.1 * now["shots"] + 0.1 * now["hits"] + 0.5 * now["blocks"]
    x = (2 * now["ixg"] * now["gp"] + s_all * now["tm_xg"] + 0.5 * (now.get("pp_ixg", 0) + s_pp * now.get("pp_tm_xg", 0))
         + 0.5 * (now.get("sh_ixg", 0) + s_all * now.get("sh_tm_xg", 0)) + stable)
    actual = (2 * now["goals"] + now["assists"] + 0.5 * (now.get("pp_goals", 0) + now.get("pp_assists", 0))
              + 0.5 * (now.get("sh_goals", 0) + now.get("sh_assists", 0)) + stable)
    gp = now["gp"]
    out = {"gp": int(gp), "fpg": round(actual / gp, 2), "xfpg": round(x / gp, 2)}
    gap = out["fpg"] - out["xfpg"]
    if gp >= LUCK_MIN_GP and abs(gap) >= LUCK_GAP:
        out["luck"] = "hot" if gap > 0 else "cold"
    return out


def week_schedule(s, monday):
    """NHL abbrev -> sorted list of game dates (ISO) in the Monday-Sunday week."""
    out = {}
    for day in nhl_get(s, f"{NHL_WEB}/schedule/{monday.isoformat()}").json().get("gameWeek", []):
        for g in day.get("games", []):
            if g.get("gameType") == 2:
                for side in ("awayTeam", "homeTeam"):
                    out.setdefault(g[side]["abbrev"], []).append(day["date"])
    return out


def roster(s, team):
    """Current NHL roster: [(id, full name, position code)]."""
    try:
        data = nhl_get(s, f"{NHL_WEB}/roster/{team}/current").json()
    except requests.RequestException:
        return []
    return [(p["id"], f'{p["firstName"]["default"]} {p["lastName"]["default"]}', p.get("positionCode", "G" if grp == "goalies" else ""))
            for grp in ("forwards", "defensemen", "goalies") for p in data.get(grp, [])]


class Model:
    def __init__(self):
        m = json.loads((ROOT / "model" / "rating_model.json").read_text())
        self.sk, self.dress_table, self.g = m["skater"], m["dress"], m["goalie"]

    def fpg(self, feats):
        total = self.sk["intercept"]
        for name, c in zip(self.sk["features"], self.sk["coef"]):
            v = feats.get(name)
            total += c * (self.sk["fill"][name] if v is None else v)
        return max(total, 0.0)

    def dress(self, d3, d10):
        b10 = 0 if d10 <= 0.5 else 1 if d10 <= 0.8 else 2
        key = f"{round(d3 * 3)}_{b10}"
        if key in self.dress_table:
            return self.dress_table[key]
        same = [v for k, v in self.dress_table.items() if k.startswith(f"{round(d3 * 3)}_")]
        return sum(same) / len(same) if same else 0.5

    def p_start(self, feats):
        z = self.g["intercept"] + sum(c * feats[n] for n, c in zip(self.g["features"], self.g["coef"]))
        return 1 / (1 + math.exp(-z))


def skater_features(games, prev, xg_now, xg_prev, team_games):
    """Inputs for one skater. games: his games this season, oldest first. team_games: his team's game ids, oldest first."""
    f = {}
    if prev:
        for r in GAME_RATES:
            f[f"prev_{r}"] = prev.get(r)
        f["prev_gp"] = prev["gp"]
    f["no_prev"] = 0.0 if prev else 1.0
    f["prev_gp"] = f.get("prev_gp", 0)
    for k in ("ixg", "onice_xgf"):
        f[f"prev_{k}"] = (xg_prev or {}).get(k)
        f[f"std_{k}"] = (xg_now or {}).get(k)
    gp = len(games)
    f["std_gp"] = gp
    for r in GAME_RATES:
        for n, w in ((5, "l5"), (10, "l10"), (20, "l20"), (None, "std")):
            tail = games[-n:] if n else games
            # no games yet this season: lean on last season
            f[f"{w}_{r}"] = mean([g[r] for g in tail]) if tail else f.get(f"prev_{r}")
    if f.get("std_ixg") is None and not gp:
        f["std_ixg"], f["std_onice_xgf"] = f["prev_ixg"], f["prev_onice_xgf"]
    w = min(gp / 20, 1)
    for r in ("fp", "toi", "pptoi", "shots", "blockedShots", "hits"):
        v = f.get(f"std_{r}")
        f[f"std_{r}_w"] = None if v is None else v * w
    played = {g["game"] for g in games}
    for n in (3, 10):
        last = team_games[-n:]
        f[f"dressed{n}"] = sum(g in played for g in last) / len(last) if last else None
    return f


def goalie_starts(model, team, goalies, starts_by_team, prev_starts, game_dates, today, confirmed=None, today_p=None):
    """Expected starts in the rest of this week for each goalie on the team.

    starts_by_team: this season's starter per team game, oldest first, as (date, goalie id).
    confirmed: {date: goalie id} of Daily Faceoff confirmed starters; those games count as 1 for him, 0 for the others.
    today_p: if given, filled with each goalie's chance of starting today's game."""
    confirmed = confirmed or {}
    hist = starts_by_team.get(team, [])
    seq = [gid for _, gid in hist]
    last_date = hist[-1][0] if hist else None
    cands = set(goalies) | {g for g in seq[-20:]} | set(confirmed.values())
    if not cands:
        return {}
    shares = {}
    for gid in cands:
        sh = lambda n: (sum(x == gid for x in seq[-n:]) / len(seq[-n:])) if seq else 0.0
        shares[gid] = dict(share5=sh(5), share10=sh(10), share20=sh(20), starts_std_share=sh(len(seq) or 1),
                           prev_season_share=min(prev_starts.get(gid, 0) / 82, 1.0))
    prev_p = {gid: float(bool(seq) and seq[-1] == gid) for gid in cands}
    exp = {gid: 0.0 for gid in cands}
    prev_day = last_date
    for d in sorted(game_dates):
        b2b = float(prev_day is not None and (date.fromisoformat(d) - date.fromisoformat(prev_day)).days == 1)
        raw = {gid: model.p_start({**shares[gid], "started_prev": prev_p[gid], "b2b": b2b, "started_prev_and_b2b": prev_p[gid] * b2b})
               for gid in cands}
        tot = sum(raw.values()) or 1
        p = {gid: v / tot for gid, v in raw.items()}
        if d in confirmed:
            p = {gid: float(gid == confirmed[d]) for gid in cands}
        if d >= today.isoformat():
            for gid in cands:
                exp[gid] += p[gid]
                if d == today.isoformat() and today_p is not None:
                    today_p[gid] = p[gid]
        prev_p, prev_day = p, d
    return exp


def write_injury_log(entries, today):
    """Keep the last INJURY_LOG_DAYS days of ESPN injury overrides, one line per player per day, to check them later."""
    cutoff = (today - timedelta(days=INJURY_LOG_DAYS)).isoformat()
    kept = []
    if INJURY_LOG.exists():
        for line in INJURY_LOG.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if cutoff <= row.get("date", "") < today.isoformat():
                kept.append(row)
    INJURY_LOG.write_text("".join(json.dumps(r) + "\n" for r in kept + entries))


def load_news():
    path = ROOT / "data" / "news.json"
    if not path.exists():
        return {}
    items = json.loads(path.read_text()).get("players", {})
    return dict(list(items.items())[:NEWS_CAP])


def utcnow():
    return datetime.now(timezone.utc)


def win_prob(home_ml, away_ml):
    """Home team's win probability from American moneylines, with the bookmaker's margin removed."""
    imp = lambda ml: -ml / (-ml + 100) if ml < 0 else 100 / (ml + 100)
    h, a = imp(home_ml), imp(away_ml)
    return h / (h + a)


def line_win_probs(odds, today, now=None):
    """{NHL abbrev: win probability} for today's games that haven't started, from leads.fetch_odds."""
    now = now or utcnow()
    out = {}
    for g in (odds or {}).get("games", []):
        try:
            start = datetime.fromisoformat(g["start_utc"].replace("Z", "+00:00"))
            if start <= now or start.astimezone(ET).date() != today:
                continue
            ph = win_prob(g["home"]["MONEY_LINE_2_WAY"], g["away"]["MONEY_LINE_2_WAY"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        out[g["home"]["team"]], out[g["away"]["team"]] = ph, 1 - ph
    return out


def add_ratings(players, espn_season, today, monday, starters=(), odds=None):
    """Add Claude Rating fields to each player dict from fetch.py (in place).

    starters: Daily Faceoff starting-goalie pages (leads.fetch_starters) for today and later this week.
    odds: today's betting lines (leads.fetch_odds); they price tonight's goalie starts (research section 30)."""
    s = requests.Session()
    season, prev_season = season_ids(espn_season)
    model = Model()
    games = load_skater_games(s, season, today)
    goalie_games = load_goalie_games(s, season, today)
    prev = load_prev_season(s, prev_season)
    prev_gs = load_prev_goalie_starts(s, prev_season)
    xg_now, xg_prev = load_xg(s, season // 10000), load_xg(s, prev_season // 10000)
    sched = week_schedule(s, monday)
    news = load_news()

    # team game order this season (every game has goalie rows) and the starter of each
    team_games, starts_by_team = {}, {}
    for g in sorted(goalie_games, key=lambda g: g["date"]):
        if g["game"] not in team_games.setdefault(g["team"], []):
            team_games[g["team"]].append(g["game"])
        if g["started"]:
            starts_by_team.setdefault(g["team"], []).append((g["date"], g["id"]))
    # each goalie's points per start this season, shrunk toward the league mean (findings section 27)
    base = model.g["points_per_start"]
    own = {}
    for g in goalie_games:
        if g["started"]:
            t = own.setdefault(g["id"], [0.0, 0])
            t[0] += g["fp"]; t[1] += 1
    pps = {gid: (fp + base * PPS_PRIOR) / (n + PPS_PRIOR) for gid, (fp, n) in own.items()}
    by_player = {}
    for g in sorted(games, key=lambda g: g["date"]):
        by_player.setdefault(g["id"], []).append(g)

    # NHL name index for matching ESPN players
    index = {}
    def add(pid, name, team, pos):
        index.setdefault(norm(name), {})[pid] = (team, pos)
    for pid, gl in by_player.items():
        add(pid, gl[-1]["name"], gl[-1]["team"], gl[-1]["pos"])
    for pid, t in prev.items():
        if pid not in by_player:
            add(pid, t["name"], None, t["pos"])
    for g in goalie_games:
        add(g["id"], g["name"], g["team"], "G")
    rosters = {}
    for team in sched:
        rosters[team] = roster(s, team)
        for pid, name, pos in rosters[team]:
            known = index.get(norm(name), {})
            if pid not in known or known[pid][0] is None:
                add(pid, name, team, pos)

    def match(p):
        cands = index.get(norm(p["name"]), {})
        if not cands:
            return None
        team = ESPN_TO_NHL.get(p["team"], p["team"])
        same = [pid for pid, (t, _) in cands.items() if t == team]
        if len(same) == 1:
            return same[0]
        want_g = p["pos"] == "G"
        pos_ok = [pid for pid, (_, pos) in cands.items() if (pos == "G") == want_g]
        return pos_ok[0] if len(pos_ok) == 1 else None

    # Daily Faceoff confirmed starters (research section 7: worth about a third of a streamed goalie's points)
    confirmed, tonight = {}, {}
    for page in starters:
        for g in page.get("games", []):
            cands = [pid for pid, (t, pos) in index.get(norm(g.get("goalie") or ""), {}).items() if pos == "G" and t == g.get("team")]
            if len(cands) != 1 or not page.get("date") or page["date"] < today.isoformat():
                continue
            if page["date"] == today.isoformat():
                tonight[g["team"]] = (cands[0], g.get("status"))
            if g.get("status") == "Confirmed":
                confirmed.setdefault(g["team"], {})[page["date"]] = cands[0]
    goalie_exp, goalie_today = {}, {}
    lines = line_win_probs(odds, today)
    for team, dates in sched.items():
        goalie_exp.update(goalie_starts(model, team, [pid for pid, _, pos in rosters.get(team, []) if pos == "G"], starts_by_team, prev_gs, dates, today,
                                        confirmed.get(team), goalie_today))

    unmatched = 0
    injury_log = []
    for p in players:
        pid = match(p)
        if pid is None:
            unmatched += 1
        inj = INJURY_DRESS.get(p.get("injury") or "", 1.0)
        n = news.get(str(p["id"])) or {}
        why = {}
        if p["pos"] == "G":
            team = ESPN_TO_NHL.get(p["team"], p["team"])
            starts = goalie_exp.get(pid, 0.0) if pid else 0.0
            seq = [gid for _, gid in starts_by_team.get(team, [])][-10:]
            why = {"exp_starts": round(starts, 2), "team_games": p["games_left"],
                   "share10": round(sum(x == pid for x in seq) / len(seq), 2) if seq and pid else None}
            if team in tonight and pid:
                who, status = tonight[team]
                why["tonight"] = {"starting": who == pid, "status": status or "Unconfirmed"}
            if n.get("apply") and n.get("starts") is not None:
                starts = max(0.0, min(float(n["starts"]), p["games_left"]))
            fpg = pps.get(pid, base) if pid else base
            # tonight's game priced by the betting line: 2.9 + 4.1 x (win probability - 0.5) per start
            p_today = min(goalie_today.get(pid, 0.0), starts) if pid else 0.0
            if team in lines and p_today > 0 and starts > 0:
                line_pps = base + LINE_PPS_SLOPE * (lines[team] - 0.5)
                why["line"] = {"win_prob": round(lines[team], 2), "pps": round(line_pps, 2)}
                fpg = ((starts - p_today) * fpg + p_today * line_pps) / starts
            why["gs"] = own.get(pid, [0, 0])[1] if pid else None
            exp_games = starts * inj
            p_dress = None
        else:
            gl = by_player.get(pid, []) if pid else []
            team = gl[-1]["team"] if gl else ESPN_TO_NHL.get(p["team"], p["team"])
            f = skater_features(gl, prev.get(pid), xg_now.get(pid), xg_prev.get(pid), team_games.get(team, []))
            f["is_D"] = 1.0 if p["pos"] == "D" else 0.0
            fpg = model.fpg(f) if pid else model.sk["replacement_fpg"]["D" if p["pos"] == "D" else "F"]
            tg = team_games.get(team, [])
            if pid is None:  # not on an NHL roster or in NHL stats this season or last
                p_dress = NOT_PLAYING_DRESS
            elif f["dressed3"] is None:  # team hasn't played yet
                p_dress = 0.9 if prev.get(pid) else 0.5
            else:
                p_dress = model.dress(f["dressed3"], f["dressed10"])
                last = gl[-1]["date"] if gl else None
                if len(tg) >= 3 and (last is None or (today - date.fromisoformat(last)).days > 14):
                    p_dress = min(p_dress, NOT_PLAYING_DRESS)
            if n.get("apply"):
                if n.get("dress") is not None:
                    p_dress = max(0.0, min(float(n["dress"]), 1.0))
                if n.get("fpg_mult") is not None:
                    fpg *= max(0.8, min(float(n["fpg_mult"]), 1.2))
            if p.get("injury") in INJURY_DRESS:
                injury_log.append({"date": today.isoformat(), "espn_id": p["id"], "nhl_id": pid, "name": p["name"],
                                   "status": p["injury"], "p_dress_table": round(p_dress, 3), "p_dress": round(min(p_dress, inj), 3)})
            p_dress = min(p_dress, inj)
            exp_games = p["games_left"] * p_dress
            why = {"toi5": f.get("l5_toi"), "toi": f.get("std_toi") if gl else None, "pp5": f.get("l5_pptoi"),
                   "sog": f.get("std_shots"), "blk": f.get("std_blockedShots"), "gp": len(gl),
                   "last_fpg": prev[pid]["fp"] if pid in prev else None, "xfp": expected_fp(xg_now.get(pid), xg_prev.get(pid), p["pos"] == "D") if pid else None}
            why = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in why.items()}
        p["cr"] = round(fpg * exp_games, 2)
        p["cr_fpg"] = round(fpg, 2)
        p["cr_games"] = round(exp_games, 2)
        p["cr_dress"] = None if p_dress is None else round(p_dress, 2)
        p["cr_matched"] = pid is not None
        p["nhl_id"] = pid
        p["cr_why"] = why
        if n:
            p["cr_news"] = {k: n.get(k) for k in ("flag", "source", "quote", "apply") if n.get(k) is not None}

    # percentile within F / D / G among the players on the site
    groups = {}
    for p in players:
        groups.setdefault("G" if p["pos"] == "G" else "D" if p["pos"] == "D" else "F", []).append(p)
    for grp in groups.values():
        vals = sorted(p["cr"] for p in grp)
        for p in grp:
            below = sum(v < p["cr"] for v in vals)
            p["cr_pct"] = round(100 * below / max(len(vals) - 1, 1))
    write_injury_log(injury_log, today)
    print(f"Claude Rating: {len(players)} players, {unmatched} not matched to NHL data, {len(news)} news entries")
    return unmatched
