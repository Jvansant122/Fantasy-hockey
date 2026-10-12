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
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
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
# where he ranks in his team-game's PP time and ice time, this season's windows only (findings section 201)
RANK_RATES = ["pp_rank", "toi_rank", "pp1", "top_line"]
P2_RATES = ["fp", "toi", "pptoi", "shots", "hits", "blockedShots", "points"]  # two seasons ago (model/train.py P2_RATES)
# position x usage and squared ice time inputs (findings section 201); same as model/train.py
D_X = ["std_toi", "std_pptoi", "l5_toi", "l5_pptoi", "std_blockedShots", "std_hits"]
SQUARED = ["std_toi", "l5_toi", "std_pptoi"]
# ESPN status caps the chance to dress (research findings section 15); recheck day-to-day after ~3 weeks of logs
INJURY_DRESS = {"OUT": 0.0, "INJURY_RESERVE": 0.0, "SUSPENSION": 0.0, "DAY_TO_DAY": 0.5}  # goalies (inside goalie_starts)
# skaters (findings section 58): injured players still dress for some of the week, unless the note says it's a long absence
SKATER_INJURY_CAP = {"OUT": 0.35, "INJURY_RESERVE": 0.15, "SUSPENSION": 0.0, "DAY_TO_DAY": 0.5}
LONG_ABSENCE = re.compile(r"remainder of the (?:\d{4}-\d{2} )?(?:regular )?(?:season|campaign)|rest of the (?:regular )?(?:season|campaign)"
                          r"|season-ending|out for the (?:season|year)|long[- ]term injured reserve|\bltir\b|surgery|surgical|\boperation\b"
                          r"|month-to-month|week-to-week|indefinitely|no timetable|no timeline|extended period|long-term"
                          r"|without a (?:clear )?(?:timetable|timeline)|(?:a few|several) (?:more )?weeks", re.I)
# long-absence words read from the long comment's sentences that name him or no other injured player; not 'surgery',
# which the long comment often recaps from an old injury (findings 236)
LONG_OWN = re.compile(r"week-to-week|month-to-month|indefinitely|(?:no|without a|without any) (?:clear )?(?:timetable|timeline)"
                      r"|(?:a few|several|a couple of) (?:more )?weeks|unclear when he(?:'ll| will) (?:be (?:available|ready) to )?return", re.I)
MONTHS = {m: i for i, m in enumerate("january february march april may june july august september october november december".split(), 1)}
# 'not expected back until November', 'until at least mid-December', 'before Christmas' (findings 236)
UNTIL = re.compile(r"(?:until|before|back in|return in|until at least|not before)\s+(?:at least\s+|around\s+|roughly\s+|sometime in\s+|\"?maybe\"?\s+)*"
                   r"(early|mid|late|the middle of|the end of)?[- ]?(" + "|".join(MONTHS) + r")\b|before christmas", re.I)
# a number after these words is a stat line or an old absence, not a return date ('four points over five games',
# 'missed the final 26 games'), except 'out/miss for the first ...' (findings 236)
TL_SKIP_PRE = re.compile(r"\b(?:ago|last|previous|past|first|final|after|since|over|across|through)\s*$")
# ESPN often replaces the real note with a bare status word; the last real one is kept in INJURY_NOTES (findings 236)
PLACEHOLDER_NOTE = re.compile(r"^\s*(ir|ir-nr|out|day-to-day|suspension)\s*$", re.I)
STATUS_SEVERITY = {"DAY_TO_DAY": 0, "OUT": 1, "SUSPENSION": 1, "INJURY_RESERVE": 2}
# ESPN injury feed's status words -> fantasy statuses, for notes on players outside the data (findings 241 (5))
FEED_STATUS = {"Day-To-Day": "DAY_TO_DAY", "Out": "OUT", "Suspension": "SUSPENSION", "Injured Reserve": "INJURY_RESERVE"}
INJURY_NOTES_DAYS = 60
# a one-game OUT note ('won't play Thursday') is stale once his team has played after it: ESPN moves him to day-to-day
# the next morning (findings 236)
ONE_GAME_OUT = re.compile(r"(?:won't|will not|not) (?:play|suit up|dress)|will miss|ruled out (?:for|of)", re.I)
ONE_GAME_NOT = re.compile(r"week|month|indefin|games", re.I)
ONE_GAME_SAME_DAY_HOUR = 17  # game dates have no start time: a note posted before 5 PM ET came before that night's game
# ESPN OUT on the day of his team's game: 0 of 20 dressed (findings 235); later games keep the OUT cap
OUT_TODAY_CAP = 0.03
SEASON_OUT = re.compile(r"remainder of the (?:\d{4}-\d{2} )?(?:regular )?(?:season|campaign)|rest of the (?:regular )?(?:season|campaign)"
                        r"|season-ending|out for the (?:season|year)", re.I)
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "ten": 10, "twelve": 12,
           "a couple of": 2, "a few": 3, "several": 4}
_NUM = r"(\d+|one|two|three|four|five|six|seven|eight|ten|twelve|a couple of|a few|several)"
TIMELINE = re.compile(_NUM + r"(?:\s*(?:-|to)\s*" + _NUM + r")?\s*(?:more\s+|additional\s+|to\s+\w+\s+)?(day|week|month|game)s?", re.I)
INJURY_LOG = ROOT / "data" / "injury_log.jsonl"
INJURY_NOTES = ROOT / "data" / "injury_notes.json"
INJURY_LOG_DAYS = 28
NOT_PLAYING_DRESS = 0.1  # not matched to NHL data at all
PREV_MIN_GP = 10  # under this many games last season, use the season before (findings 216)
# no NHL game in 14+ days while the team kept playing: cap by days since his last game (handoff 19-20 audit, re-test 3)
IDLE_DRESS = ((21, 0.5), (35, 0.2))  # 15-21 days 0.5, 22-35 days 0.2, longer 0.1


def idle_cap(days):
    return next((cap for top, cap in IDLE_DRESS if days <= top), NOT_PLAYING_DRESS)
ET = ZoneInfo("America/New_York")
LINE_PPS_SLOPE = 4.1  # points per start per unit of win probability implied by the moneyline (findings section 30)
# this week's starts (not Season): his own points per start already carry his team's strength, so with the line or the
# schedule on top strong teams' starters read ~0.5 high. Week pricing keeps 0.4 of his gap to the league's rate this season
# (shrunk over 300 starts to 2.89), and the schedule slope drops to 3.0. 13,120 starts 2021-26: per-start error -1.5% and
# weekly goalie error -0.3%, rank flat (findings 231 (8), handoff 134)
WEEK_PPS_KEEP, WEEK_SCHED_SLOPE, LEAGUE_PPS_PRIOR = 0.4, 3.0, 300
# expected fantasy points (xG research, sections 3-5): assist-share priors (all situations, power play) and the hot/cold note
XFP_SHARE_F, XFP_SHARE_D, XFP_PRIOR_GOALS = (0.51, 0.46), (0.32, 0.57), 15
LUCK_MIN_GP, LUCK_GAP = 15, 0.15
EARLY_SHARE_K, EARLY_SHARE_GAMES = 6, 10  # goalie start shares shrink toward last season's split (findings section 41)
SEASON_SHARE_K = 10  # Season rating: games of last season's goalie split blended into this season's
# usage x efficiency skater inputs (correlation research section 4): shrinkage minutes and last season's weight
UX_PRIOR_MIN, UX_PRIOR_PP_MIN, UX_PREV_W = 300.0, 60.0, 0.5
UX_STATS = {"goals": 2, "assists": 1, "shots": 0.1, "hits": 0.1, "blockedShots": 0.5}
DISPLACED_CUT = {"F": 0.08, "D": 0.12}  # least-used healthy skater sits more when a regular returns (findings section 63)
AGE_YOUNG, AGE_OLD = (24, 0.01), (29, 0.009)  # Season FP/G + 0.01 a year under 24, - 0.009 a year over 29 (findings 310 (3))
NEXT_UP_PP_D_FPG, NEXT_UP_PP_D_MIN = 0.15, 1.0  # Week FP/G for the D next in line when the top PP D (1+ PP min/G) is out (findings 310 (5))
SCHED_SHRINK, SCHED_WIN = 15, (-0.175, 0.594, 0.349)  # starts without a line priced by team strength (findings 70; slope WEEK_SCHED_SLOPE)
SEASON_GAMES = 84  # 2026-27 regular season per team (findings section 75); last season's inputs stay over 82
PRESEASON_INJURED = {"short": 0.45, "long": 0.3, "season": 0.0}  # rest-of-season dress share, hurt before playing (findings 74)
BAD_START_LOGIT = -1.0  # next-game start logit for a starter who gave up 5+, or was pulled after 3+ (findings 131, 163)
BAD_START_LOGIT_2 = -0.3  # ... and in the team's game after next (findings 132)
BREAK_LOGITS = ((7, 1.5), (4, 0.4))  # team's next game 4-6 / 7+ days after its last: last starter's next-game logit (findings 196)
# second night of a back-to-back, night-1 starter's logit: the team's no. 1 rests more, a backup who played goes again (findings 200)
B2B_NIGHT2_NO1, B2B_NIGHT2_OTHER = -0.8, 0.9
# back-to-backs still ahead in the week, the no. 1's start logit (most starts in the team's last 20, ties to the last starter):
# night 1 when it is the next game, night 2 of that pair, night 2 of a pair further ahead, and every other game except the
# shipped night-2 case above. 2021-26 replay of 12,960 team-game mornings: Brier -0.6%, no. 1 week-sum error -0.7%, better
# in 5 of 5 seasons (findings 231, handoffs 133 and 190)
B2B_AHEAD_NIGHT1, B2B_AHEAD_NIGHT2_NEXT, B2B_AHEAD_NIGHT2_LATER, NO1_ORDINARY_LOGIT = -0.4, -1.2, -0.6, 0.15
# the no. 1 started night 1 and rests night 2: the chain logs his backup as night 2's starter, which carries him into the
# game after it (no. 1 0.687 there vs 0.804 actual). Every other goalie's logit for that game; held-out Brier -4.4%,
# 5 of 5 seasons, calibrated after (findings 222, handoff 223)
AFTER_NO1_REST_OTHER_LOGIT = -1.0
# the last starter 5-7 straight team games in: rested next game more than the model sees; 8+ are workhorses (findings 202)
STREAK_LOGIT, STREAK_RANGE = -1.0, (5, 7)
ROTATION_LOGIT, ROTATION_STARTS = -1.2, 6  # last starter's next game when the last 6+ starts strictly alternate (findings 227)
PATTERN_LOGITS = {(3, True): 0.8, (3, False): -0.4, (4, False): -0.8}  # last starter's next game in a repeating A A B or
# A A B B pattern: (period, the pattern keeps him in) -> shift; a period-4 stay gets nothing (findings 243)
ROTATION_LATER_LOGIT = -0.6
# two goalies started the last 10: by the last starter's last-6 start string (oldest first, 1 = he started), a cut to
# the other goalie's next game (findings 246): the no. 1 back after a single spot start (ends 1 1 1 0 1), a goalie
# answering the other's two starts with three straight (ends 0 0 1 1 1), a rotation after a double (1 1 0 1 0 1)
LAST6_OTHER_LOGITS = {"110101": -0.6, "11101": -1.0, "00111": -1.2}
# three goalies started the last 10: the next start goes to the recent ones more than the model sees; a cut to each of
# the three whose latest start was 3+ team games ago (findings 247: 0.20 against 0.12 actual, Brier -1.8%, every season)
THREE_GOALIE_IDLE_LOGIT = -1.0
# the rest of the week after those next-game rules: the last starter read high in two later games (findings 249, every
# season): a strict rotation's third game (0.48 against 0.37 actual) and, when no rotation, pattern, last-6 or
# three-goalie rule fires, the game after next (0.56 against 0.51)
ROTATION_THIRD_LOGIT, PLAIN_SECOND_LOGIT = -0.9, -0.45


def three_goalie_idle(seq):
    """The goalies to cut next game when exactly three started the team's last 10: those not among the last 3 starters."""
    if len(set(seq[-10:])) != 3:
        return set()
    return set(seq[-10:]) - set(seq[-3:])


def last_six_cut(seq):
    """The other goalie's next-game cut for the last starter's last-6 string, 0 when no string fires or more than two
    goalies started the last 10 (findings 246)."""
    if len(seq) < 10 or len(set(seq[-10:])) != 2:
        return 0.0
    s6 = "".join("1" if x == seq[-1] else "0" for x in seq[-6:])
    key = s6 if s6 == "110101" else s6[-5:] if s6[-5:] in ("11101", "00111") else None
    return LAST6_OTHER_LOGITS.get(key, 0.0)  # ... and the goalie due next, in the game after next unless it's night 2 of a back-to-back (findings 228)
# both were fit from a team's 11th game. Through its 10th a no. 1 on night 2 still shows about 7% against 3% real (2021-26 replay
# with every rostered goalie, n=125, better in 5 of 5 seasons): an extra shift on his share after the split. The streak shift
# already fits there (68% against 63%, and a larger one scores worse) (findings 213, handoff 105)
EARLY_GAMES, B2B_NIGHT2_NO1_EARLY = 10, -1.0
WIN_LOGIT, LOSS_LOGIT = 0.25, -0.2  # next-game start logit after any other win, or loss in regulation or OT (findings 133)
LOSS_LOGITS = {3: -0.35, 4: -0.65}  # ... graded by goals against when the loss wasn't a bad start: 3 or 4 against cut deeper (findings 225)
LOW_GA_WIN_LOGIT = 0.3  # ... plus this after a win allowing 0-1 goals (findings 163)
# a starter who got no decision left early, nearly always hurt: next-game start 0.45 against 0.12 actual (findings 163),
# and he stays out a median of 6 team games, so the cut lasts: games 2-4 read 0.51-0.59 against 0.19-0.22 actual
# (findings 164). Games 2+ are fit after the later-game softening, so they are divided by LATER_GAME_TEMP here.
NO_DECISION_LOGIT = -2.3
NO_DECISION_LATER = (-2.0 / 0.8, -1.4 / 0.8)  # game after next, then every game after that
NO_DECISION_GAMES = 16  # covers a two-week playoff round
# ... and his Season start share runs high until he starts again: 0.48 against 0.33 of the next 20 starts (findings 167)
NO_DECISION_SEASON = 0.68
PRIOR_TOP_CAP = 0.7  # the early-season prior's top share of last season's starts, at most (findings 234; replaced a 70-start floor)
INJURED_TAKER_WEIGHT = 0.5  # an injured partner's weight when a goalie's lost Season share is handed out (findings 234)
# a goalie who came on in relief in the team's last game looks like an idle backup, since only starts are counted, but
# he starts next more often (0.47 against 0.58 after a pulled starter; third goalies and call-ups 0.23 against 0.47):
# start-logit shifts for the next game and later games, plus more for one with 0-3 of the last 20 starts (findings 170)
RELIEF_LOGITS, RELIEF_RARE_LOGIT, RELIEF_RARE_STARTS = (1.2, 0.9), 0.5, 3
LATER_GAME_TEMP = 0.8  # start logits for games 2+ ahead are scaled by this: the chained week runs overconfident (findings 134)
# a goalie in his first 1-3 starts for a new team after a trade or claim: the team got him to play him, but his old-team
# starts don't count, so he reads as a backup (next-game start 0.20 against 0.32 actual, findings 156)
NEW_TEAM_START_LOGIT, NEW_TEAM_STARTS = 1.0, 3
# goalies on today's roster take ~94% of a team's next 20 starts; call-ups, claims and injury fill-ins take the rest,
# so the Season start share is scaled down (a no. 1 read 0.71 against 0.65 actual, findings 161)
SEASON_SHARE_SCALE = 0.88
# ... and it leans toward the goalie stopping more pucks than his partners: + intercept + slope x (his save% minus the
# start-weighted save% of the team's other starters this season, x 100), both shrunk as in shrunk_save_pcts; refit on
# this function's own shares, 2023-26 goalie-Mondays (findings 330 (1), handoff 270)
SAVE_GAP_SEASON = (0.008, 0.0485)
SAVE_SHRINK, LEAGUE_SAVE_PCT = (600, 1000), 0.902
# goalie Season values ran wider than what goalies went on to score (top eighth 2.55 against 2.14, bottom 0.21 against 0.33),
# while skaters' are calibrated: intercept + slope x (Season share x pts/start) for a goalie with any share puts them on the
# skaters' scale for the pooled board, keep values and trades; refit on this site's Season values, 2023-26 Mondays
# (findings 330 (2), handoff 271)
GOALIE_SEASON_SCALE = (0.32, 0.72)
# pulled (under 40:00 of ice time) in his last start, without the no-decision cut: his rest-of-season share came in
# 0.03 below (0.361 against 0.331); refit on this site's shares, 2023-26 Mondays (findings 330 (3), handoff 272)
PULLED_SEASON, PULLED_TOI = 0.03, 2400


def bad_start(g):
    """5+ goals against, or under 45 minutes on ice with 3+ against."""
    return g["ga"] >= 5 or (g["toi"] < 45 * 60 and g["ga"] >= 3)


def last_start_shift(g):
    """Start-logit shifts for the goalie who started the team's last game: (next game, game after next)."""
    if bad_start(g):
        return (BAD_START_LOGIT, BAD_START_LOGIT_2)
    if g.get("wins"):
        return (WIN_LOGIT + (LOW_GA_WIN_LOGIT if g["ga"] <= 1 else 0.0), 0.0)
    if g.get("losses") or g.get("otl"):
        return (LOSS_LOGITS.get(g["ga"], LOSS_LOGIT), 0.0)
    # started, but another goalie got the decision
    return (NO_DECISION_LOGIT, NO_DECISION_LATER[0]) + (NO_DECISION_LATER[1],) * (NO_DECISION_GAMES - 2)


# P(dresses) logit shift by game number from today (1, 2, 3, 4+), for a skater who dressed for his team's last game
# and for one who missed it: the weekly P is right on average but a player who is out is less likely to play the next
# game than the last one, and a regular the reverse (findings 147)
DRESS_SHIFT_IN, DRESS_SHIFT_OUT = (0.6, 0.2, -0.1, -0.3), (-0.55, -0.15, 0.1, 0.2)
# a healthy scratch in his team's last game comes back far more often than an injured player: +0.5 on top of the
# missed-game shift, every game (0.25 to 0.34 per game against 0.345 actual, findings 153)
SCRATCH_LOGIT = 0.5
# a skater who dressed for his team's last game in his first 1-3 games after a trade or waiver claim: the new team plays
# him more than his old-team dressed share says (0.78 to 0.85 per game against 0.86 actual, findings 155)
NEW_TEAM_LOGIT, NEW_TEAM_GAMES = 0.45, 3
# dressed for his team's last game but played a fraction of his season-average ice time: he left early, usually hurt,
# before ESPN flags him (0.70 next game against 0.18 actual under 40%, findings 165). Extra logit shifts for the next
# game, the one after and later games, by last-game TOI share; ESPN's injury cap takes over once he is flagged.
LEFT_EARLY_LOGITS = ((0.40, (-2.5, -1.8, -1.1)), (0.55, (-0.8, -0.5, 0.0)))
# ... and his Season rating's rest-of-season dress share runs high too (0.69/0.63 against 0.60/0.56, findings 166)
LEFT_EARLY_SEASON_LOGIT = -0.4


def left_early_shifts(last_toi, avg_toi):
    """Extra per-game dress logits (next game, the one after, later) for a skater whose last game was short."""
    if not avg_toi or last_toi is None:
        return ()
    for cut, shifts in LEFT_EARLY_LOGITS:
        if last_toi < cut * avg_toi:
            return shifts
    return ()


# just back in the lineup (dressed for the last game): back 1-2 games from a 5+ game absence that was mostly not healthy
# scratches (an injury) he plays more than the site said (0.73 against 0.81); back 1-3 games from a 1-4 game
# healthy-scratch spell he sits again more (0.83 against 0.77): rotation players (findings 168)
BACK_INJURY_LOGIT, BACK_SCRATCH_LOGIT = 0.4, -0.4


def comeback_logit(tg, played, scratch_list, pid):
    """Dress-logit shift for a skater just back in the lineup. tg: his team's game ids, oldest first; played: the ids he
    dressed for; scratch_list(game id): that game's healthy-scratch ids, or None if it can't be read."""
    back = 0
    for x in reversed(tg):
        if x not in played:
            break
        back += 1
    k, gap = len(tg) - back, 0
    while k > 0 and tg[k - 1] not in played:
        gap, k = gap + 1, k - 1
    if not gap or not k or not 1 <= back <= 3:  # no absence, or no games for the team before it
        return 0.0
    long_gap = gap >= 5 and back <= 2
    if not long_gap and gap > 4:
        return 0.0
    lists = [scratch_list(x) for x in tg[k:k + gap][-10:]]
    known = [pid in l for l in lists if l is not None]
    scratched = bool(known) and sum(known) >= 0.5 * len(known)  # unreadable lists count as not scratched
    if long_gap:
        return 0.0 if scratched else BACK_INJURY_LOGIT
    return BACK_SCRATCH_LOGIT if scratched else 0.0


# the Season rating's rest-of-season dress share for a skater in his first 1-10 games for a new team (0.62-0.67 against
# 0.71-0.72 actual, findings 158)
NEW_TEAM_SEASON_LOGIT, NEW_TEAM_SEASON_GAMES = 0.3, 10


def dress_by_game(base, n, sat_last, cap, shift=0.0, by_game=()):
    """Chance he dresses for each of his team's next n games: the weekly P shifted by game number (plus any extra
    logit shift, and by_game's per-game shifts with its last entry covering later games), capped."""
    shifts = DRESS_SHIFT_OUT if sat_last else DRESS_SHIFT_IN
    z = math.log(min(max(base, 1e-4), 1 - 1e-4) / (1 - min(max(base, 1e-4), 1 - 1e-4))) + shift
    extra = [by_game[min(k, len(by_game) - 1)] if by_game else 0.0 for k in range(n)]
    return [min(1 / (1 + math.exp(-(z + shifts[min(k, 3)] + extra[k]))), cap) for k in range(n)]


def load_scratches(s, game_ids):
    """{game id: NHL ids on its healthy-scratch list} from the game page's right rail, which lists healthy scratches
    only, not the injured. A game whose list can't be read or isn't posted is left out."""
    def rail(gid):
        try:
            return nhl_get(s, f"{NHL_WEB}/gamecenter/{gid}/right-rail").json().get("gameInfo") or {}
        except (requests.RequestException, ValueError) as e:
            print(f"No scratch list for game {gid}: {e}", file=sys.stderr)
            return None
    out = {}
    gids = sorted(game_ids)
    for gid, info in zip(gids, pmap(rail, gids)):
        if info is None:
            continue
        sides = [info.get(k) or {} for k in ("awayTeam", "homeTeam")]
        if all(isinstance(t.get("scratches"), list) for t in sides):
            out[gid] = {x["id"] for t in sides for x in t["scratches"] if x.get("id")}
    return out


UNPLAYED_REGULAR, UNPLAYED_REGULAR_GP = 0.38, 60  # rest-of-season dress share, healthy regular yet to play (findings 118)
# an ESPN status (injury, suspension) in the injury log within CLEARED_DAYS that ESPN has now dropped, and he hasn't played since
CLEARED_DAYS = 7
CLEARED_NEW_DRESS, CLEARED_NEW_NEXT, CLEARED_NEW_SEASON = 0.8, 0.75, 0.72  # no games yet this season (findings 172)
CLEARED_LOGIT, CLEARED_SEASON_LOGIT = 0.9, 0.4  # games this season, missed the last one (findings 173)
# DAY_TO_DAY and dressed for the team's last game: cap by that game's TOI over his season average (findings 176)
DTD_PLAYED_CAPS = ((0.55, 0.15), (0.8, 0.5), (float("inf"), 0.75))
OUT_LEFT_EARLY_CAP = 0.15  # OUT and dressed for the last game but under 0.55 of his TOI: 0.17-0.21 actual (findings 176)
DFO_LINEUP_NEXT = 0.9  # sat the last game, no ESPN status, in Daily Faceoff's lines: next-game floor (findings 174)
PPS_PRIOR = 15  # starts of league-average points blended into each goalie's own points per start
NEWS_CAP = 25
OFFSEASON_MOVER_MULT = 0.965  # skater on a new team since last season: leave-one-season-out 0.963-0.970 (findings 230)
# ESPN team abbreviations that differ from the NHL's
ESPN_TO_NHL = {"TB": "TBL", "NJ": "NJD", "SJ": "SJS", "LA": "LAK", "UTAH": "UTA", "WAS": "WSH", "MON": "MTL", "CLB": "CBJ"}


def pmap(f, items, workers=6):
    """[f(x) for x in items], run a few at a time: a run is mostly waiting on ~100-200 small NHL calls, which one after
    another took about 15 s and grow by four calls a week (speed audit, Oct 12 2026). Order and errors as the plain loop."""
    items = list(items)
    if len(items) < 2:
        return [f(x) for x in items]
    with ThreadPoolExecutor(min(workers, len(items))) as ex:
        return list(ex.map(f, items))


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
            time.sleep(2 ** attempt)  # back off: ~100 calls a run, and an immediate retry hits the same blip (findings 230)


# finished weeks of per-game reports never change once the NHL's stat corrections are in, so the update workflow keeps them
# between runs (speed audit plan 3: 4 reports a week, ~110 calls by March). Off unless NHL_CACHE names a folder.
NHL_CACHE = os.environ.get("NHL_CACHE")
CACHE_SETTLED_DAYS = 3  # a week whose last day is at least this many days before today is cached


def stats_report(s, report, season, start=None, end=None, teams=False):
    """One NHL stats API report. With dates: one row per player-game; without: season totals per player
    (teams=True keeps the teamAbbrevs column, which the aggregate view leaves empty)."""
    exp = f"seasonId={season} and gameTypeId=2"
    if start:
        exp += f' and gameDate>="{start}" and gameDate<="{end}"'
    path = None
    if NHL_CACHE and start and date.fromisoformat(end) <= et_date() - timedelta(days=CACHE_SETTLED_DAYS):
        path = Path(NHL_CACHE) / f"{report.replace('/', '-')}_{season}_{start}_{end}.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
    data = nhl_get(s, f"{NHL_STATS}/{report}", isAggregate="false" if start or teams else "true", isGame="true" if start else "false",
                   start=0, limit=-1, cayenneExp=exp).json()["data"]
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        except OSError as e:
            print(f"NHL cache write failed: {e}", file=sys.stderr)
    return data


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


def pos_group(pos):
    """G, D or F from an ESPN or NHL position code."""
    return "G" if pos == "G" else "D" if pos == "D" else "F"


def nickname_cands(index, name, team):
    """Players on the same NHL team with the same last name whose first names share their first two letters
    (ESPN "Zack", "Sam", "Joe" for the NHL's "Zachary", "Samuel", "Joseph"; findings 112)."""
    words = norm(name).split()
    if len(words) < 2 or not team:
        return {}
    first, last = words[0], " ".join(words[1:])
    out = {}
    for key, cands in index.items():
        kw = key.split()
        if len(kw) >= 2 and " ".join(kw[1:]) == last and kw[0][:2] == first[:2] and kw[0] != first:
            out.update({pid: v for pid, v in cands.items() if v[0] == team})
    return out


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def season_ids(espn_season):
    """ESPN's 2027 season is the NHL's 20262027."""
    y = espn_season - 1
    return y * 10000 + y + 1, (y - 1) * 10000 + y


def et_date():
    return datetime.now(ZoneInfo("America/New_York")).date()


def history_end(today):
    """Last day of game history to load: yesterday, or last night when a 12-3 AM run still has ESPN on the day before
    (findings 183). A clock far from today (tests, backfills) leaves it at today - 1."""
    et = et_date()
    return (et if 0 < (et - today).days <= 1 else today) - timedelta(days=1)


def load_skater_games(s, season, today):
    """This season's skater games with league fantasy points, keyed by (playerId, gameId)."""
    first = date(season // 10000, 9, 20)
    rows = {}
    spans = list(weekly(first, history_end(today)))
    reports = dict(zip([(a, rep) for a, _ in spans for rep in ("summary", "realtime", "timeonice")],
                       pmap(lambda x: stats_report(s, f"skater/{x[2]}", season, x[0], x[1]),
                            [(a, b, rep) for a, b in spans for rep in ("summary", "realtime", "timeonice")])))
    for a, b in spans:
        for r in reports[(a, "summary")]:
            rows[(r["playerId"], r["gameId"])] = {
                "id": r["playerId"], "game": r["gameId"], "date": r["gameDate"], "team": r["teamAbbrev"],
                "name": r["skaterFullName"], "pos": r["positionCode"], "toi": (r.get("timeOnIcePerGame") or 0) / 60,
                **{k: r.get(k) or 0 for k in ("goals", "assists", "ppPoints", "shPoints", "shots")}}
        for r in reports[(a, "realtime")]:
            row = rows.get((r["playerId"], r["gameId"]))
            if row:
                row.update(hits=r.get("hits") or 0, blockedShots=r.get("blockedShots") or 0, iCF=r.get("totalShotAttempts") or 0)
        for r in reports[(a, "timeonice")]:
            row = rows.get((r["playerId"], r["gameId"]))
            if row:
                row["pptoi"] = (r.get("ppTimeOnIce") or 0) / 60
                row["shtoi"] = (r.get("shTimeOnIce") or 0) / 60
    for row in rows.values():
        for k in ("hits", "blockedShots", "iCF", "pptoi", "shtoi"):
            row.setdefault(k, 0)
        row["points"] = row["goals"] + row["assists"]
        row["fp"] = sum(row[k] * w for k, w in WEIGHTS.items())
    add_ranks(rows.values())
    return list(rows.values())


def _rank_min(vals):
    """Rank of each value, 1 = largest, ties share the smaller rank."""
    return [1 + sum(w > v for w in vals) for v in vals]


def add_ranks(rows):
    """Per (game, team): rank of his PP TOI among the team's skaters, of his TOI among its forwards or defensemen,
    PP1 (top 5 in PP TOI with over half a minute) and top line (top 3 F or top 2 D in TOI). Same as model/train.py."""
    teams, groups = {}, {}
    for r in rows:
        teams.setdefault((r["game"], r["team"]), []).append(r)
        groups.setdefault((r["game"], r["team"], pos_group(r["pos"])), []).append(r)
    for rs in teams.values():
        top = max(r["pptoi"] for r in rs)
        for r, k in zip(rs, _rank_min([r["pptoi"] for r in rs])):
            r["pp_share"] = r["pptoi"] / top if top > 0 else 0.0  # his PP time over the team's most that game
            r["pp_rank"] = k
            r["pp1"] = 1.0 if k <= 5 and r["pptoi"] > 0.5 else 0.0
    for (_, _, grp), rs in groups.items():
        for r, k in zip(rs, _rank_min([r["toi"] for r in rs])):
            r["toi_rank"] = k
            r["top_line"] = 1.0 if k <= (2 if grp == "D" else 3) else 0.0


def load_goalie_games(s, season, today):
    first = date(season // 10000, 9, 20)
    out = []
    for week in pmap(lambda ab: stats_report(s, "goalie/summary", season, *ab), weekly(first, history_end(today))):
        out += [{"id": r["playerId"], "game": r["gameId"], "date": r["gameDate"], "team": r["teamAbbrev"],
                 "name": r["goalieFullName"], "started": r.get("gamesStarted") or 0,
                 "ga": r.get("goalsAgainst") or 0, "toi": r.get("timeOnIce") or 0,
                 "sv": r.get("saves") or 0, "sa": r.get("shotsAgainst") or 0,
                 "wins": r.get("wins") or 0, "losses": r.get("losses") or 0, "otl": r.get("otLosses") or 0,
                 "fp": goalie_fp(r)}
                for r in week]
    return out


def load_prev_season(s, season):
    """Last season's per-game rates per skater, from season totals."""
    out = {}
    for r in stats_report(s, "skater/summary", season, teams=True):
        gp = r.get("gamesPlayed") or 0
        if gp:
            out[r["playerId"]] = {"gp": gp, "name": r["skaterFullName"], "pos": r["positionCode"],
                                  "team": (r.get("teamAbbrevs") or "").split(",")[-1].strip() or None,
                                  "toi": (r.get("timeOnIcePerGame") or 0) / 60,
                                  **{k: (r.get(k) or 0) for k in ("goals", "assists", "ppPoints", "shPoints", "shots")}}
    for r in stats_report(s, "skater/realtime", season):
        if r["playerId"] in out:
            out[r["playerId"]].update(hits=r.get("hits") or 0, blockedShots=r.get("blockedShots") or 0, iCF=r.get("totalShotAttempts") or 0)
    for r in stats_report(s, "skater/timeonice", season):
        if r["playerId"] in out:
            out[r["playerId"]]["pptoi_tot"] = (r.get("ppTimeOnIce") or 0) / 60
            out[r["playerId"]]["shtoi_tot"] = (r.get("shTimeOnIce") or 0) / 60
    rates = {}
    for pid, t in out.items():
        gp = t["gp"]
        fp = sum((t.get(k) or 0) * w for k, w in WEIGHTS.items())
        rates[pid] = {"gp": gp, "name": t["name"], "pos": t["pos"], "fp": fp / gp, "toi": t["toi"], "team": t.get("team"),
                      "pptoi": t.get("pptoi_tot", 0) / gp, "shtoi": t.get("shtoi_tot", 0) / gp, "points": (t["goals"] + t["assists"]) / gp,
                      **{k: (t.get(k) or 0) / gp for k in ("shots", "iCF", "hits", "blockedShots", "goals", "assists", "ppPoints")}}
    prev_ranks(rates, {pid: t.get("team") for pid, t in out.items()})
    return rates


def prev_ranks(rates, team_of):
    """Last season's usage ranks, for skaters with no game yet this season (findings 213): his PP TOI/G rank among his last
    team's regulars (12 F and 6 D with the most games) and his TOI/G rank among its forwards or defensemen, with pp1 and
    top_line from them as in add_ranks, and his PP TOI/G over the team's highest (pp_share). Without a last team they stay unset and the model fills the training mean."""
    by_team = {}
    for pid, r in rates.items():
        if team_of.get(pid):
            by_team.setdefault(team_of[pid], []).append(pid)
    for pids in by_team.values():
        regs = {g: sorted((p for p in pids if pos_group(rates[p]["pos"]) == g), key=lambda p: -rates[p]["gp"])[:n]
                for g, n in (("F", 12), ("D", 6))}
        pool = regs["F"] + regs["D"]
        for pid in pids:
            r, grp = rates[pid], pos_group(rates[pid]["pos"])
            r["pp_rank"] = 1 + sum(rates[q]["pptoi"] > r["pptoi"] for q in pool if q != pid)
            r["toi_rank"] = 1 + sum(rates[q]["toi"] > r["toi"] for q in regs[grp] if q != pid)
            r["pp1"] = 1.0 if r["pp_rank"] <= 5 and r["pptoi"] > 0.5 else 0.0
            r["top_line"] = 1.0 if r["toi_rank"] <= (2 if grp == "D" else 3) else 0.0
        top = max(rates[p]["pptoi"] for p in pids)  # his PP TOI over his team's top one, the last-game PP share's stand-in
        for pid in pids:
            rates[pid]["pp_share"] = rates[pid]["pptoi"] / top if top > 0 else 0.0


def goalie_fp(r):
    """League fantasy points of an NHL goalie/summary row: W 2, L -1, OTL +1, GA -1, SV 0.2, SO 3."""
    return (2 * (r.get("wins") or 0) - (r.get("losses") or 0) + (r.get("otLosses") or 0) - (r.get("goalsAgainst") or 0)
            + 0.2 * (r.get("saves") or 0) + 3 * (r.get("shutouts") or 0))


def load_prev_goalie_season(s, season):
    """Last season per goalie: (games started, fantasy points, saves, shots against). The points include relief appearances."""
    return {r["playerId"]: (r.get("gamesStarted") or 0, goalie_fp(r), r.get("saves") or 0, r.get("shotsAgainst") or 0)
            for r in stats_report(s, "goalie/summary", season)}


def shrunk_save_pcts(goalie_games, prev_goalies):
    """Each goalie's save% this season shrunk SAVE_SHRINK[0] shots toward last season's, itself shrunk SAVE_SHRINK[1] shots
    toward last season's league save% (findings 330 (1)). Returns ({goalie id: save%}, league save%)."""
    sv_all = sum(t[2] for t in prev_goalies.values())
    sa_all = sum(t[3] for t in prev_goalies.values())
    lg = sv_all / sa_all if sa_all else LEAGUE_SAVE_PCT
    prior = {gid: (t[2] + lg * SAVE_SHRINK[1]) / (t[3] + SAVE_SHRINK[1]) for gid, t in prev_goalies.items()}
    now = {}
    for g in goalie_games:
        t = now.setdefault(g["id"], [0, 0])
        t[0] += g.get("sv", 0); t[1] += g.get("sa", 0)
    out = {gid: (sv + prior.get(gid, lg) * SAVE_SHRINK[0]) / (sa + SAVE_SHRINK[0]) for gid, (sv, sa) in now.items()}
    return {**{gid: v for gid, v in prior.items() if gid not in out}, **out}, lg


def displaced_skaters(by_player, team_games, healthy):
    """When a regular (dressed 80%+ of his team's 5-20 games before the gap) has missed 3+ straight team games and
    ESPN now lists him healthy or day-to-day, the team's skater at his position (F or D) with the least ice time in
    the last game is the one who sits. healthy: NHL ids ESPN lists without a longer injury.
    Returns {nhl id of the displaced player: (cut to P(dresses), regular's name)}."""
    last = {}
    for pid, gl in by_player.items():
        tg = team_games.get(gl[-1]["team"], [])
        if tg:
            played = {g["game"] for g in gl}
            streak = next((i for i, g in enumerate(reversed(tg)) if g in played), len(tg))
            last[pid] = (gl[-1], tg, played, streak)
    out = {}
    for pid, (g, tg, played, streak) in last.items():
        before = tg[:len(tg) - streak][-20:]
        if streak < 3 or pid not in healthy or len(before) < 5 or sum(x in played for x in before) < 0.8 * len(before):
            continue
        grp = "D" if g["pos"] == "D" else "F"
        depth = [(h["toi"], hid) for hid, (h, htg, _, hs) in last.items()
                 if hs == 0 and h["team"] == g["team"] and h["game"] == tg[-1] and ("D" if h["pos"] == "D" else "F") == grp]
        if depth:
            out[min(depth)[1]] = (DISPLACED_CUT[grp], g["name"])
    return out


def lineup_context(by_player, team_games):
    """Per skater (findings 320 (1)): he missed his team's last game; how many same-position (F or D) teammates with 5+
    games and more TOI/G missed it; how many teammates with 5+ games and more PP TOI/G (over half a minute) missed it.
    The count is 0 for a skater with under 5 games. Same as model/train.py add_context."""
    info, runs = {}, {}
    for pid, gl in by_player.items():
        tg = team_games.get(gl[-1]["team"], [])
        info[pid] = (gl[-1]["team"], pos_group(gl[-1]["pos"]), len(gl), mean([g["toi"] for g in gl]),
                     mean([g["pptoi"] for g in gl]), float(bool(tg) and gl[-1]["game"] != tg[-1]))
        # his run of missed team games back from the last one (to 10), and back after missing the one before (findings 320 (5))
        mine, last10 = {g["game"] for g in gl if g["team"] == gl[-1]["team"]}, tg[-10:][::-1]
        runs[pid] = (float(next((j for j, x in enumerate(last10) if x in mine), len(last10))),
                     float(len(last10) >= 2 and last10[0] in mine and last10[1] not in mine))
    out_regs = {}
    for v in info.values():
        if v[2] >= 5 and v[5]:
            out_regs.setdefault(v[0], []).append(v)
    out = {}
    for pid, (team, grp, gp, toi, pp, missed) in info.items():
        mates = out_regs.get(team, []) if gp >= 5 else []
        out[pid] = {"missed_last": missed, "out_run": runs[pid][0], "back_last": runs[pid][1],
                    "out_above_toi": float(sum(v[1] == grp and v[3] > toi for v in mates)),
                    "out_above_pp": float(sum(v[4] > 0.5 and v[4] > pp for v in mates))}
    return out


def next_up_pp_d(by_player, team_games, out):
    """Per team, defensemen with 5+ games this season ranked by PP TOI/G: when the top one (1+ PP min/G) missed the team's
    last game or ESPN has him OUT, IR or suspended (out: NHL ids), the next healthy one who played that game gets more
    power-play time (findings 310 (5)). Returns {nhl id of the next-up D: top D's name}."""
    teams = {}
    for pid, gl in by_player.items():
        if len(gl) >= 5 and gl[-1]["pos"] == "D":
            teams.setdefault(gl[-1]["team"], []).append((sum(g["pptoi"] for g in gl) / len(gl), pid, gl))
    boost = {}
    for team, ds in teams.items():
        tg = team_games.get(team, [])
        if not tg:
            continue
        ds.sort(key=lambda d: -d[0])
        top_pp, top, top_gl = ds[0]
        if top_pp < NEXT_UP_PP_D_MIN or not (top in out or top_gl[-1]["game"] != tg[-1]):
            continue
        nxt = next((pid for _, pid, gl in ds[1:] if pid not in out and gl[-1]["game"] == tg[-1]), None)
        if nxt is not None:
            boost[nxt] = top_gl[-1]["name"]
    return boost


def load_birthdates(s, seasons):
    """NHL id: birth date, from the stats API's skater bios report for these seasons. Empty if it can't be read: the
    Season rating then goes without its age adjustment."""
    out = {}
    for season in seasons:
        try:
            rows = stats_report(s, "skater/bios", season)
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"NHL skater bios {season} unavailable: {e}", file=sys.stderr)
            continue
        for r in rows:
            try:
                out.setdefault(r["playerId"], date.fromisoformat(r["birthDate"]))
            except (KeyError, TypeError, ValueError):
                pass
    return out


def season_age_adj(birth, today):
    """Season FP/G adjustment for age: young skaters improve through the season, 30+ fade (findings 310 (3))."""
    if birth is None:
        return 0.0
    age = (today - birth).days / 365.25
    return AGE_YOUNG[1] * max(0.0, AGE_YOUNG[0] - age) - AGE_OLD[1] * max(0.0, age - AGE_OLD[0])


def load_xg(s, year):
    """MoneyPuck season file: xG per game (all situations) for the model, plus the season counts behind
    expected fantasy points (all situations, power play and short-handed). Empty if the file isn't there yet."""
    try:
        text = nhl_get(s, MONEYPUCK.format(year=year)).text
    except requests.RequestException as e:
        print(f"MoneyPuck {year} unavailable: {e}", file=sys.stderr)
        return {}
    lines = text.splitlines()
    if not lines or "situation" not in lines[0]:  # a 200 with an empty or odd body: rate without xG, not without ratings
        print(f"MoneyPuck {year} returned no table", file=sys.stderr)
        return {}
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
                    pre + "tm_goals": num(f, "OnIce_F_goals") - num(f, "I_F_goals"),
                    pre + "ogf": num(f, "OnIce_F_goals"), pre + "oxgf": num(f, "OnIce_F_xGoals")})
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


def week_schedule(s, monday, games=None, weeks=1, starts=None):
    """NHL abbrev -> sorted list of game dates (ISO) in the Monday-Sunday week(s) from monday (2 in a playoff round).
    games, if given, is filled with NHL abbrev -> [(date, opponent, home)]; starts with (NHL abbrev, date) -> the game's
    start time (UTC ISO), when the schedule gives one (findings 241)."""
    out = {}
    days = [d for k in range(weeks) for d in nhl_get(s, f"{NHL_WEB}/schedule/{(monday + timedelta(days=7 * k)).isoformat()}").json().get("gameWeek", [])]
    for day in days:
        for g in day.get("games", []):
            if g.get("gameType") == 2:
                for side, other in (("awayTeam", "homeTeam"), ("homeTeam", "awayTeam")):
                    out.setdefault(g[side]["abbrev"], []).append(day["date"])
                    if games is not None:
                        games.setdefault(g[side]["abbrev"], []).append((day["date"], g[other]["abbrev"], side == "homeTeam"))
                    if starts is not None and g.get("startTimeUTC"):
                        starts[(g[side]["abbrev"], day["date"])] = g["startTimeUTC"]
    return out


def team_strength(s, prev_season):
    """Goal differential per game this season, shrunk over 15 games toward half of last season's (findings section 70).
    Empty if the standings can't be read, and then no start is priced by schedule."""
    try:
        end = next(x["standingsEnd"] for x in nhl_get(s, f"{NHL_WEB}/standings-season").json()["seasons"] if x["id"] == prev_season)
        rows = lambda when: {r["teamAbbrev"]["default"]: r for r in nhl_get(s, f"{NHL_WEB}/standings/{when}").json().get("standings", [])}
        last = {t: (r["goalFor"] - r["goalAgainst"]) / max(r["gamesPlayed"], 1) for t, r in rows(end).items()}
        return {t: (r["goalFor"] - r["goalAgainst"] + SCHED_SHRINK * 0.5 * last.get(t, 0.0)) / (r["gamesPlayed"] + SCHED_SHRINK)
                for t, r in rows("now").items()}
    except (requests.RequestException, KeyError, StopIteration, ValueError) as e:
        print(f"Standings unavailable, goalie starts not priced by schedule: {e!r}", file=sys.stderr)
        return {}


def model_win_prob(gd_team, gd_opp, home):
    return 1 / (1 + math.exp(-(SCHED_WIN[0] + SCHED_WIN[1] * (gd_team - gd_opp) + SCHED_WIN[2] * home)))


ROSTER_WAITS = (0, 3, 10)  # seconds before each roster try: a timeout must not take a team's goalies to 0 (findings 230)


def roster(s, team, goalie_games=()):
    """Current NHL roster: [(id, full name, position code)]. Tried with backoff; if it still can't be read, the goalies
    who started for the team this season stand in (from goalie_games), so its goalies keep their Season rating."""
    for wait in ROSTER_WAITS:
        time.sleep(wait)
        try:
            r = s.get(f"{NHL_WEB}/roster/{team}/current", headers=UA, timeout=30)
            r.raise_for_status()
            data = r.json()
            return [(p["id"], f'{p["firstName"]["default"]} {p["lastName"]["default"]}', p.get("positionCode", "G" if grp == "goalies" else ""))
                    for grp in ("forwards", "defensemen", "goalies") for p in data.get(grp, [])]
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
            print(f"Roster {team} unavailable: {e!r}", file=sys.stderr)
    names = {}
    for g in goalie_games:
        if g["team"] == team and g["started"]:
            names[g["id"]] = g["name"]
    return [(gid, name, "G") for gid, name in names.items()]


class Model:
    def __init__(self):
        m = json.loads((ROOT / "model" / "rating_model.json").read_text())
        self.sk, self.dress_table, self.g = m["skater"], m["dress"], m["goalie"]
        self.dress_lr = m.get("dress_logit")
        sd = ROOT / "model" / "season_dress.json"
        self.season_lr = json.loads(sd.read_text()) if sd.exists() else None
        sr = ROOT / "model" / "season_range.json"
        self.season_range_table = json.loads(sr.read_text()) if sr.exists() else None

    def season_range(self, season, is_d, gp_last, gp_before, age=None):
        """80% range of the Season rating and his experience group (findings 345): rookies and second-year players finish
        further above their Season rating than veterans do. gp_last / gp_before: NHL games last season / two seasons ago; age in
        years (28+ is a veteran whatever his recent games: back from the AHL, Europe or injury)."""
        t = self.season_range_table
        if not t:
            return None, None
        exp = "rookie" if gp_last + gp_before < t["rookie_gp"] else "year 2" if gp_before < t["rookie_gp"] else "veteran"
        if age is not None and age >= t.get("veteran_age", 99):
            exp = "veteran"
        pos = "D" if is_d else "F"
        lo_cut, hi_cut = t["cuts"][pos]
        tier = "low" if season < lo_cut else "mid" if season < hi_cut else "top"
        lo, hi = t["cells"][f"{pos}|{tier}|{exp}"]
        return [round(max(season + lo, 0.0), 2), round(season + hi, 2)], exp

    def season_dress(self, x, shift=0.0):
        """Share of the team's remaining games he dresses (rest-of-season logistic, findings section 56)."""
        m = self.season_lr
        z = m["intercept"] + sum(c * x[k] for k, c in m["coef"].items()) + shift
        return 1 / (1 + math.exp(-z))

    def fpg(self, feats, season=False):
        """Points a game: the Week rating's ridge, or with season=True the Season rating's (no hinge curves, findings 320 (2);
        decomposed scoring forecasts, findings 320 (3))."""
        feats = {**feats, **forecast_features(feats, self.sk)}
        m = self.sk.get("season", self.sk) if season else self.sk
        if season and m.get("rates"):
            feats |= season_features(feats, m)
        total = m["intercept"]
        for name, c in zip(m["features"], m["coef"]):
            v = feats.get(name)
            total += c * (m["fill"][name] if v is None else v)
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


def skater_features(games, prev, xg_now, xg_prev, team_games, is_d=False, p2=None, context=None, age=None):
    """Inputs for one skater. games: his games this season, oldest first. team_games: his team's game ids, oldest first.
    p2: his per-game rates two seasons ago (findings 310 (4)), None if he didn't play then. context: his lineup_context
    entry (None before his first game). age: his age in years on the matchup Monday, None if unknown."""
    f = {"is_D": 1.0 if is_d else 0.0, "age_m": age}
    for k in ("missed_last", "out_above_toi", "out_above_pp", "out_run", "back_last"):
        f[k] = (context or {}).get(k) if games else None
    # last game's and last 3 games' role (findings 320 (1))
    for c in ("pp_share", "pp_rank", "toi_rank", "pp1", "top_line"):
        f[f"r1_{c}"] = games[-1].get(c) if games else None
        f[f"r3_{c}"] = mean([g.get(c) for g in games[-3:]]) if games else None
    w2 = min(p2["gp"] / 82, 1) if p2 else 0.0
    for r in P2_RATES + ["goals", "assists"]:
        f[f"p2_{r}"] = p2.get(r) if p2 else None
    f["p2_gp"], f["no_p2"] = (p2["gp"], 0.0) if p2 else (0, 1.0)
    for r in ("fp", "toi", "pptoi"):
        f[f"p2_{r}_w"] = (p2.get(r) or 0) * w2 if p2 else 0.0
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
    for r in RANK_RATES:  # before his first game: last season's ranks (findings 213), else None and the training mean
        for n, w in ((5, "l5"), (10, "l10"), (20, "l20"), (None, "std")):
            tail = games[-n:] if n else games
            f[f"{w}_{r}"] = mean([g.get(r) for g in tail]) if tail else (prev or {}).get(r)
    if f.get("std_ixg") is None and not gp:
        f["std_ixg"], f["std_onice_xgf"] = f["prev_ixg"], f["prev_onice_xgf"]
    # the ice-time forecast's inputs (findings 310 (1)): last game and last 3 games this season
    f["last_toi"], f["pp_l1"] = (games[-1]["toi"], games[-1]["pptoi"]) if games else (None, None)
    f["toi_l3"] = mean([g["toi"] for g in games[-3:]]) if games else None
    f["pp_l3"] = mean([g["pptoi"] for g in games[-3:]]) if games else None
    # the Season rating's decomposed inputs (findings 320 (3)): this season's and last season's totals with MoneyPuck's
    # on-ice goals and xG for (all situations, 5 on 4), and short-handed ice time
    for k, c in (("points", "points"), ("ppPoints", "ppPoints"), ("toi", "toi"), ("pptoi", "pptoi")):
        f[f"c_{k}"] = sum(g[c] for g in games)
        f[f"pv_{k}"] = prev[c] * prev["gp"] if prev and prev.get(c) is not None else None
    for k, c in (("a_ogf", "ogf"), ("a_oxgf", "oxgf"), ("pp_ogf", "pp_ogf"), ("pp_oxgf", "pp_oxgf")):
        f[f"c_{k}"] = (xg_now or {}).get(c)
        f[f"pv_{k}"] = (xg_prev or {}).get(c)
    if f["pv_a_oxgf"] is None or (f["pv_a_oxgf"] == 0 and (f["pv_toi"] or 0) > 100):  # no MoneyPuck season for him
        for k in ("a_ogf", "a_oxgf", "pp_ogf", "pp_oxgf"):
            f[f"pv_{k}"] = None
    sh = [g.get("shtoi", 0) for g in games]
    f["sh_l1"], f["sh_l5"], f["sh_l10"], f["sh_std"] = (sh[-1], mean(sh[-5:]), mean(sh[-10:]), mean(sh)) if sh else (None,) * 4
    f["sh_prev"] = prev.get("shtoi") if prev else None
    if not games and prev:  # no game yet this season: last season stands in for the last-game inputs too (findings 213, 291 (2))
        f["last_toi"] = f["toi_l3"] = prev.get("toi")
        f["pp_l1"] = f["pp_l3"] = prev.get("pptoi")
        for c in ("pp_rank", "toi_rank", "pp1", "top_line", "pp_share"):
            if prev.get(c) is not None:
                f[f"r1_{c}"] = f[f"r3_{c}"] = prev[c]
        if prev.get("shtoi") is not None:
            f["sh_l1"] = f["sh_l5"] = f["sh_l10"] = f["sh_std"] = prev["shtoi"]
    w = min(gp / 20, 1)
    for r in ("fp", "toi", "pptoi", "shots", "blockedShots", "hits"):
        v = f.get(f"std_{r}")
        f[f"std_{r}_w"] = None if v is None else v * w
    for n in D_X:
        f[f"D_x_{n}"] = f["is_D"] * (f.get(n) or 0)
    for n in SQUARED:
        f[f"{n}2"] = None if f.get(n) is None else f[n] ** 2
    played = {g["game"] for g in games}
    for n in (3, 10):
        last = team_games[-n:]
        f[f"dressed{n}"] = sum(g in played for g in last) / len(last) if last else None
    return f


SHORT_OPENING_GAMES, SHORT_OPENING_TOI = 3, 0.5  # 1-3 games, the latest under half of last season's TOI (findings 171)


def short_opening(games, prev):
    """His games this season without the ones cut short, when he has 1-3 games and the latest was under half of last
    season's average TOI (left hurt in the opening week); None otherwise."""
    if not games or len(games) > SHORT_OPENING_GAMES or not prev or not prev.get("toi"):
        return None
    lo = SHORT_OPENING_TOI * prev["toi"]
    return [g for g in games if g["toi"] >= lo] if games[-1]["toi"] < lo else None


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


def _num(x):
    return float(WORDNUM.get(x.lower(), 0) or x)


def timeline_days(text):
    """Stated time out in days ('out 4-6 weeks', 'miss at least two weeks'), as research/injury/notes.py reads it."""
    best = None
    for m in TIMELINE.finditer(text):
        pre = text[max(0, m.start() - 60):m.start()].lower()
        if not re.search(r"miss|out|sidelined|recover|timeline|week-to-week|shelved|absen|keep him|expected|estimated|least|another|surgery"
                         r"|away|require", pre):
            continue
        if TL_SKIP_PRE.search(pre) and not re.search(r"(?:out|miss) for the first\s*$", pre):
            continue
        lo = _num(m.group(1))
        hi = _num(m.group(2)) if m.group(2) else lo
        unit = m.group(3).lower()
        if unit == "day" and hi > 60 or unit == "game" and hi > 40 or unit == "week" and hi > 30:
            continue
        v = (lo + hi) / 2 * {"day": 1, "week": 7, "month": 30, "game": 2.2}[unit]
        best = v if best is None else max(best, v)
    return best


def injury_note(note, today):
    """What ESPN's latest injury note says (findings section 58): {long, season, days_left}. Status words come from the
    headline (the longer comment often recaps old injuries); the stated timeline from both. Out for the season also from a
    sentence of the longer comment that names him, since the headline may say only "LTIR" (Shesterkin, findings 216).
    Long-absence words also from long-comment sentences that name him or no other injured player, and a stated return
    month ('until November') counts as a return date (findings 236)."""
    short, long_ = note.get("short") or "", note.get("long") or ""
    last = (note.get("name") or "").split()[-1:]
    sentences = re.split(r"(?<=[.!?])\s+", long_)
    own = " ".join(x for x in sentences if last and last[0].lower() in x.lower())
    mine = " ".join(x for x in sentences if last and (last[0].lower() in x.lower() or not re.search(r"[A-Z][\w'.-]+ \([a-z -]+\)", x)))
    days = timeline_days(short + " " + long_)
    nd = note_date(note.get("date"))
    gone = (today - nd).days if nd else 0
    left = None if days is None else days - gone
    u = until_days(short + " " + own, nd or today)
    if u is not None:
        left = u - gone if left is None else max(left, u - gone)
    season = bool(SEASON_OUT.search(short) or SEASON_OUT.search(own))
    long_abs = season or bool(LONG_ABSENCE.search(short)) or bool(LONG_OWN.search(mine)) or (left is not None and left >= 14)
    return {"long": long_abs, "season": season, "days_left": left}


def note_date(text):
    """The ET calendar date of an ESPN note's timestamp (a note posted after 8 PM ET carries the next UTC date), a plain
    date as written, or None."""
    text = str(text or "")
    if len(text) <= 10:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    t = parse_utc(text)
    return t.astimezone(ET).date() if t else None


def until_days(text, day):
    """Days from day to the latest stated return month ('not expected back until November', 'until at least
    mid-December', 'before Christmas'), or None."""
    best = None
    for m in UNTIL.finditer(text):
        if m.group(0).lower() == "before christmas":
            d = date(day.year, 12, 25)
        else:
            mo = MONTHS[m.group(2).lower()]
            dom = {"early": 5, "mid": 15, "the middle of": 15, "late": 25, "the end of": 28}.get((m.group(1) or "").lower(), 1)
            d = date(day.year + (mo < day.month - 1), mo, dom)
            if (d - day).days > 200:  # 'until December' read in January is last December's stale note, not next year's
                d = d.replace(year=d.year - 1)
        v = (d - day).days
        best = v if best is None else max(best, v)
    return best


def parse_utc(text):
    """An ISO time ('2026-10-11T21:37Z', '...+00:00', or naive meaning UTC) as an aware datetime, or None."""
    try:
        t = datetime.fromisoformat(str(text or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def game_after(when, day, game_day, start):
    """A game on game_day (ET, ISO) came after a note at when (aware) whose ET date is day: a later date, or the same date
    with a start after the note (findings 241); with no start time known, the note came before ONE_GAME_SAME_DAY_HOUR ET."""
    if game_day > day:
        return True
    if game_day < day:
        return False
    st = parse_utc(start)
    return st > when if st else when.astimezone(ET).hour < ONE_GAME_SAME_DAY_HOUR


def one_game_out_over(note, team_game_dates, starts=None):
    """A one-game OUT note ('won't play Thursday') once his team has played a game after the note's time (findings 236).
    team_game_dates: his team's played game dates (ET, ISO). starts: {date: that game's start, UTC ISO} where known
    (findings 241): a game on the note's own day counts when it started after the note; with no start time, when the note
    came before ONE_GAME_SAME_DAY_HOUR ET."""
    short = (note or {}).get("short") or ""
    if not ONE_GAME_OUT.search(short) or ONE_GAME_NOT.search(short):
        return False
    when = parse_utc(note.get("date"))
    if when is None:
        return False
    day = when.astimezone(ET).date().isoformat()
    return any(game_after(when, day, d[:10], (starts or {}).get(d[:10])) for d in team_game_dates)


def load_injury_notes():
    """{ESPN id (str): {date, short, long, name, status, seen}}: each player's last real ESPN note (findings 236)."""
    try:
        data = json.loads(INJURY_NOTES.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_injury_notes(stored, today):
    """Save the notes seen in the last INJURY_NOTES_DAYS days."""
    cutoff = (today - timedelta(days=INJURY_NOTES_DAYS)).isoformat()
    kept = {k: v for k, v in sorted(stored.items()) if isinstance(v, dict) and str(v.get("seen") or v.get("date") or "")[:10] >= cutoff}
    try:
        INJURY_NOTES.write_text(json.dumps(kept, indent=1, sort_keys=True) + "\n")
    except OSError as e:
        print(f"Injury notes not saved: {e!r}", file=sys.stderr)


def carry_injury_notes(notes, stored, status, played_since, today):
    """Swap a bare status word ('ir', 'out') for the player's last real note, and remember each real note (findings 236).

    notes: {ESPN id: today's feed note}, changed in place. stored: load_injury_notes(), changed in place.
    status: {ESPN id: his ESPN fantasy status now}. played_since(ESPN id, note time): he dressed for an NHL game after the
    note was posted. The stored note is used when it wasn't written under a day-to-day status, his status now is at least
    as severe (day-to-day < out, suspension < IR), and he hasn't played since. Returns the ESPN ids whose note was swapped.
    A note is stored with his fantasy status, or the feed's own when he isn't in the data (findings 241 (5)), and a note
    still in use is marked seen today, so a season-out note shown as a bare 'ir' isn't pruned after 60 days."""
    swapped = set()
    for aid, note in list(notes.items()):
        if not PLACEHOLDER_NOTE.match(note.get("short") or ""):
            stored[str(aid)] = {"date": note.get("date"), "short": note.get("short"), "long": note.get("long"),
                                "name": note.get("name"), "status": status.get(aid) or FEED_STATUS.get(note.get("status")),
                                "seen": today.isoformat()}
            continue
        old = stored.get(str(aid))
        if not old or old.get("status") == "DAY_TO_DAY":
            continue
        if STATUS_SEVERITY.get(status.get(aid), -1) < STATUS_SEVERITY.get(old.get("status"), 9):
            continue
        if played_since(aid, str(old.get("date") or "")):
            continue
        notes[aid] = {**note, **{k: old.get(k) for k in ("date", "short", "long")}, "name": note.get("name") or old.get("name")}
        old["seen"] = today.isoformat()
        swapped.add(aid)
    return swapped


def usually_back(status, missed, note):
    """Display line for the why panel: typical games still to miss (medians in findings section 58)."""
    if note and note["season"]:
        return {"season": True}
    if note and note["days_left"] is not None and note["days_left"] > 0:
        return {"days": round(note["days_left"])}
    if note and note["long"]:
        return {"games": "16"}
    return {"INJURY_RESERVE": {"games": "10"}, "OUT": {"games": "4"},
            "DAY_TO_DAY": {"games": "3" if missed else "0-1"}}.get(status)


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


def season_dress_features(games, team, team_games, f, dress_f, prev, fpg, markov, played=None):
    """Inputs for the rest-of-season P(dresses) logistic: next week's lineup features plus last season's games, ice time,
    games left, and a lineup chain (drop-out rate since joining the team, league return curve by games missed).
    played: the team's games so far when team_games is cut short (a traded player yet to play for his new team)."""
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
    left = max(SEASON_GAMES - (len(team_games) if played is None else played), 0)
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


def context_gaps(f):
    """The last-game vs last-10 gaps the lineup context scales (findings 320 (1)); None when a factor is missing."""
    def prod(a, b, c):
        v = [f.get(a), f.get(b), f.get(c)]
        return None if None in v else (v[0] if a == "missed_last" else min(v[0], 3)) * (v[1] - v[2])
    return {"ml_gap": prod("missed_last", "l10_toi", "last_toi"), "ml_ppgap": prod("missed_last", "l10_pptoi", "pp_l1"),
            "oa_gap": prod("out_above_toi", "last_toi", "l10_toi"), "oap_gap": prod("out_above_pp", "pp_l1", "l10_pptoi")}


def week_extras(f):
    """The Week rating's finishing-luck, last-game role, age and return-from-absence inputs (findings 320 (4), (5); same as
    model/train.py week_extras): goals below / above individual xG per game (this season x min(GP/20, 1), and last
    season), the shortfall x shots, the last-game ice-time jump either way, a top-line / PP1 spot in the last game he
    rarely had before (None when a factor is missing: the ridge fills it with its training mean); max(0, 24 - age),
    max(0, age - 29); and missed the team's last game, his missed run, back after a miss, each also x FP/G (0 if missing)."""
    g = lambda k: f.get(k)
    sub = lambda a, b: None if g(a) is None or g(b) is None else g(a) - g(b)
    pos = lambda v: None if v is None else max(v, 0.0)
    neg = lambda v: None if v is None else max(-v, 0.0)
    gw = min((g("std_gp") or 0) / 20, 1)
    lk, lkp = sub("std_goals", "std_ixg"), sub("prev_goals", "prev_ixg")
    out = {"luck_neg": None if lk is None else neg(lk) * gw, "luck_pos": None if lk is None else pos(lk) * gw,
           "luck_negp": neg(lkp), "luck_posp": pos(lkp)}
    out["luck_sh"] = None if out["luck_neg"] is None or g("std_shots") is None else out["luck_neg"] * g("std_shots")
    out["j1_up"], out["j1_dn"] = pos(sub("last_toi", "l10_toi")), neg(sub("last_toi", "l10_toi"))
    new = lambda a, b: None if g(a) is None or g(b) is None else g(a) * (1 - g(b))
    out["new_top"], out["new_pp1"] = new("r1_top_line", "std_top_line"), new("r1_pp1", "std_pp1")
    age = 27.0 if g("age_m") is None else g("age_m")  # age bends (findings 320 (5)): unknown reads 27, no bend
    out["ag_y"], out["ag_o"] = max(0.0, 24 - age), max(0.0, age - 29)
    ml, orun, bk = g("missed_last") or 0.0, min(g("out_run") or 0.0, 10), g("back_last") or 0.0
    fp, sx = g("std_fp") or 0.0, g("sx_fp") or 0.0
    out |= {"r_ml": ml, "r_orun": orun, "r_ml_fp": ml * fp, "r_ml_sx": ml * sx, "r_orun_fp": orun * fp, "r_back": bk,
            "r_back_fp": bk * fp}
    return out


def forecast_features(f, sk):
    """Ridge inputs built on the others (same arithmetic as model/train.py): next week's TOI and PP TOI forecast, the
    usage x efficiency inputs with the forecasts in place of last-5 ice time (findings 310 (1)), and the products of the
    12 strongest inputs, each missing factor filled with its training mean (findings 310 (2))."""
    out = week_extras(f)
    fc = sk.get("toi_forecast")
    if fc:
        x = dict(f, gpw=min((f.get("std_gp") or 0) / 20, 1)) | context_gaps(f)
        val = {tgt: m["intercept"] + sum(c * (m["fill"][k] if x.get(k) is None else x[k]) for k, c in zip(m["inputs"], m["coef"]))
               for tgt, m in fc.items()}
        u = ux_features(dict(f, l5_toi=val["y_toi"], l5_pptoi=max(val["y_pptoi"], 0.0)), "D" if f.get("is_D") else "F", sk.get("ux_means"))
        out |= {f"fc_{k}": v for k, v in u.items()} | {"fc_toi": val["y_toi"], "fc_pptoi": val["y_pptoi"]}
    for c, knots in (sk.get("hinges") or {}).items():  # Week rating's bend points (findings 320 (2)); missing x: filled
        v = out.get(c, f.get(c))
        for i, q in enumerate(knots):
            out[f"h_{c}_{i}"] = None if v is None else max(v - q, 0.0)
    pairs = sk.get("pairs")
    if pairs:
        fill = pairs["fill"]
        v = {k: fill[k] if f.get(k) is None else f[k] for k in pairs["inputs"]}
        for i, a in enumerate(pairs["inputs"]):
            for b in pairs["inputs"][i + 1:]:
                out[f"{a}*{b}"] = v[a] * v[b]
    return out


IPP_K, IPP_EV_MIN, IPP_PP_MIN = 20.0, 300.0, 60.0  # shrinkage of IPP and on-ice xGF per minute (findings 320 (3))
RATE_WIN = ["l5", "l10", "l20", "std", "prev", "p2"]


def season_features(f, m):
    """The Season rating's decomposed inputs (same arithmetic as model/train.py decomposed): per-minute rate forecasts
    alone and x the TOI forecast; IPP x on-ice xGF per minute x forecast EV and PP minutes; a short-handed TOI forecast
    and blocks / hits x forecast PK and EV minutes. f carries the forecasts (fc_toi, fc_pptoi)."""
    num = lambda k: f.get(k) or 0.0
    lin = lambda mm, x: mm["intercept"] + sum(c * (mm["fill"][k] if x.get(k) is None else x[k]) for k, c in zip(mm["inputs"], mm["coef"]))
    flags = {"no_prev": f.get("no_prev"), "is_D": f.get("is_D"), "gpw": min(num("std_gp") / 20, 1), "no_p2": f.get("no_p2")}
    out = {}
    for c, mm in m["rates"].items():
        x = dict(flags)
        for w in RATE_WIN:
            v, t = f.get(f"{w}_{c}"), f.get(f"{w}_toi")
            k = f"{w}_{c}_pm"
            x[k] = None if v is None or t is None or t <= 3 else min(max(v / t, mm["lo"][k]), mm["hi"][k])
        r = lin(mm, x)
        out[f"rf_{c}"], out[f"rr_{c}"] = r * f["fc_toi"], r
    has_c, has_p = f.get("c_a_ogf") is not None, f.get("pv_a_ogf") is not None
    t = lambda c: (num(f"c_{c}") if has_c else 0.0) + 0.5 * (num(f"pv_{c}") if has_p else 0.0)
    mu = m["ipp_mu"]["D" if f.get("is_D") else "F"]
    ipp_ev = (t("points") - t("ppPoints") + IPP_K * mu["ipp_ev"]) / (t("a_ogf") - t("pp_ogf") + IPP_K)
    ipp_pp = (t("ppPoints") + IPP_K * mu["ipp_pp"]) / (t("pp_ogf") + IPP_K)
    xev = (t("a_oxgf") - t("pp_oxgf") + IPP_EV_MIN * mu["xev"]) / (t("toi") - t("pptoi") + IPP_EV_MIN)
    xpp = (t("pp_oxgf") + IPP_PP_MIN * mu["xpp"]) / (t("pptoi") + IPP_PP_MIN)
    pp = max(f["fc_pptoi"], 0.0)
    out |= {"ipp_ev": ipp_ev, "ipp_pp": ipp_pp, "xev": xev, "xpp": xpp,
            "xpts_ev": ipp_ev * xev * max(f["fc_toi"] - pp, 0.0), "xpts_pp": ipp_pp * xpp * pp}
    out["xpts"] = out["xpts_ev"] + out["xpts_pp"]
    sh = max(lin(m["sh_forecast"], dict(f, gpw=flags["gpw"])), 0.0)
    ev = max(f["fc_toi"] - pp - sh, 0.0)
    tot = num("std_toi") * num("std_gp") + num("prev_toi") * num("prev_gp") * 0.5
    rate = lambda c, prior: (num(f"std_{c}") * num("std_gp") + num(f"prev_{c}") * num("prev_gp") * 0.5 + 300 * prior) / (tot + 300)
    blk, hit = rate("blockedShots", 0.06), rate("hits", 0.08)
    out |= {"fc_sh": sh, "fc_ev": ev, "blk_x_sh": blk * sh, "blk_x_ev": blk * ev, "hit_x_ev": hit * ev}
    return out


def strict_rotation(seq, cands, n=ROTATION_STARTS):
    """The team's last n+ starts strictly alternate between two goalies who are both still here (A, B, A, B, ...):
    coaches running such a rotation keep it going (findings 227)."""
    last = seq[-n:]
    return len(last) == n and len(set(last)) == 2 and set(last) <= set(cands) and all(a != b for a, b in zip(last, last[1:]))


def repeating_pattern(seq, cands):
    """(period, the pattern keeps the last starter in) when the last starts repeat a period-3 or period-4 pattern
    between two goalies who are both still here over 2+ full cycles (A A B A A B, A A B B A A B B), not a strict A/B
    rotation; the longest repeat wins. None otherwise (findings 243)."""
    best = None
    for per in (3, 4):
        n = 0
        while n < len(seq) - per and seq[-1 - n] == seq[-1 - n - per]:
            n += 1
        tail = seq[-(n + per):]
        if n + per >= 2 * per and len(set(tail)) == 2 and set(tail) <= set(cands) \
                and not all(a != b for a, b in zip(tail, tail[1:])) and (best is None or n + per > best[2]):
            best = (per, seq[-per] == seq[-1], n + per)
    return best[:2] if best else None


def goalie_starts(model, team, goalies, starts_by_team, prev_starts, game_dates, today, confirmed=None, today_p=None, season_share=None,
                  injury=None, last_start=None, movers=(), relief=(), season_out=(), by_date=None, save_pcts=None, pulled=()):
    """Expected starts in the rest of this week for each goalie on the team.

    starts_by_team: this season's starter per team game, oldest first, as (date, goalie id).
    confirmed: {date: goalie id} of Daily Faceoff confirmed starters; those games count as 1 for him, 0 for the others.
    today_p: if given, filled with each goalie's chance of starting today's game.
    season_share: if given, filled with each goalie's long-run share of team starts (this season's starts
    blended with last season's split over SEASON_SHARE_K games), for the Season rating.
    injury: {goalie id: ESPN injury factor}; scales his chance before each game is split among the team's goalies,
    so an injured starter's games go to his partner (findings section 48).
    last_start: (goalie id, (shift next game, shift game after next, ...)) for whoever started the team's last game:
    his start logit drops after a bad start or a loss and rises after a win (findings 131-133); after he left without
    a decision it stays down for every game in the window, until a Daily Faceoff confirmation names him (findings 164).
    movers: goalies who started for another NHL team this season; with 1-3 starts for this team their logit rises,
    and their season_share counts only this team's games since their first start for it.
    relief: goalies who came on in relief in the team's last game; their start logit rises (findings 170).
    season_out: goalies ESPN says are out for the season: no season_share, and theirs goes to the others (findings 216, 234).
    by_date: if given, filled with {goalie id: {date: chance he starts}} for each game from today on (findings 236).
    pulled: goalies pulled from their latest start; their season_share drops PULLED_SEASON unless the no-decision cut
    already applies (findings 330 (3))."""
    confirmed, injury = confirmed or {}, injury or {}
    hist = starts_by_team.get(team, [])
    seq = [gid for _, gid in hist]
    last_date = hist[-1][0] if hist else None
    # the roster, not the recent starters: a goalie traded or waived away kept ~40% of his old team's next start
    # (findings 160); recent starters only stand in when the roster is missing
    cands = (set(goalies) if goalies else set(seq[-20:]) | set(relief)) | set(confirmed.values())
    if not cands:
        return {}
    # early season: pull each share toward his share of the team's starts last season, fading out by the team's
    # 10th game, so one opening-night start doesn't read as a 100/0 split (findings section 41)
    k = EARLY_SHARE_K * max(0.0, 1 - len(seq) / EARLY_SHARE_GAMES)
    last_total = sum(prev_starts.get(gid, 0) for gid in cands)
    prior = {gid: prev_starts.get(gid, 0) / last_total if last_total else 1 / len(cands) for gid in cands}
    # a no. 1 whose old partner left isn't given a prior near 1: the top prior is capped and the excess goes to the
    # others in proportion to theirs, evenly if they had none (findings 217, 234)
    top_gid = max(sorted(cands, key=str), key=prior.get)
    if len(cands) >= 2 and prior[top_gid] > PRIOR_TOP_CAP:
        extra, others = prior[top_gid] - PRIOR_TOP_CAP, [g for g in cands if g != top_gid]
        rest = sum(prior[g] for g in others)
        prior[top_gid] = PRIOR_TOP_CAP
        for g in others:
            prior[g] += extra * (prior[g] / rest if rest else 1 / len(others))
    if season_share is not None:
        for gid in cands:
            # a goalie who came from another team counts only the games since his first start here (findings 157)
            sq = seq[seq.index(gid):] if gid in movers and gid in seq else seq
            # a quarter recent form (half-life 5 team games) so a new no. 1 shows sooner (findings section 76)
            w = [0.5 ** ((len(sq) - 1 - i) / 5) for i in range(len(sq))]
            k0 = 10 * 0.5 ** (len(sq) / 5)
            ewm = (sum(wi for wi, x in zip(w, sq) if x == gid) + k0 * prior[gid]) / (sum(w) + k0)
            season_share[gid] = SEASON_SHARE_SCALE * (0.75 * (sum(x == gid for x in sq) + SEASON_SHARE_K * prior[gid]) / (len(sq) + SEASON_SHARE_K)
                                                      + 0.25 * ewm)
        if save_pcts:
            sv, lg = save_pcts
            for gid in cands:
                others = {x: seq.count(x) for x in set(seq) if x != gid}  # no other starter yet: no gap
                gap = (sv.get(gid, lg) - sum(sv.get(x, lg) * n for x, n in others.items()) / sum(others.values())) * 100 if others else 0.0
                season_share[gid] = min(max(season_share[gid] + SAVE_GAP_SEASON[0] + SAVE_GAP_SEASON[1] * gap, 0.0), 1.0)
        # out for the season, or left his last start without a decision and not yet confirmed back in net (findings 167),
        # unless ESPN already has him out: the share he loses goes to the team's other goalies, a short-term injured one
        # (out, IR) at half weight, since he is back for most of the rest (findings 216-217, 234)
        moved, takers = 0.0, [gid for gid in cands if gid not in season_out]
        for gid in cands:
            cut = 1.0 if gid in season_out else (1 - NO_DECISION_SEASON) if (
                last_start and last_start[0] == gid and last_start[1][:1] == (NO_DECISION_LOGIT,) and gid not in confirmed.values()
                and injury.get(gid, 1.0) > 0) else 0.0
            if not cut and gid in pulled:
                season_share[gid] = max(season_share[gid] - PULLED_SEASON, 0.0)
            if cut:
                moved += season_share[gid] * cut
                season_share[gid] *= 1 - cut
                takers = [g for g in takers if g != gid]
        wt = {g: season_share[g] * (INJURED_TAKER_WEIGHT if injury.get(g, 1.0) <= 0 else 1.0) for g in takers}
        tot = sum(wt.values())
        for g in takers:
            season_share[g] += moved * wt[g] / tot if tot else 0.0
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
    ahead = 0  # unplayed team games so far: 0 is the next one
    shift_gid, shifts = last_start or (None, ())
    new_team = {gid for gid in cands if gid in movers and 1 <= seq.count(gid) <= NEW_TEAM_STARTS}
    run = next((i for i, x in enumerate(reversed(seq)) if x != seq[-1]), len(seq)) if seq else 0
    on_streak = STREAK_RANGE[0] <= run <= STREAK_RANGE[1]
    rotating = strict_rotation(seq, cands)
    pattern = PATTERN_LOGITS.get(repeating_pattern(seq, cands), 0.0) if seq and not rotating else 0.0
    other_cut = last_six_cut(seq)
    idle3 = three_goalie_idle(seq)
    plain = bool(seq) and not rotating and not pattern and not other_cut and not idle3
    prev_d = None
    recent20 = [g for g in seq[-20:] if g in cands]
    no1 = max(set(recent20), key=lambda g: (recent20.count(g), g == seq[-1])) if recent20 else None
    dates_sorted = sorted(game_dates)
    after_rest = False
    relieved = {gid: RELIEF_RARE_LOGIT if seq[-20:].count(gid) <= RELIEF_RARE_STARTS else 0.0 for gid in relief if gid in cands}
    for d in sorted(game_dates):
        if last_date is not None and d <= last_date:  # already played: its real starter is in the history
            continue
        day = date.fromisoformat(d)
        # a team-wide break rests everyone: the last starter isn't benched just because days passed (findings 196)
        gap = (day - date.fromisoformat(last_date)).days if ahead == 0 and last_date else 0
        rested = next((v for days, v in BREAK_LOGITS if gap >= days), 0.0)
        night2_no1 = False
        night_after = prev_d is not None and (day - prev_d).days == 1  # the night after the previous game still to play
        if gap == 1 and seq:  # night 2 of a back-to-back: shift whoever started night 1, by role
            # the no. 1 among the goalies still here: a departed no. 1's starts don't count (findings 234)
            recent = [g for g in seq[-20:] if g in cands]
            top = max((recent.count(g) for g in set(recent)), default=0)
            night2_no1 = bool(recent) and recent.count(seq[-1]) == top
            rested = B2B_NIGHT2_NO1 if night2_no1 else B2B_NIGHT2_OTHER
            after_rest = seq[-1] == no1  # the game after night 2 goes back to him (handoff 223)
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
            z = (shifts[ahead] if gid == shift_gid and ahead < len(shifts) else 0.0) + (NEW_TEAM_START_LOGIT if gid in new_team else 0.0) \
                + (rested if rested and gid == seq[-1] else 0.0) \
                + (STREAK_LOGIT if on_streak and ahead == 0 and gid == seq[-1] else 0.0) \
                + (ROTATION_LOGIT if rotating and ahead == 0 and gid == seq[-1] else 0.0) \
                + (pattern if pattern and ahead == 0 and gid == seq[-1] else 0.0) \
                + (other_cut if other_cut and ahead == 0 and gid != seq[-1] and gap != 1 else 0.0) \
                + (ROTATION_LATER_LOGIT if rotating and ahead == 1 and gid == seq[-2] and not night_after else 0.0)
            if after_rest and ahead == 1 and gid != no1:
                z += AFTER_NO1_REST_OTHER_LOGIT
            if gid == no1:  # back-to-backs still ahead, and his ordinary nights (findings 231)
                nxt = next((x for x in dates_sorted if x > d), None)
                prv = [x for x in dates_sorted if x < d and (last_date is None or x > last_date)]
                n1 = nxt is not None and (date.fromisoformat(nxt) - day).days == 1
                n2 = bool(prv) and (day - date.fromisoformat(prv[-1])).days == 1
                if n1 and ahead == 0:
                    z += B2B_AHEAD_NIGHT1
                if n2:
                    z += B2B_AHEAD_NIGHT2_NEXT if ahead == 1 else B2B_AHEAD_NIGHT2_LATER
                if not n1 and not n2 and not (ahead == 0 and gap == 1):
                    z += NO1_ORDINARY_LOGIT
            if ahead == 0 and gid in idle3:
                z += THREE_GOALIE_IDLE_LOGIT
            if seq and gid == seq[-1] and (rotating and ahead == 2 or plain and ahead == 1):
                z += ROTATION_THIRD_LOGIT if rotating else PLAIN_SECOND_LOGIT
            if gid in relieved:
                z += RELIEF_LOGITS[min(ahead, 1)] + relieved[gid]
            if z and 0 < raw[gid] < 1:
                raw[gid] = 1 / (1 + math.exp(-(math.log(raw[gid] / (1 - raw[gid])) + z)))
            raw[gid] *= injury.get(gid, 1.0)
        tot = sum(raw.values()) or 1
        p = {gid: v / tot for gid, v in raw.items()}
        early = B2B_NIGHT2_NO1_EARLY if night2_no1 and len(seq) <= EARLY_GAMES else 0.0
        if early and 0 < p.get(seq[-1], 0) < 1:
            was = p[seq[-1]]
            now = 1 / (1 + math.exp(-(math.log(was / (1 - was)) + early)))
            p = {gid: now if gid == seq[-1] else v * (1 - now) / (1 - was) for gid, v in p.items()}
        ahead += 1
        prev_d = day
        q = p
        if ahead > 1:  # games after the next one are softened and split across the team's goalies again; the chain keeps p
            q = {gid: v if v <= 0 or v >= 1 else 1 / (1 + math.exp(-LATER_GAME_TEMP * math.log(v / (1 - v)))) for gid, v in p.items()}
            tot = sum(q.values()) or 1
            q = {gid: v / tot for gid, v in q.items()}
        if d in confirmed:
            p = q = {gid: float(gid == confirmed[d]) for gid in cands}
            if confirmed[d] == shift_gid:  # confirmed back in net: his last result no longer says anything
                shifts = ()
        if d >= today.isoformat():
            for gid in cands:
                exp[gid] += q[gid]
                if d == today.isoformat() and today_p is not None:
                    today_p[gid] = q[gid]
                if by_date is not None:
                    by_date.setdefault(gid, {})[d] = q[gid]
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


def recently_flagged(today):
    """{ESPN id: latest date flagged} for any ESPN status in the injury log within the last CLEARED_DAYS days."""
    cutoff = (today - timedelta(days=CLEARED_DAYS)).isoformat()
    out = {}
    if INJURY_LOG.exists():
        for line in INJURY_LOG.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("date", "") >= cutoff and row.get("espn_id") is not None:
                out[row["espn_id"]] = max(out.get(row["espn_id"], ""), row["date"][:10])
    return out


def load_news():
    path = ROOT / "data" / "news.json"
    if not path.exists():
        return {}
    items = json.loads(path.read_text()).get("players", {})
    return dict(list(items.items())[:NEWS_CAP])


def utcnow():
    return datetime.now(timezone.utc)


def game_started(p):
    """His team's game today has already started (fetch.py's today_start, UTC): it isn't left to play (findings 178)."""
    try:
        return bool(p.get("today_start")) and datetime.fromisoformat(p["today_start"].replace("Z", "+00:00")) <= utcnow()
    except ValueError:
        return False


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


def add_ratings(players, espn_season, today, monday, starters=(), odds=None, injuries=None, weeks=1, reschedule=None):
    """Add Claude Rating fields to each player dict from fetch.py (in place).

    starters: Daily Faceoff starting-goalie pages (leads.fetch_starters) for today and later this week.
    odds: today's betting lines (leads.fetch_odds); they price tonight's goalie starts (research section 30).
    injuries: ESPN's injury notes (leads.fetch_injuries); a long absence takes a skater's week to 0 (findings section 58).
    reschedule(p, nhl team): fetch.py's, gives a player ESPN lists as 'FA' his NHL team's games (findings 230)."""
    notes = {i["athlete_id"]: i for i in injuries or [] if i.get("athlete_id")}
    flagged = recently_flagged(today)  # read before this run rewrites the log
    stored_notes = load_injury_notes()
    s = requests.Session()
    if hasattr(s, "mount"):  # enough pooled connections for the loads below running side by side
        s.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=32))
    season, prev_season = season_ids(espn_season)
    model = Model()
    week_games, week_starts = {}, {}
    # the feeds don't depend on each other: load them side by side (each still loads as it did one by one)
    games, goalie_games, prev, prev2, prev_goalies, xg_now, xg_prev, births, sched, strength = pmap(lambda f: f(), [
        lambda: load_skater_games(s, season, today),
        lambda: load_goalie_games(s, season, today),
        lambda: load_prev_season(s, prev_season),
        lambda: load_prev_season(s, prev_season - 10001),  # two seasons ago, a ridge input (findings 310 (4))
        lambda: load_prev_goalie_season(s, prev_season),
        lambda: load_xg(s, season // 10000),
        lambda: load_xg(s, prev_season // 10000),
        lambda: load_birthdates(s, (season, prev_season)),
        lambda: week_schedule(s, monday, week_games, weeks, week_starts),
        lambda: team_strength(s, prev_season),
    ], workers=4)
    prev_gs = {gid: t[0] for gid, t in prev_goalies.items()}
    save_pcts = shrunk_save_pcts(goalie_games, prev_goalies)
    news = load_news()

    # team game order this season (every game has goalie rows) and the starter of each
    team_games, starts_by_team, last_start = {}, {}, {}
    for g in sorted(goalie_games, key=lambda g: g["date"]):
        if g["game"] not in team_games.setdefault(g["team"], []):
            team_games[g["team"]].append(g["game"])
        if g["started"]:
            starts_by_team.setdefault(g["team"], []).append((g["date"], g["id"]))
            last_start[g["team"]] = (g["id"], last_start_shift(g))  # the latest start wins
    team_dates = {}  # each team's played game dates (ET)
    for g in goalie_games:
        team_dates.setdefault(g["team"], set()).add(g["date"][:10])
    # came on in relief in his team's latest game (findings 170)
    relief = {}
    for g in goalie_games:
        if not g["started"] and g["toi"] > 0 and team_games.get(g["team"]) and g["game"] == team_games[g["team"]][-1]:
            relief.setdefault(g["team"], set()).add(g["id"])
    start_teams = {}
    for g in goalie_games:
        if g["started"]:
            start_teams.setdefault(g["id"], set()).add(g["team"])
    movers = {gid for gid, teams in start_teams.items() if len(teams) > 1}  # started for two NHL teams this season
    latest = {}  # each goalie's own latest start (findings 330 (3))
    for g in sorted(goalie_games, key=lambda g: g["date"]):
        if g["started"]:
            latest[g["id"]] = g
    pulled = {gid for gid, g in latest.items() if g["toi"] < PULLED_TOI}
    scratches = load_scratches(s, {tg[-1] for tg in team_games.values() if tg})
    tried = {tg[-1] for tg in team_games.values() if tg}

    def scratch_list(gid):  # older games' lists, read only when a player just back needs them (findings 168)
        if gid not in tried:
            tried.add(gid)
            scratches.update(load_scratches(s, {gid}))
        return scratches.get(gid)
    # each goalie's points per start this season, shrunk over 15 starts (findings sections 27 and 60)
    base = model.g["points_per_start"]
    own = {}
    for g in goalie_games:
        if g["started"]:
            t = own.setdefault(g["id"], [0.0, 0])
            t[0] += g["fp"]; t[1] += 1
    # the prior is his own last-season pts/start, itself shrunk to the league mean over 15 starts (findings section 60)
    prior = {gid: (fp + base * PPS_PRIOR) / (n + PPS_PRIOR) for gid, (n, fp, *_) in prev_goalies.items() if n}
    pps = {gid: (fp + prior.get(gid, base) * PPS_PRIOR) / (n + PPS_PRIOR) for gid, (fp, n) in own.items()}
    # the league's points per start this season, for this week's pricing (findings 231 (8))
    league_pps = (sum(fp for fp, _ in own.values()) + base * LEAGUE_PPS_PRIOR) / (sum(n for _, n in own.values()) + LEAGUE_PPS_PRIOR)
    by_player = {}
    for g in sorted(games, key=lambda g: g["date"]):
        by_player.setdefault(g["id"], []).append(g)
    context = lineup_context(by_player, team_games)  # who missed each team's last game (findings 320 (1))
    # the older scratch lists that players just back ask scratch_list for, read side by side now rather than one by one in
    # the loop below (about 30 calls, 8 s; speed audit, Oct 12 2026): each skater's window on his latest team, a superset
    want = set()
    for gl in by_player.values():
        if team_games.get(gl[-1]["team"]):
            comeback_logit(team_games[gl[-1]["team"]], {g["game"] for g in gl}, want.add, gl[-1]["id"])
    want -= tried
    tried |= want
    scratches.update(load_scratches(s, want))

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
    # every team with a game this week or this season: one with no game this week keeps its goalies' Season (findings 230)
    teams = sorted(set(sched) | set(team_games))
    for team, r in zip(teams, pmap(lambda t: roster(s, t, goalie_games), teams)):
        rosters[team] = r
        for pid, name, pos in rosters[team]:
            known = index.get(norm(name), {})
            if pid not in known or known[pid][0] is None:
                add(pid, name, team, pos)
    on_roster = {pid for r in rosters.values() for pid, _, _ in r}
    # missed all or most of last season (Barkov): the season before instead of a newcomer's prior, for players on a current
    # NHL roster or with games this season, so retired players don't come back (findings 216)
    missed = {pid for pid in on_roster | set(by_player) if (prev.get(pid) or {}).get("gp", 0) < PREV_MIN_GP}
    prev3 = {}
    if missed:
        older = prev2
        label = f"{(prev_season - 10001) // 10000}-{str((prev_season - 10001) % 100).zfill(2)}"  # 20242025 -> 2024-25 (findings 234)
        prev.update({pid: dict(older[pid], season_label=label) for pid in missed if (older.get(pid) or {}).get("gp", 0) >= PREV_MIN_GP})
        # a player whose "last season" is the one before (missed last season): his two-seasons-ago input is three seasons back,
        # as in training (findings 291 (4))
        prev3 = load_prev_season(s, prev_season - 20002) if any(v.get("season_label") for v in prev.values()) else {}
        # ... and his MoneyPuck inputs come from that same season, so goals and xG describe one season as in training
        swapped = [pid for pid in missed if (prev.get(pid) or {}).get("season_label")]
        if swapped:
            xg_older = load_xg(s, prev_season // 10000 - 1)
            for pid in swapped:
                xg_prev[pid] = xg_older.get(pid)

    def name_match(p):
        team = ESPN_TO_NHL.get(p["team"], p["team"])
        cands = index.get(norm(p["name"]), {}) or nickname_cands(index, p["name"], team)
        if not cands:
            return None
        grp = pos_group(p["pos"])
        same = [pid for pid, (t, _) in cands.items() if t == team]
        if len(same) > 1:  # teammates with one name (Vancouver's two Elias Petterssons): the position decides (findings 112)
            same = [pid for pid in same if pos_group(cands[pid][1]) == grp]
        if len(same) == 1:
            return same[0]
        pos_ok = [pid for pid, (_, pos) in cands.items() if pos_group(pos) == grp] \
            or [pid for pid, (_, pos) in cands.items() if (pos == "G") == (grp == "G")]  # a forward ESPN lists at D, or the reverse
        return pos_ok[0] if len(pos_ok) == 1 else None

    # an unsigned ESPN free agent with a rostered player's name (the other Matt Murray) isn't that player (findings 241 (4))
    claimed = {name_match(q) for q in players if q.get("team") != "FA"} - {None}

    def match(p):
        pid = name_match(p)
        return None if p.get("team") == "FA" and pid in claimed else pid

    # Daily Faceoff confirmed starters (research section 7: worth about a third of a streamed goalie's points)
    confirmed, tonight = {}, {}
    for page in starters:
        for g in page.get("games", []):
            cands = [pid for pid, (t, pos) in index.get(norm(g.get("goalie") or ""), {}).items() if pos == "G" and t == g.get("team")] \
                or [pid for pid, (t, pos) in nickname_cands(index, g.get("goalie"), g.get("team")).items() if pos == "G"]  # "Matthew" Murray (findings 175)
            if not page.get("date") or page["date"] < today.isoformat():
                continue
            if not cands and g.get("status") == "Confirmed" and g.get("goalie"):
                # a confirmed starter not in the NHL data yet (a new call-up): the team's other goalies sit (findings 217)
                confirmed.setdefault(g["team"], {})[page["date"]] = f"unmatched:{g['goalie']}"
                if page["date"] == today.isoformat():
                    tonight[g["team"]] = (f"unmatched:{g['goalie']}", "Confirmed")
            if len(cands) != 1:
                continue
            if page["date"] == today.isoformat():
                tonight[g["team"]] = (cands[0], g.get("status"))
            if g.get("status") == "Confirmed":
                confirmed.setdefault(g["team"], {})[page["date"]] = cands[0]
    # ESPN lists him as a free agent ('FA') but he has played this season and an NHL roster has him: his team's games
    # (Bunting, findings 230)
    roster_team = {pid: team for team, r in rosters.items() for pid, _, _ in r}
    for p in players:
        if reschedule and p["pos"] != "G" and p.get("team") == "FA":
            pid = match(p)
            if pid in by_player and roster_team.get(pid):
                reschedule(p, roster_team[pid])
    # a bare status word ('ir') in place of ESPN's real note: the last real note, if it still applies (findings 236)
    pid_of = {p["id"]: match(p) for p in players}
    goalie_dates = {}
    for g in goalie_games:
        goalie_dates.setdefault(g["id"], []).append((g["date"][:10], g["team"]))

    def played_since(aid, when):
        """He dressed for a game after the note at when: a later ET date, or that date with a start after the note
        (findings 241 (5): a note posted during a game he played isn't thrown away)."""
        pid, t = pid_of.get(aid), parse_utc(when)
        if not (pid and t):
            return False
        day = t.astimezone(ET).date().isoformat()
        return any(game_after(t, day, g["date"][:10], week_starts.get((g["team"], g["date"][:10])))
                   for g in by_player.get(pid, [])) \
            or any(game_after(t, day, d, week_starts.get((tm, d))) for d, tm in goalie_dates.get(pid, []))
    carried = carry_injury_notes(notes, stored_notes, {p["id"]: p.get("injury") for p in players}, played_since, today)
    if carried:
        print(f"Kept the last real injury note for {len(carried)} players whose note is now a bare status word")
    goalie_inj, goalie_gone = {}, set()
    for p in players:
        if p["pos"] == "G" and p.get("injury") in INJURY_DRESS:
            pid = match(p)
            if pid:
                goalie_inj[pid] = INJURY_DRESS[p["injury"]]
                if p["id"] in notes and injury_note(notes[p["id"]], today)["season"]:
                    goalie_gone.add(pid)
    goalie_exp, goalie_today, goalie_season, goalie_nights = {}, {}, {}, {}
    lines = line_win_probs(odds, today)
    # every team, not just those with a game this week: no game this week keeps the Season rating (findings 230)
    for team in sorted(set(sched) | set(team_games)):
        dates = sched.get(team, [])
        roster_g = [pid for pid, _, pos in rosters.get(team, []) if pos == "G"]
        tod, sea, nights = {}, {}, {}
        exp = goalie_starts(model, team, roster_g, starts_by_team, prev_gs, dates, today, confirmed.get(team), tod, sea, goalie_inj,
                            last_start.get(team), movers, relief.get(team, ()), goalie_gone, nights, save_pcts, pulled)
        # a goalie gets his numbers from the team whose roster lists him, not whichever team ran last (findings 160)
        mine = set(roster_g) | set((confirmed.get(team) or {}).values())
        # (decided once: deciding per output let the first one claim the goalie and dropped his Season, findings 230)
        kept = {gid for gid in set(exp) | set(sea) if (gid in mine if roster_g else gid not in goalie_exp)}
        for src, dst in ((nights, goalie_nights), (exp, goalie_exp), (tod, goalie_today), (sea, goalie_season)):
            dst.update({gid: v for gid, v in src.items() if gid in kept})

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
                   "share10": round(sum(x == pid for x in seq) / len(seq), 2) if seq and pid else None,
                   "n10": len(seq)}  # the team may have played fewer than 10 (findings 232)
            if team in tonight and pid:
                who, status = tonight[team]
                why["tonight"] = {"starting": who == pid, "status": status or "Unconfirmed"}
            # tonight's game already started: its start is no longer ahead (findings 178)
            started = game_started(p)
            if started:
                starts = max(0.0, starts - goalie_today.get(pid, 0.0)) if pid else 0.0
            # on waivers he misses his team's games before he clears (findings 122): keep that share of the starts
            team_left = len([d for d in sched.get(team, []) if d > today.isoformat() or (d == today.isoformat() and not started)])
            if p.get("owner") == "waivers" and team_left > p["games_left"]:
                starts *= p["games_left"] / team_left
            # dropped after today's first puck: his team keeps him only until ESPN's overnight rollover (findings 241 (3))
            if p.get("pending_drop"):
                starts = min(starts, goalie_today.get(pid, 0.0) if pid and p["games_left"] else 0.0)
            if n.get("apply") and n.get("starts") is not None:
                starts = max(0.0, min(float(n["starts"]), p["games_left"]))
            fpg = pps.get(pid, prior.get(pid, base)) if pid else base
            why["own_pps"] = round(fpg, 2)  # his own, before tonight's line and the schedule (findings 232)
            raw_season = goalie_season.get(pid, 0.0) * fpg if pid else 0.0
            season = GOALIE_SEASON_SCALE[0] + GOALIE_SEASON_SCALE[1] * raw_season if raw_season > 0 else 0.0
            # start chance per game from today, for the page's open-slot count (findings 236)
            p["cr_g_nights"] = {d: round(q, 2) for d, q in sorted(goalie_nights.get(pid, {}).items())
                                if not p.get("pending_drop") or d <= today.isoformat()} if pid else {}
            # this week's starts: 0.4 of his gap to the league rate, then the line or the schedule (findings 231 (8))
            fpg = league_pps + WEEK_PPS_KEEP * (fpg - league_pps)
            # tonight's game priced by the betting line: that + 4.1 x (win probability - 0.5) per start
            p_today = min(goalie_today.get(pid, 0.0), starts) if pid and not started else 0.0
            if team in lines and p_today > 0 and starts > 0:
                line_pps = fpg + LINE_PPS_SLOPE * (lines[team] - 0.5)
                why["line"] = {"win_prob": round(lines[team], 2), "pps": round(line_pps, 2)}
                fpg = ((starts - p_today) * fpg + p_today * line_pps) / starts
            # the rest of the week's starts, with no line yet, priced by team strength and home ice (findings section 70)
            lined = team in lines and p_today > 0
            rest = [(d, opp, home) for d, opp, home in week_games.get(team, [])
                    if d > today.isoformat() or (d == today.isoformat() and not lined and not started)]
            if strength and team in strength and rest and starts > 0:
                bonus = sum(WEEK_SCHED_SLOPE * (model_win_prob(strength[team], strength.get(opp, 0.0), home) - 0.5) for _, opp, home in rest) / len(rest)
                fpg += (starts - (p_today if lined else 0.0)) * bonus / starts
                why["sched"] = round(bonus, 2)
            why["gs"] = own.get(pid, [0, 0])[1] if pid else None
            # ESPN's games-started count can lag a start its points already include (findings 182): the ESPN columns
            # (This yr/G, Proj/G, ESPN week) use NHL starts, with fetch.py's blend toward ESPN's projection
            if pid in own and own[pid][1]:
                gfp, gs = own[pid]
                p["gp"], p["cur_ppg"] = gs, round(gfp / gs, 2)
                prior_ppg = p.get("proj_ppg") if p.get("proj_ppg") is not None else p.get("last_ppg")
                w = min(gs, 20) / 20 * 0.6
                p["ppg"] = round(p["cur_ppg"] * w + prior_ppg * (1 - w), 2) if prior_ppg is not None else p["cur_ppg"]
            why["p_today"] = round(goalie_today.get(pid, 0.0), 2) if pid and not started else 0.0  # chance he starts tonight (stream card)
            # ... and before his game started: the page's open-G count keeps using it once he's in net (findings 213)
            why["p_today_pre"] = round(goalie_today.get(pid, 0.0), 2) if pid else 0.0
            why["exp_starts"] = round(starts, 2)  # after tonight's start, waivers and news (findings 182)
            exp_games = starts  # ESPN injury already applied in goalie_starts, where his starts go to his partner
            p_dress = None
        else:
            gl = by_player.get(pid, []) if pid else []
            espn_team = ESPN_TO_NHL.get(p["team"], p["team"])
            team = gl[-1]["team"] if gl else espn_team
            tg = team_games.get(team, [])
            # traded or claimed but yet to play for the new team (ESPN already lists it): his old team's games after his
            # last one aren't misses, so stop its schedule there; the new-team shift applies from game 0 (findings 159)
            traded = bool(gl) and espn_team != team and espn_team in team_games and gl[-1]["game"] in tg
            if traded:
                tg = tg[:tg.index(gl[-1]["game"]) + 1]
            # his "last season" is already the one before (missed last season): the season before that stands in (findings 291 (4))
            p2 = prev3.get(pid) if (prev.get(pid) or {}).get("season_label") else prev2.get(pid)
            age = (monday - births[pid]).days / 365.25 if pid in births else None
            f = skater_features(gl, prev.get(pid), xg_now.get(pid), xg_prev.get(pid), tg, is_d=p["pos"] == "D", p2=p2,
                                  context=context.get(pid), age=age)
            f.update(ux_features(f, "D" if p["pos"] == "D" else "F", model.sk.get("ux_means")))
            fpg = model.fpg(f) if pid else model.sk["replacement_fpg"]["D" if p["pos"] == "D" else "F"]
            fpg_s = model.fpg(f, season=True) if pid else fpg  # the Season rating's ridge (findings 320 (2))
            # opening games cut short: average with his points a game leaving those games out (findings 171)
            kept = short_opening(gl, prev.get(pid)) if pid else None
            if kept is not None:
                f2 = skater_features(kept, prev.get(pid), xg_now.get(pid) if kept else None, xg_prev.get(pid), tg,
                                     is_d=p["pos"] == "D", p2=p2, context=context.get(pid), age=age)
                f2.update(ux_features(f2, "D" if p["pos"] == "D" else "F", model.sk.get("ux_means")))
                fpg = (fpg + model.fpg(f2)) / 2
                fpg_s = (fpg_s + model.fpg(f2, season=True)) / 2
            # with a new team since last season, and with one team this season: rated ~3.5% high (findings 230)
            last_team = (prev.get(pid) or {}).get("team") if pid else None
            if last_team and team in rosters and last_team != team and not traded and len({g["team"] for g in gl}) <= 1:
                fpg *= OFFSEASON_MOVER_MULT
                fpg_s *= OFFSEASON_MOVER_MULT
            df_, base_dress, cap = None, None, 1.0
            if pid is None or (not gl and p["team"] == "FA"):  # not in NHL stats, or unsigned and yet to play (findings 216)
                p_dress = NOT_PLAYING_DRESS
            elif f["dressed3"] is None:  # team hasn't played yet
                p_dress = 0.9 if prev.get(pid) else 0.5
            else:
                df_ = dress_features(gl, team, tg, f, today) if model.dress_lr else None
                p_dress = base_dress = model.dress_logit(df_) if df_ else model.dress(f["dressed3"], f["dressed10"])
                last = gl[-1]["date"] if gl else None
                idle = None if last is None else (today - date.fromisoformat(last)).days
                if len(tg) >= 3 and (idle is None or idle > 14):
                    cap = NOT_PLAYING_DRESS if idle is None else idle_cap(idle)
                    p_dress = min(p_dress, cap)
            # missed his team's most recent game: the mid-week swap card's trigger (findings section 51)
            sat_last = bool(pid and tg and tg[-1] not in {g["game"] for g in gl})
            # ESPN dropped his status within the week and he hasn't played since: a suspension served, an injury healed
            # flagged, and no game since: one who played through the flag and then sat isn't coming back (findings 183)
            # flagged the day of his last game counts only if he left that game early, hurt in it (findings 213)
            cleared = bool(pid) and not p.get("injury") and p["id"] in flagged and (not gl or sat_last) \
                and (not gl or gl[-1]["date"][:10] < flagged[p["id"]]
                     or (gl[-1]["date"][:10] == flagged[p["id"]] and bool(left_early_shifts(gl[-1]["toi"], f.get("std_toi")))))
            cleared_new = cleared and not gl and bool(tg)  # yet to play this season (findings 172)
            cleared_out = cleared and bool(gl)  # in season, still out (findings 173)
            if cleared_new:
                base_dress, cap, p_dress = None, 1.0, CLEARED_NEW_DRESS
            elif cleared_out and base_dress is not None:
                cap = 1.0
                p_dress = dress_by_game(base_dress, 1, False, 1.0, CLEARED_LOGIT)[0]
            # sat it as a healthy scratch: on that game's scratch list; if the list can't be read, no ESPN injury status
            scratch = sat_last and not p.get("injury") and (pid in scratches[tg[-1]] if tg[-1] in scratches else True)
            comeback = 0.0 if sat_last or traded or not gl else comeback_logit(tg, {g["game"] for g in gl}, scratch_list, pid)
            # just traded or claimed: 1-3 games for his current team after games for another team this season
            run = next((k for k, g in enumerate(reversed(gl)) if g["team"] != gl[-1]["team"]), None) if gl else None
            new_team = traded or (not sat_last and run is not None and 1 <= run <= NEW_TEAM_GAMES)
            # dressed for the last game but barely played it, and ESPN hasn't flagged him yet (findings 165)
            early = () if sat_last or traded or not gl or p.get("injury") else left_early_shifts(gl[-1]["toi"], f.get("std_toi"))
            here = sum(g["team"] == gl[-1]["team"] for g in gl) if gl else 0
            new_team_season = 0 < here < len(gl) and here <= NEW_TEAM_SEASON_GAMES
            # Season rating: rest-of-season share of games he dresses (findings section 56), before ESPN's injury cap and news
            # season_share: a fixed share for a player yet to play, which the details name (findings 232)
            season_share = None
            if df_ and model.season_lr:
                season = fpg_s * model.season_dress(season_dress_features(gl, team, tg, f, df_, prev.get(pid), fpg_s, model.season_lr["markov"],
                                                                         played=len(team_games.get(team, []))),
                                                 NEW_TEAM_SEASON_LOGIT * new_team_season + LEFT_EARLY_SEASON_LOGIT * bool(early)
                                                 + CLEARED_SEASON_LOGIT * cleared_out)
            elif cleared_new:
                season_share = CLEARED_NEW_SEASON
            elif not gl and pid and p.get("injury") in ("DAY_TO_DAY", "OUT", "INJURY_RESERVE", "SUSPENSION"):
                # hurt or suspended before playing this season: such regulars dress for ~41% of the rest, not the idle cap's
                # 10% (findings 74; suspensions take the short-injury share, findings 118)
                pre = injury_note(notes[p["id"]], today) if p["id"] in notes else None
                season_share = PRESEASON_INJURED["season" if pre and pre["season"] else "long" if pre and pre["long"] else "short"]
            elif not gl and pid in on_roster and tg and not p.get("injury") and (prev.get(pid) or {}).get("gp", 0) >= UNPLAYED_REGULAR_GP:
                # a regular (60+ games last season, on a current NHL roster, so not retired or overseas) who hasn't played
                # while his team has: such players dress for 0.38 of the rest (findings 118)
                season_share = UNPLAYED_REGULAR
            else:
                season = fpg_s * p_dress
            if season_share is not None:
                season = fpg_s * season_share
            age_adj = season_age_adj(births.get(pid), today) if pid else 0.0
            if fpg_s > 0:
                season *= max(fpg_s + age_adj, 0.0) / fpg_s
            if n.get("apply"):
                if n.get("dress") is not None:
                    p_dress = max(0.0, min(float(n["dress"]), 1.0))
                    base_dress = None  # the news call replaces the lineup model
                if n.get("fpg_mult") is not None:
                    fpg *= max(0.8, min(float(n["fpg_mult"]), 1.2))
            note = injury_note(notes[p["id"]], today) if p.get("injury") and p["id"] in notes else None
            inj = 0.0 if note and note["long"] else SKATER_INJURY_CAP.get(p.get("injury") or "", 1.0)
            # OUT for one named game that his team has since played: day-to-day, as ESPN has him by the next morning (findings 236)
            one_game_over = p.get("injury") == "OUT" and p["id"] in notes and not (note and note["long"]) \
                and one_game_out_over(notes[p["id"]], team_dates.get(team, ()),
                                      {d: st for (tm, d), st in week_starts.items() if tm == team})
            if one_game_over:
                inj = SKATER_INJURY_CAP["DAY_TO_DAY"]
            if p.get("injury") in ("DAY_TO_DAY", "OUT") and gl and not sat_last and not (note and note["long"]) and f.get("std_toi"):
                share = gl[-1]["toi"] / f["std_toi"]
                if p["injury"] == "DAY_TO_DAY":
                    inj = next(c for top, c in DTD_PLAYED_CAPS if share < top)
                elif share < DTD_PLAYED_CAPS[0][0]:
                    inj = min(inj, OUT_LEFT_EARLY_CAP)
            if p.get("injury") in SKATER_INJURY_CAP:
                injury_log.append({"date": today.isoformat(), "espn_id": p["id"], "nhl_id": pid, "name": p["name"],
                                   "status": p["injury"], "long_note": bool(note and note["long"]),
                                   "p_dress_table": round(p_dress, 3), "p_dress": round(min(p_dress, inj), 3)})
            p_dress = min(p_dress, inj)
            gn = p["games_left"]
            # sat the last game, but Daily Faceoff's projected lines have him: 14 of 14 dressed next time (findings 174)
            in_lines = sat_last and not p.get("injury") and p.get("dfo_out") is False
            per_game = dress_by_game(base_dress, gn, sat_last and not in_lines, min(cap, inj),
                                     SCRATCH_LOGIT * scratch + NEW_TEAM_LOGIT * new_team + comeback + CLEARED_LOGIT * cleared_out, early) \
                if base_dress is not None else [CLEARED_NEW_NEXT] + [p_dress] * (gn - 1) if cleared_new and gn else [p_dress] * gn
            dress_next = per_game[0] if per_game else CLEARED_NEW_NEXT if cleared_new else p_dress
            if in_lines and per_game:  # no game left: nothing to lift (findings 182)
                dress_next = max(dress_next, DFO_LINEUP_NEXT)
                per_game = [dress_next] + per_game[1:]
            # ESPN has him OUT on the day of his team's game, still to start: that game counts ~0 (findings 235)
            if p.get("injury") == "OUT" and not one_game_over and per_game and p.get("today_start") and not game_started(p):
                per_game[0] = min(per_game[0], OUT_TODAY_CAP)
                dress_next = per_game[0]
            exp_games = sum(per_game)
            if gn:
                p_dress = exp_games / gn  # the week's average, shown as "to dress"
            why = {"toi5": f.get("l5_toi"), "toi": f.get("std_toi") if gl else None, "pp5": f.get("l5_pptoi"),
                   "sog": f.get("std_shots"), "blk": f.get("std_blockedShots"), "gp": len(gl),
                   "last_fpg": prev[pid]["fp"] if pid in prev else None, "xfp": expected_fp(xg_now.get(pid), xg_prev.get(pid), p["pos"] == "D") if pid else None}
            why = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in why.items()}
            if (prev.get(pid) or {}).get("season_label"):  # last season's line is from the season before (findings 234)
                why["last_season"] = prev[pid]["season_label"]
            why["season_fpg"] = round(fpg_s, 2)  # the Season rating's points a game, its own ridge (findings 320 (2))
            if season_share is not None:
                why["season_share"] = round(season_share, 2)
            if abs(age_adj) >= 0.03:
                why["age_adj"] = round(age_adj, 2)
            gp_last = 0 if (prev.get(pid) or {}).get("season_label") else (prev.get(pid) or {}).get("gp", 0)
            rng, exp = model.season_range(season, p["pos"] == "D", gp_last, (prev2.get(pid) or {}).get("gp", 0),
                                          (today - births[pid]).days / 365.25 if pid in births else None) if pid else (None, None)
            if rng:
                why["season_range"] = rng
                if exp != "veteran":
                    why["exp"] = exp
            if p.get("injury") in SKATER_INJURY_CAP and p["injury"] != "SUSPENSION":
                why["back"] = usually_back(p["injury"], sat_last, note)
        p["cr"] = round(fpg * exp_games, 2)
        p["cr_fpg"] = round(fpg, 2)
        p["cr_season"] = round(season, 2)
        p["cr_games"] = round(exp_games, 2)
        p["cr_dress"] = None if p_dress is None else round(p_dress, 2)
        p["cr_dress_next"] = None if p_dress is None else round(dress_next, 2)  # his team's next game: tonight cards
        p["sat_last"] = sat_last
        p["cr_matched"] = pid is not None
        p["nhl_id"] = pid
        p["cr_why"] = why
        if n:
            p["cr_news"] = {k: n.get(k) for k in ("flag", "source", "quote", "apply") if n.get(k) is not None}

    # a regular is back: the least-used skater at his position sits more than the dress model sees (findings section 63)
    by_nhl = {p["nhl_id"]: p for p in players if p.get("nhl_id") and p["pos"] != "G"}
    healthy = {pid for pid, p in by_nhl.items() if p.get("injury") in (None, "", "DAY_TO_DAY")}
    for pid, (cut, regular) in displaced_skaters(by_player, team_games, healthy).items():
        p = by_nhl.get(pid)
        if p and p["cr_dress"]:
            dress = max(0.0, p["cr_dress"] - cut)
            p["cr_games"] = round(p["games_left"] * dress, 2)
            p["cr"] = round(p["cr_fpg"] * p["cr_games"], 2)
            p["cr_dress"] = round(dress, 2)
            p["cr_dress_next"] = round(max(0.0, p["cr_dress_next"] - cut), 2)
            p["cr_why"]["displaced"] = regular

    # the top power-play D is out: the next one up gets his PP time this week, not in the Season number (findings 310 (5))
    out = {p["nhl_id"] for p in players if p.get("nhl_id") and p.get("injury") in ("OUT", "INJURY_RESERVE", "SUSPENSION")}
    for pid, top in next_up_pp_d(by_player, team_games, out).items():
        p = by_nhl.get(pid)
        if p and p["cr_matched"]:
            p["cr_fpg"] = round(p["cr_fpg"] + NEXT_UP_PP_D_FPG, 2)
            p["cr"] = round(p["cr_fpg"] * p["cr_games"], 2)
            p["cr_why"]["pp_d_out"] = top

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
    write_injury_notes(stored_notes, today)
    print(f"Claude Rating: {len(players)} players, {unmatched} not matched to NHL data, {len(news)} news entries")
    return unmatched
