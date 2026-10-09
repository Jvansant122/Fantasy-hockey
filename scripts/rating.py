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
EARLY_SHARE_K, EARLY_SHARE_GAMES = 6, 10  # goalie start shares shrink toward last season's split (findings section 41)
SEASON_SHARE_K = 10  # Season rating: games of last season's goalie split blended into this season's
# usage x efficiency skater inputs (correlation research section 4): shrinkage minutes and last season's weight
UX_PRIOR_MIN, UX_PRIOR_PP_MIN, UX_PREV_W = 300.0, 60.0, 0.5
UX_STATS = {"goals": 2, "assists": 1, "shots": 0.1, "hits": 0.1, "blockedShots": 0.5}
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
        self.dress_lr = m.get("dress_logit")
        sd = ROOT / "model" / "season_dress.json"
        self.season_lr = json.loads(sd.read_text()) if sd.exists() else None

    def season_dress(self, x):
        """Share of the team's remaining games he dresses (rest-of-season logistic, findings section 56)."""
        m = self.season_lr
        z = m["intercept"] + sum(c * x[k] for k, c in m["coef"].items())
        return 1 / (1 + math.exp(-z))

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

    def dress_logit(self, f):
        """P(dresses) per team game from lineup history (correlation research section 6, handoff row 11)."""
        m = self.dress_lr
        z = m["intercept"] + sum(c * f[k] for k, c in m["coef"].items())
        return 1 / (1 + math.exp(-z))

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


def dress_features(games, team, team_games, f, today):
    """Lineup-history inputs for the P(dresses) logistic, from this season's games on his current team since he joined.
    None when he hasn't played for the team yet (the old table and fallbacks apply)."""
    mine = [g for g in games if g["team"] == team]
    if not mine or mine[0]["game"] not in team_games:
        return None
    since = team_games[team_games.index(mine[0]["game"]):]
    played = {g["game"] for g in mine}
    spells, cur = [], 0
    for gid in since:
        if gid in played:
            if cur:
                spells.append(cur)
            cur = 0
        else:
            cur += 1
    os_ = min(cur, 5)
    last_toi = min(max(mine[-1]["toi"], 0), 30)
    out = {f"os{k}": float(os_ == k) for k in range(1, 6)}
    out.update(dressed3=f["dressed3"] or 0, dressed10=f["dressed10"] or 0, miss_rate=1 - len(played) / len(since),
               last_toi=last_toi, toi_drop=min(max(last_toi - (f.get("std_toi") or last_toi), -15), 10), low_toi=float(last_toi < 10),
               os0_x_d10=float(os_ == 0) * (f["dressed10"] or 0), os_pos_x_lasttoi=float(os_ > 0) * last_toi,
               days_since=min((today - date.fromisoformat(mine[-1]["date"])).days, 14), is_D=f["is_D"],
               long_spells=min(sum(x >= 3 for x in spells), 4), short_spells=min(sum(x <= 2 for x in spells), 6),
               new_team=float(len(since) < 5))
    return out


def lineup_chain_share(k, p_out, h, n):
    """Expected share of the next n team games in the lineup, from out-streak k (0 = played the last one): from in,
    drops out with p_out; out k games, returns with the league curve h[k-1] (k capped at len(h))."""
    kmax = len(h)
    dist = [0.0] * (kmax + 1)
    dist[min(k, kmax)] = 1.0
    tot = 0.0
    for _ in range(n):
        new = [0.0] * (kmax + 1)
        new[0] = dist[0] * (1 - p_out) + sum(dist[j] * h[j - 1] for j in range(1, kmax + 1))
        new[1] = dist[0] * p_out
        for j in range(1, kmax):
            new[j + 1] += dist[j] * (1 - h[j - 1])
        new[kmax] += dist[kmax] * (1 - h[kmax - 1])
        dist = new
        tot += dist[0]
    return tot / max(n, 1)


def season_dress_features(games, team, team_games, f, dress_f, prev, fpg, markov):
    """Inputs for the rest-of-season P(dresses) logistic: next week's lineup features plus last season's games, ice time,
    games left, and a lineup chain (drop-out rate since joining the team, league return curve by games missed)."""
    mine = {g["game"] for g in games if g["team"] == team}
    since = team_games[team_games.index(next(g["game"] for g in games if g["team"] == team)):]
    seq = [gid in mine for gid in since]
    n_in = sum(1 for a in seq[:-1] if a)
    n_drop = sum(1 for a, b in zip(seq, seq[1:]) if a and not b)
    k = 0
    for x in reversed(seq):
        if x:
            break
        k += 1
    pos = "D" if f["is_D"] else "F"
    prior = markov["prior_games"]
    p_out = (n_drop + prior * markov["p_out_mean"][pos]) / (n_in + prior)
    left = max(82 - len(team_games), 0)
    share = lineup_chain_share(k, p_out, markov["return_curve"][pos], max(left, 1))
    share = min(max(share, 0.01), 0.99)
    x = dict(dress_f)
    x.update(prev_dress_rate=min((prev or {}).get("gp", 0) / 82, 1.0), no_prev=0.0 if prev else 1.0,
             std_toi=f.get("std_toi") or 0, l5_toi=f.get("l5_toi") or 0, std_pptoi=f.get("std_pptoi") or 0, fpg=fpg,
             games_left=left, os_long=float(k >= 6), os_raw=min(k, 40), mk_logit=math.log(share / (1 - share)), mk_p_out=p_out)
    return x


def ux_features(f, pos, mu):
    """Usage x efficiency inputs (research/correlation/findings.md section 4): per-minute scoring rates from this season
    plus half of last season, shrunk to the position mean over 300 minutes (60 PP minutes), times last-5 ice time.
    Same arithmetic as model/train.py ux_features."""
    names = ["sx_ux", "sx_ev", "sx_pp", "sx_fp", "sx_shots", "sx_hits", "sx_blockedShots", "sx_ixg"]
    if mu is None or f.get("std_toi") is None or f.get("l5_toi") is None:
        return dict.fromkeys(names)
    m = mu[pos]
    gp_s, gp_p = f["std_gp"], (f.get("prev_gp") or 0) * UX_PREV_W
    def tot(c):
        return (f.get(f"std_{c}") or 0) * gp_s + (f.get(f"prev_{c}") or 0) * gp_p
    toi, pp = tot("toi"), tot("pptoi")
    r = {c: (tot(c) + UX_PRIOR_MIN * m[c]) / (toi + UX_PRIOR_MIN) for c in list(UX_STATS) + ["ixg", "fp"]}
    r_ev = (tot("points") - tot("ppPoints") + UX_PRIOR_MIN * m["evpts"]) / (toi - pp + UX_PRIOR_MIN)
    r_pp = (tot("ppPoints") + UX_PRIOR_PP_MIN * m["pp"]) / (pp + UX_PRIOR_PP_MIN)
    t5, p5 = f["l5_toi"], f.get("l5_pptoi") or 0
    out = {"sx_ux": sum(w * r[c] for c, w in UX_STATS.items()) * t5 + 0.5 * r_pp * p5,
           "sx_ev": r_ev * max(t5 - p5, 0), "sx_pp": r_pp * p5, "sx_fp": r["fp"] * t5}
    for c in ("shots", "hits", "blockedShots", "ixg"):
        out[f"sx_{c}"] = r[c] * t5
    return out


def goalie_starts(model, team, goalies, starts_by_team, prev_starts, game_dates, today, confirmed=None, today_p=None, season_share=None,
                  injury=None):
    """Expected starts in the rest of this week for each goalie on the team.

    starts_by_team: this season's starter per team game, oldest first, as (date, goalie id).
    confirmed: {date: goalie id} of Daily Faceoff confirmed starters; those games count as 1 for him, 0 for the others.
    today_p: if given, filled with each goalie's chance of starting today's game.
    season_share: if given, filled with each goalie's long-run share of team starts (this season's starts
    blended with last season's split over SEASON_SHARE_K games), for the Season rating.
    injury: {goalie id: ESPN injury factor}; scales his chance before each game is split among the team's goalies,
    so an injured starter's games go to his partner (findings section 48)."""
    confirmed, injury = confirmed or {}, injury or {}
    hist = starts_by_team.get(team, [])
    seq = [gid for _, gid in hist]
    last_date = hist[-1][0] if hist else None
    cands = set(goalies) | {g for g in seq[-20:]} | set(confirmed.values())
    if not cands:
        return {}
    # early season: pull each share toward his share of the team's starts last season, fading out by the team's
    # 10th game, so one opening-night start doesn't read as a 100/0 split (findings section 41)
    k = EARLY_SHARE_K * max(0.0, 1 - len(seq) / EARLY_SHARE_GAMES)
    last_total = sum(prev_starts.get(gid, 0) for gid in cands)
    prior = {gid: prev_starts.get(gid, 0) / last_total if last_total else 1 / len(cands) for gid in cands}
    if season_share is not None:
        for gid in cands:
            season_share[gid] = (sum(x == gid for x in seq) + SEASON_SHARE_K * prior[gid]) / (len(seq) + SEASON_SHARE_K)
    shares = {}
    for gid in cands:
        def sh(n, gid=gid):
            window = seq[-n:]
            return (sum(x == gid for x in window) + k * prior[gid]) / (len(window) + k) if window or k else 0.0
        shares[gid] = dict(share5=sh(5), share10=sh(10), share20=sh(20), starts_std_share=sh(len(seq) or 1),
                           prev_season_share=min(prev_starts.get(gid, 0) / 82, 1.0))
    prev_p = {gid: float(bool(seq) and seq[-1] == gid) for gid in cands}
    # rest and workload (findings section 11); games still to come add their likeliest starter as we go
    log = [(date.fromisoformat(d), gid) for d, gid in hist]
    exp = {gid: 0.0 for gid in cands}
    for d in sorted(game_dates):
        if last_date is not None and d <= last_date:  # already played: its real starter is in the history
            continue
        day = date.fromisoformat(d)
        b2b = float(bool(log) and (day - log[-1][0]).days == 1)
        week = [gid for gd, gid in log if (day - gd).days <= 7]
        raw = {}
        for gid in cands:
            streak = 0
            for _, x in reversed(log):
                if x != gid:
                    break
                streak += 1
            streak = min(streak, 10)
            # no start yet in a team's first 10 games is a small sample, not a benching: count from opening night
            last = max((gd for gd, x in log if x == gid), default=log[0][0] if log and len(seq) < EARLY_SHARE_GAMES else None)
            raw[gid] = model.p_start({**shares[gid], "started_prev": prev_p[gid], "b2b": b2b, "started_prev_and_b2b": prev_p[gid] * b2b,
                                      "streak": streak, "days_since_start": min((day - last).days, 30) if last else 30,
                                      "team_games_7d": len(week), "starts_7d": week.count(gid), "streak_x_b2b": streak * b2b})
            raw[gid] *= injury.get(gid, 1.0)
        tot = sum(raw.values()) or 1
        p = {gid: v / tot for gid, v in raw.items()}
        if d in confirmed:
            p = {gid: float(gid == confirmed[d]) for gid in cands}
        if d >= today.isoformat():
            for gid in cands:
                exp[gid] += p[gid]
                if d == today.isoformat() and today_p is not None:
                    today_p[gid] = p[gid]
        likely = max(p, key=p.get)
        log.append((day, likely if p[likely] > 0.5 else None))
        prev_p = p
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
    goalie_inj = {}
    for p in players:
        if p["pos"] == "G" and p.get("injury") in INJURY_DRESS:
            pid = match(p)
            if pid:
                goalie_inj[pid] = INJURY_DRESS[p["injury"]]
    goalie_exp, goalie_today, goalie_season = {}, {}, {}
    lines = line_win_probs(odds, today)
    for team, dates in sched.items():
        goalie_exp.update(goalie_starts(model, team, [pid for pid, _, pos in rosters.get(team, []) if pos == "G"], starts_by_team, prev_gs, dates, today,
                                        confirmed.get(team), goalie_today, goalie_season, goalie_inj))

    unmatched = 0
    injury_log = []
    for p in players:
        pid = match(p)
        if pid is None:
            unmatched += 1
        inj = INJURY_DRESS.get(p.get("injury") or "", 1.0)
        n = news.get(str(p["id"])) or {}
        why = {}
        sat_last = False
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
            season = goalie_season.get(pid, 0.0) * fpg if pid else 0.0
            # tonight's game priced by the betting line: 2.9 + 4.1 x (win probability - 0.5) per start
            p_today = min(goalie_today.get(pid, 0.0), starts) if pid else 0.0
            if team in lines and p_today > 0 and starts > 0:
                line_pps = base + LINE_PPS_SLOPE * (lines[team] - 0.5)
                why["line"] = {"win_prob": round(lines[team], 2), "pps": round(line_pps, 2)}
                fpg = ((starts - p_today) * fpg + p_today * line_pps) / starts
            why["gs"] = own.get(pid, [0, 0])[1] if pid else None
            why["p_today"] = round(goalie_today.get(pid, 0.0), 2) if pid else 0.0  # chance he starts tonight (stream card)
            exp_games = starts  # ESPN injury already applied in goalie_starts, where his starts go to his partner
            p_dress = None
        else:
            gl = by_player.get(pid, []) if pid else []
            team = gl[-1]["team"] if gl else ESPN_TO_NHL.get(p["team"], p["team"])
            f = skater_features(gl, prev.get(pid), xg_now.get(pid), xg_prev.get(pid), team_games.get(team, []))
            f["is_D"] = 1.0 if p["pos"] == "D" else 0.0
            f.update(ux_features(f, "D" if p["pos"] == "D" else "F", model.sk.get("ux_means")))
            fpg = model.fpg(f) if pid else model.sk["replacement_fpg"]["D" if p["pos"] == "D" else "F"]
            tg = team_games.get(team, [])
            df_ = None
            if pid is None:  # not on an NHL roster or in NHL stats this season or last
                p_dress = NOT_PLAYING_DRESS
            elif f["dressed3"] is None:  # team hasn't played yet
                p_dress = 0.9 if prev.get(pid) else 0.5
            else:
                df_ = dress_features(gl, team, tg, f, today) if model.dress_lr else None
                p_dress = model.dress_logit(df_) if df_ else model.dress(f["dressed3"], f["dressed10"])
                last = gl[-1]["date"] if gl else None
                if len(tg) >= 3 and (last is None or (today - date.fromisoformat(last)).days > 14):
                    p_dress = min(p_dress, NOT_PLAYING_DRESS)
            # missed his team's most recent game: the mid-week swap card's trigger (findings section 51)
            sat_last = bool(pid and tg and tg[-1] not in {g["game"] for g in gl})
            # Season rating: rest-of-season share of games he dresses (findings section 56), before ESPN's injury cap and news
            if df_ and model.season_lr:
                season = fpg * model.season_dress(season_dress_features(gl, team, tg, f, df_, prev.get(pid), fpg, model.season_lr["markov"]))
            else:
                season = fpg * p_dress
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
        p["cr_season"] = round(season, 2)
        p["cr_games"] = round(exp_games, 2)
        p["cr_dress"] = None if p_dress is None else round(p_dress, 2)
        p["sat_last"] = sat_last
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
        for key in ("cr", "cr_season"):
            vals = sorted(p[key] for p in grp)
            for p in grp:
                below = sum(v < p[key] for v in vals)
                p[key + "_pct" if key != "cr" else "cr_pct"] = round(100 * below / max(len(vals) - 1, 1))
    write_injury_log(injury_log, today)
    print(f"Claude Rating: {len(players)} players, {unmatched} not matched to NHL data, {len(news)} news entries")
    return unmatched
