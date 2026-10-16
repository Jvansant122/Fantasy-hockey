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

import season_boost
import winners as winners_model

ROOT = Path(__file__).resolve().parent.parent
NHL_STATS = "https://api.nhle.com/stats/rest/en"
NHL_WEB = "https://api-web.nhle.com/v1"
MONEYPUCK = "https://moneypuck.com/moneypuck/playerData/seasonSummary/{year}/regular/skaters.csv"
MONEYPUCK_TEAMS = "https://moneypuck.com/moneypuck/playerData/seasonSummary/{year}/regular/teams.csv"
UA = {"User-Agent": "Mozilla/5.0 (fantasy-hockey-lookup)"}
WEIGHTS = {"goals": 2, "assists": 1, "ppPoints": 0.5, "shPoints": 0.5, "shots": 0.1, "hits": 0.1, "blockedShots": 0.5}
GAME_RATES = ["fp", "toi", "pptoi", "shots", "iCF", "hits", "blockedShots", "goals", "assists", "ppPoints", "points"]
# where he ranks in his team-game's PP time and ice time, this season's windows only (findings section 201)
RANK_RATES = ["pp_rank", "toi_rank", "pp1", "top_line"]
P2_RATES = ["fp", "toi", "pptoi", "shots", "hits", "blockedShots", "points"]  # two seasons ago (model/train.py P2_RATES)
# a skater with no game yet: the Week's this-season windows from both seasons (handoff 429, idea 1187), and FP/G moves
# EARLY_WEEK_ESPN of the way to ESPN's preseason projection at 0 GP / 1-3 GP (early-Monday Spearman +0.003 / +0.019 / +0.005
# in 2023-26, free agents +0.001 / +0.012 / +0.008, MSE lower every season; hunt13/early_wk)
TWO_SEASON_RATES = P2_RATES + ["goals", "assists"]
EARLY_WEEK_ESPN = ((0, 0.25), (3, 0.19))
# opponents' last-season goals against per game (shrunk OPP_LAST_SHRINK games to the league, as a fraction above it), faded
# by OPP_LAST_K / (OPP_LAST_K + the opponent's games this season), x his Week FP/G (handoff 504, findings 402: Week Spearman
# +0.0007 weeks 2-8 in every test season)
OPP_LAST_C, OPP_LAST_K, OPP_LAST_SHRINK = 0.45, 5, 10
# position x usage and squared ice time inputs (findings section 201); same as model/train.py
D_X = ["std_toi", "std_pptoi", "l5_toi", "l5_pptoi", "std_blockedShots", "std_hits"]
SQUARED = ["std_toi", "l5_toi", "std_pptoi"]
# ESPN status caps the chance to dress (research findings section 15); recheck day-to-day after ~3 weeks of logs
INJURY_DRESS = {"OUT": 0.0, "INJURY_RESERVE": 0.0, "SUSPENSION": 0.0, "DAY_TO_DAY": 0.5}  # goalies (inside goalie_starts)
# skaters (findings section 58): injured players still dress for some of the week, unless the note says it's a long absence
SKATER_INJURY_CAP = {"OUT": 0.35, "INJURY_RESERVE": 0.15, "SUSPENSION": 0.0, "DAY_TO_DAY": 0.5}
# IR, game today / tomorrow / 2+ days after the run: dressed 0.05 / 0.07 / 0.19, players come off IR during the week
# (hunt14/dress2, findings 476, handoff 629); the week-level cap is the 2+ day one
IR_DAY_CAPS = (0.05, 0.15, 0.25)
# a goalie with Daily Faceoff injury news since his last appearance (at most 14 days old, no return or full-practice
# words): his start odds are multiplied by 0.2 for the team's next game and 0.35 later, min with ESPN's factor (handoff 679, findings 503)
GOALIE_DFO_INJ_CAPS, GOALIE_DFO_INJ_DAYS = (0.2, 0.35), 14
# ESPN-listed with an illness note at most 3 days old: OUT dressed 0.77 / 0.74 vs the 0.35 cap (hunt14/phrases, findings 502)
ILLNESS_WORDS = re.compile(r"illness|\bill\b|\bsick|\bflu\b|virus", re.I)
ILLNESS_CAP, ILLNESS_DAYS = 0.75, 3
# ESPN return words about him ("will play", "expected to return", "was activated", "practiced"), no illness word: the cap
# lifts to 0.5 and each game gains 0.5 logit (handoff 677, findings 502 (3))
RETURN_WORDS = re.compile(
    r"expected to (?:return|play|suit up|dress|be (?:in the lineup|available|good to go|ready to go))"
    r"|will (?:play|suit up|dress|draw (?:back )?into|be in the lineup|be available|return to (?:the lineup|action|game action|play))"
    r"|(?:set|slated|poised|on track|cleared|in line) to (?:return|play|suit up|dress|make his return)|back in the lineup|good to go"
    r"|(?:participated|took part) in [\w' -]{0,25}(?:practice|skate|session)|\bpracticed\b|full[- ]contact|full practice|returned to practice"
    r"|(?:was|been|has been|got) (?:activated|reinstated|removed from injured reserve)", re.I)
RETURN_NEG = re.compile(r"(?:\bnot|n't|unclear|uncertain|whether|\bif|before|until|once|unless|when|whenever|need to|can be|eligible to"
                        r"|no longer|hop(?:e|es|ing)|think|doubt)\W+(?:\w+\W+){0,4}$", re.I)
RETURN_CAP, RETURN_LOGIT = 0.5, 0.5
# a suspension counts only for its length (handoff 631, findings 476 (3)): N from ESPN's note, the team's games since the
# listing served; no stated length: game j since the listing capped at 0.85 x the share of 25 spells (2021-26) shorter than j
SUSP_LEN = re.compile(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)[- ]game suspension|suspended (?:for )?"
                      r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten) (?:regular[- ]season |more )?games?", re.I)
SUSP_UNKNOWN_CAPS = (0.0, 0.17, 0.44, 0.61, 0.68, 0.82)
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
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
# a long ESPN note that is only week-to-week, only a timeline, or only LTIR: per-game caps (game today or tomorrow, game 2+
# days after the run) instead of 0. Week-to-week / timeline-only long notes dressed 0.04-0.21, LTIR 0.01-0.13
# (hunt14/returns, findings 489; handoff 657)
LONG_NOTE_CAPS = {"wtw": (0.05, 0.20), "timeline": (0.10, 0.30), "ltir": (0.05, 0.05)}
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
# no NHL game yet this season, no ESPN status, and Daily Faceoff's latest roster move for him a call-up in the last week:
# 657 such call-ups 2021-26 played 0.31 of their team's games the next week (0.18-0.47 by season; handoff 502)
RECALL_NEW_DRESS, RECALL_NEW_DAYS = 0.25, 7
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
# trades and claims (findings 269-272): a newcomer at his position cuts the least-used skater like a returning regular until
# the newcomer has played ARRIVAL_GAMES for the team; a departed D lifts the team's healthy D out 1-4 games by DEPARTED_D_LIFT
ARRIVAL_GAMES, DEPARTED_D_LIFT, DEPARTED_D_GAMES, DEPARTED_D_OUT = 3, 0.10, 3, (1, 4)
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
# night 1 (every one still ahead this week, findings 277), night 2 of that pair, night 2 of a pair further ahead, and every other game except the
# shipped night-2 case above. 2021-26 replay of 12,960 team-game mornings: Brier -0.6%, no. 1 week-sum error -0.7%, better
# in 5 of 5 seasons (findings 231, handoffs 133 and 190)
B2B_AHEAD_NIGHT1, B2B_AHEAD_NIGHT2_NEXT, B2B_AHEAD_NIGHT2_LATER, NO1_ORDINARY_LOGIT = -0.4, -1.2, -0.6, 0.15
# ... at half size in an even tandem (top two goalies within one start of the team's last 20; findings 402, handoff 451)
EVEN_TANDEM_B2B_SCALE = 0.5
# early season (team under EARLY_SHARE_GAMES games): a roster goalie with no start for the team yet, every game this week
# (third goalies read 0.21 against 0.07 actual, night 2 0.33 against 0.15; 15 seasons, findings 402, handoff 450)
EARLY_NO_START_LOGIT = -0.75
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
# the no. 1 on a team where 3+ goalies started the last 10, every game after the next (handoff 626): his week starts were
# 1.315 against 1.454 actual on such teams, 2021-26
THREE_GOALIE_NO1_LOGIT = 0.5
# the no. 1 on any night that isn't night 2 of a back-to-back: rested no. 1s started 0.615 of next games vs 0.586 given; on the
# reported chance, not the chain (handoff 363, ledger 1455: 15 of 15 seasons better at +0.05, worse in 2023-24 in the chain)
RESTED_NO1_LOGIT = 0.05
# another goalie started the team's last game, win or lose: the no. 1 started 0.571 of the games after vs 0.528 given; on
# the reported chance, every game left (handoff 364, ledger 2137: 15 of 15 seasons better at +0.1, the chain version weaker)
NO1_AFTER_OTHER_LOGIT = 0.1
# the rest of the week after those next-game rules: the last starter read high in two later games (findings 249, every
# season): a strict rotation's third game (0.48 against 0.37 actual) and, when no rotation, pattern, last-6 or
# three-goalie rule fires, the game after next (0.56 against 0.51)
ROTATION_THIRD_LOGIT, PLAIN_SECOND_LOGIT = -0.9, -0.45
# the no. 1 in the next game: a Sunday or Monday game after one day off (findings 254-255, handoff 291), else a road game
# that isn't night 2 of a back-to-back (findings 257, handoff 292); both picked in every fold, 2021-26
SUNMON_REST1_NO1_LOGIT, ROAD_NO1_LOGIT = -0.6, -0.3
# the no. 1's start logit by opponent, form and rest (findings 262-263, handoff 333): + NO1_OPP_LOGIT x the opponent's
# points% to date (shrunk OPP_SHRINK_GAMES toward last season) minus OPP_CENTER, per OPP_SCALE, clipped at +-OPP_CLIP;
# + NO1_NONWIN[0] + NO1_NONWIN[1] x his straight starts without a win (cap NO1_NONWIN_CAP), at NO1_NONWIN_LATER weight
# for games 2+ ahead; + NO1_REST[1] when the game is NO1_REST[0]+ days after the team's previous one
NO1_OPP_LOGIT, OPP_CENTER, OPP_SCALE, OPP_CLIP, OPP_SHRINK_GAMES = 0.2, 0.55, 0.10, 2.0, 10
NO1_NONWIN, NO1_NONWIN_CAP, NO1_NONWIN_LATER, NO1_REST = (0.2, -0.25), 3, 0.5, (3, 0.4)
# the no. 1's next game only: after he started one night of a back-to-back and the next game is 2 days after night 2
# (findings 264-265, handoff 334); after a backup started and didn't win the last game; after the latest backup start was a
# win 2-3 team games ago (findings 266-267, handoff 335)
NO1_SPLIT_B2B, NO1_AFTER_BACKUP_LOSS, NO1_AFTER_BACKUP_WIN = 0.8, 0.8, -0.6


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
LOSS_34_LOGIT_2 = -0.2  # ... and a 3-4 goal loss also lowers his game after next (handoff 625; bad starts keep theirs)
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
# Daily Faceoff's "Likely" starter starts that game at least this often: 23 of 23 live, Oct 8-14, while the site gave
# 0.64-0.75 (findings 463 (6), handoff 617; live-only, no archive of the tags). Re-grade at the Oct 30 review
LIKELY_START_FLOOR = 0.90
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
# a healthy goalie's Season share after the save% gap stays at least min(his share, 0.03), and a healthy goalie taking over
# an out-for-season one's share weighs at least 0.1 (handoff 446)
GOALIE_SEASON_FLOOR, GOALIE_TAKER_MIN = 0.03, 0.1
SAVE_SHRINK, LEAGUE_SAVE_PCT = (600, 1000), 0.902
# goalie Season values ran wider than what goalies went on to score (top eighth 2.55 against 2.14, bottom 0.21 against 0.33),
# while skaters' are calibrated: intercept + slope x (Season share x pts/start) for a goalie with any share puts them on the
# skaters' scale for the pooled board, keep values and trades; refit on this site's Season values, 2023-26 Mondays
# (findings 330 (2), handoff 271)
GOALIE_SEASON_SCALE = (0.32, 0.72)
# pulled (under 40:00 of ice time) in his last start, without the no-decision cut: his rest-of-season share came in
# 0.03 below (0.361 against 0.331); refit on this site's shares, 2023-26 Mondays (findings 330 (3), handoff 272)
PULLED_SEASON, PULLED_TOI = 0.03, 2400
# out 6+ team games: a skater who dressed for most of last season comes back sooner than the lineup chain expects,
# a high scorer slightly later than his rate suggests; logit += a x last season's dress rate + b x Season FP/G, fitted with
# model/season_dress.json's logit as offset, 2023-26 (findings 330 (4), handoff 273)
DURABLE_OUT = (0.31, -0.10)
# games this season (capped at 40, over 40) in the same logit: a skater new to the NHL (no games last season) who has stuck
# keeps his spot and one with 5 or fewer goes back down (findings 350 (2), handoff 311); one who played 1-40 games last
# season and has stuck this season keeps his too (findings 350 (3), handoff 312). Fitted with the live logit as offset
NEW_PLAYER_SEASON, FEW_GAMES_SEASON, PART_TIMER_SEASON = 0.41, -0.26, 0.38
# Daily Faceoff news in the same logit (findings 360 (1), handoff 330), fitted with the live chain as offset 2021-26:
# out now and sent down since his last game: a + b x ln(1 + team games out); in the lineup and called up in the last
# NEWS_RECALL_DAYS: a + b x min(GP, 40)/40; latest injury item since his last game says season-ending / surgery
NEWS_SENT_DOWN, NEWS_CALLED_UP, NEWS_RECALL_DAYS = (-0.82, 0.18), (-0.18, 0.18), 60
NEWS_SEASON_OUT, NEWS_SURGERY = -1.5, -0.33
# a conditioning loan: dressed 0.20-0.44 of the rest vs 0.11-0.18 predicted (hunt14/snotes, findings 505 (5), handoff 688)
NEWS_CONDITIONING_SEASON = 0.5
# Season dress logit for a skater out now from ESPN's own note: its stated timeline as a share of his team's games left
# (findings 488, handoff 658), and out for the season when Daily Faceoff hasn't said so (handoff 659)
SEASON_TIMELINE_C, ESPN_SEASON_OUT = -0.6, -3.0
# ESPN-listed after playing his team's last game: dressed 0.61-0.70 of the rest vs 0.66-0.78 predicted (hunt14/snotes,
# findings 505 (4), handoff 687); a note posted from the day before that game, not a personal, scratch or demotion note
LISTED_PLAYED_SEASON = -0.3
NOT_INJURY_NOTE = re.compile(r"personal|healthy scratch|scratched|assigned|sent down|reassigned|demot|loan", re.I)
NEWS_INDEFINITE = -0.3  # "out indefinitely", "long-term", "several months" without season-level wording (findings 371, idea 963)
NEWS_STALE_DAYS = 2
# lineup spots per team (12 F, 6 D) and the slack weights on the Season dress logit (findings 370, handoff 350)
LINEUP_SPOTS = {False: 12, True: 6}
LINEUP_SLACK = (1.34, 2.84)
# last season's regulars (MISSING_REG_GP+ games) with no NHL game yet this season: they come back and take games from the
# skaters filling in, which the slack (skaters with games this season) can't see. Season dress logit += c x W x K/(K + team
# games), W = their last-season TOI/G / 15 min (F) or 20 min (D), summed per last season's team; team MISSING_REG_MIN_TG+
# games (handoffs 509, 513, findings 402: Season Spearman +0.0003 all / +0.0007 free agents, every test season)
MISSING_REG_C, MISSING_REG_K, MISSING_REG_GP, MISSING_REG_MIN_TG = -0.08, 20, 40, 3
# rookies picked in the top 24 dress for more of the rest of the season than the Season share says (hunt14/rookies,
# findings 477, handoff 632): first NHL season within the last two, at most 60 NHL games over the last two seasons
ROOKIE_PICK_SEASON_LOGIT, ROOKIE_PICK_MAX, ROOKIE_PICK_GP = 0.3, 24, 60
# Week: those regulars with a Daily Faceoff injury or roster-move item in the last WEEK_MISSING_NEWS_DAYS count in the Week
# slack sums as WEEK_MISSING_A x K/(K + team games) x last-season TOI/G / (15 F, 20 D) players on every date, from the team's
# 3rd game (handoff 510, findings 402: Brier -0.023/-0.036 points, both held-out seasons)
WEEK_MISSING_A, WEEK_MISSING_K, WEEK_MISSING_GP, WEEK_MISSING_NEWS_DAYS, WEEK_MISSING_MIN_TG = 0.5, 20, 40, 30, 3
# Week: per team game, the same open spots from the per-game Week chances themselves; logit += over x min(s, 0) + room x
# max(s, 0), s = open spots / 3 (handoff 489, findings 402: Brier -0.50 / -0.57 points, fits 2.23/1.14 and 1.98/0.96)
# handoff 517: over-full side 2.0 -> 2.8 in a joint refit after 515-516 moved the scratch lifts (findings 402)
WEEK_SLACK = (2.8, 1.0)
# healthy regulars (dressed his team's last game at DEPTH_TOI+ minutes, no injury listing) dress more than the Week model
# gives them: + this on the logit after the slack fit, outside its sums (handoff 355, findings 317: site 0.951 vs actual 0.981
# live Oct 9-13; still ~0.96 after the boosted Week model and the slack fit)
HEALTHY_REG_LOGIT = 0.5
# a healthy depth skater (under DEPTH_TOI, not left early) with FIGHT_PIM+ penalty minutes in his last game keeps his spot:
# + this on the same step (handoff 356, findings 323: +0.065/+0.092 dressed over the site's chance, offset +0.75/+0.57)
FIGHT_LOGIT, FIGHT_PIM = 0.5, 5
# before that: the first home game after WEEK_TRIP_MIN+ straight road games brings skaters who missed games back (+0.1) and
# sits regulars a little (-0.6 x (1 - p)); out WEEK_OUT_LONG_GAMES+ straight games, +0.05 a game further into the week
# (handoff 500, findings 402: Brier -0.085 / -0.111 points on top of the slack)
WEEK_HOME_AFTER_TRIP, WEEK_TRIP_MIN = (0.1, -0.6), 2
WEEK_OUT_LONG, WEEK_OUT_LONG_GAMES = 0.05, 3
# ESPN's preseason projected FP/G blended into the Season FP/G (after the age term) with weight a + b g + c g^2,
# g = min(GP, 40)/40: 0.29 at 0 games, about 0.07-0.08 from 30 games on (findings 360 (2), handoffs 331-332)
ESPN_PROJ_BLEND = (0.29, -0.57, 0.35)
# Season FP/G over-reacts to a change in ice time vs last season seen in a few games: pull it back, gone after 10 games
# (handoff 507, findings 402: Season Spearman +0.0002 all / +0.0004 free agents, every test season)
SEASON_TOI_CHG_C, SEASON_TOI_CHG_K, SEASON_TOI_CHG_GAMES = -0.28, 3, 10
SEASON_TOI_CHG_CAP = 5.0  # minutes (handoff 512)
# boosted models trained on 16 seasons (model/train_boost.py, scripts/season_boost.py; findings 360 (3)-(4), handoff 332):
# the Season dress logit moves BOOST_DRESS_W of the way to the boosted one, and the Season FP/G gains
# (boosted - FP/G) x (a + b g). The ESPN weights above were refitted jointly with these, leaving one season out over 2021-26
BOOST_DRESS_W = 0.56
# Week dress logit shifts from Daily Faceoff news since his last game (handoffs 406-408, ideas 1137-1139, findings 400;
# hunt13/p_week*.py: Brier on out-now player-games -5.2% / -5.8% in 2024-25 / 2025-26, all rows -2.1% / -2.6% with 407-408).
# Handoff 428 (idea 1180, hunt13/p_stack_fast.py): practice, skate, gtd, dtd, wtw and the timeline refit on top of the boosted
# model once stat lines stopped reading as timelines; a timeline 7+ days past the game, or one already run out (timeline_up)
WEEK_NEWS = {"practice": 1.55, "skate": 1.19, "gtd": 0.37, "ret": 0.5, "dtd": 0.16, "wtw": -0.21, "long": -0.7, "timeline": -1.25,
             "timeline_up": 0.17, "down_out": -1.1, "down_in": -1.4, "hurt_out": -2.1, "hurt_dtd": -1.9}
WEEK_NEWS_TIMELINE_DAYS = 7
# league-wide F/D scoring drift into the Season FP/G (handoff 405, idea 1118: +0.0002 full board, +0.0005 free agents)
DRIFT_W, DRIFT_MIN_GP = 0.5, 10
# the Week's per-game dress logits move BOOST_WEEK_W x (logit(b) - logit(mean of his per-game chances)), b = a 16-season
# boosted next-week dress share (model/season_boost_week.json) clipped to BOOST_WEEK_CLIP, after the other per-game shifts
# and the news terms (handoffs 411-414, ideas 1142, 1161-1168: with its injury-news inputs and 406-408, Brier -5.1% / -9.6%
# in 2024-25 / 2025-26, Week Spearman +0.008)
BOOST_WEEK_W, BOOST_WEEK_CLIP = 0.9, (0.005, 0.995)
BOOST_FPG_BLEND = (0.60, -0.37)  # an older stored copy (the fetch has failed since) adds nothing
# a goalie whose last game last season was for another team: his last-season pts/start, for the Season rating only, moves
# by this x (his team's points% last season - his old team's); 3 is the scoring math, a win is worth 3 more than a loss
# (findings 330 (5), handoff 274)
MOVER_PPS_BETA = 3.0


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
        return (LOSS_LOGITS.get(g["ga"], LOSS_LOGIT), LOSS_34_LOGIT_2 if g["ga"] in (3, 4) else 0.0)
    # started, but another goalie got the decision
    return (NO_DECISION_LOGIT, NO_DECISION_LATER[0]) + (NO_DECISION_LATER[1],) * (NO_DECISION_GAMES - 2)


# P(dresses) logit shift by game number from today (1, 2, 3, 4+), for a skater who dressed for his team's last game
# and for one who missed it: the weekly P is right on average but a player who is out is less likely to play the next
# game than the last one, and a regular the reverse (findings 147)
DRESS_SHIFT_IN, DRESS_SHIFT_OUT = (0.6, 0.2, -0.1, -0.3), (-0.55, -0.15, 0.1, 0.2)
# a healthy scratch in his team's last game comes back far more often than an injured player: +0.5 on top of the
# missed-game shift, every game (0.25 to 0.34 per game against 0.345 actual, findings 153); split by position, since
# scratched defensemen come back more often than forwards (findings 291, handoff 345)
# Since handoff 516 it is applied after the boosted Week model at half the old size (F 0.3, D 0.65, long 0.75 before it;
# after it at full size the fit was worse; LOSO scale 0.5 / 0.375, hunt13/lift_order)
SCRATCH_LOGIT = {"F": 0.15, "D": 0.325}
# ... and a healthy scratch who has missed 5+ straight games gets a turn, F and D alike (findings 293, handoff 347)
SCRATCH_LONG_LOGIT, SCRATCH_LONG_GAMES = 0.375, 5
# ... and a teammate at his position (5+ games) left the team's last game early, under 40% of his average minutes: the
# scratch fills in, D most (0.6 dressed against the site's 0.3, findings 307, handoff 352)
# Handoff 353 (findings 308): a second band, 40-55% of his minutes, and D +1.8 under 40%: (share below, {F, D} logit)
TEAMMATE_LEFT_BANDS, TEAMMATE_LEFT_GP = ((0.40, {"F": 0.5, "D": 1.8}), (0.55, {"F": 0.25, "D": 1.2})), 5
# a healthy scratch after his team's last game, on top of the win-run shift (findings 312, handoff 354): by game from his
# team's next one (game 1, game 2, later games), strongest next game and gone by the third after a non-win (findings
# 326-327, handoff 357; replaces 354's flat 0.15, 0.225 after handoff 517)
SCRATCH_LAST_RESULT_BY_GAME = {"win": (-0.35, -0.15, -0.10), "other": (0.20, 0.15, 0.0)}
# a healthy depth forward's special-teams role in his last game: 1:00+ shorthanded, or no power-play and no shorthanded time
# (findings 328-329, handoff 358). Added with the healthy-regular and fight lifts, after the lineup fit: measured there on
# the current stack, -0.2 for no special teams was too much and inside the fit it hurt out-now players
# (hunt13/st_role_post). Defencemen unchanged
ST_ROLE_LOGIT, ST_PK_MIN = {"pk": 0.2, "none": -0.1}, 1.0
# a healthy scratch whose last game played, within 10 days, had 0:30+ of power-play time comes back less (findings 332-333,
# handoff 359); measured on the current stack at -0.1 with the lifts above, not the row's -0.25 (hunt13/pp_scratch_post)
PP_SCRATCH_LOGIT, PP_SCRATCH_MIN, PP_SCRATCH_DAYS = -0.1, 0.5, 10
# a healthy depth skater who played the fewest minutes of his team's dressed forwards (or D) in its last game sits more,
# other depth skaters less (findings 335-336, handoff 360); measured on the current stack at -0.1 / +0.1 with the lifts
# above, not the row's -0.15 / +0.15 (hunt13/toi_rank_post)
DEPTH_RANK_LOGIT = {"lowest": -0.1, "other": 0.1}
# ... but the lowest-minutes one who had a point in that game keeps his spot more: +0.2 back (findings 343, handoff 362;
# measured on the current stack, every season, half and position)
LOW_SCORER_LOGIT = 0.2
# missed his team's last game, not on its healthy-scratch list, no ESPN injury status, no Daily Faceoff injury or
# roster-move item since that game (hunt14/week, findings 475, handoff 623): 0.10 predicted vs 0.06 dressed
NO_STATUS_OUT_LOGIT = -0.5
# a Daily Faceoff day-to-day item counts more against his team's next game: the news terms are flat over the week and
# over-call game 1, 0.24 predicted vs 0.18 dressed (handoff 624)
DTD_NEXT_LOGIT = -0.4
# ESPN-listed, note's stated return 0-7 days away: games before it dressed 0.18 / 0.19 vs 0.28 / 0.24 predicted
# (hunt14/returns2, findings 496, handoff 670)
BEFORE_RETURN_LOGIT = -0.6
# the scratch lifts above (teammate left early, last result, the scratch logit itself) are applied after the boosted Week
# model, with the news: before it, the boost took back ~90% of them (handoffs 515-516, hunt13/lift_order)
# a depth skater (under DEPTH_TOI minutes) with a point in his last game keeps his spot more (findings 304, handoff 351)
DEPTH_POINT_LOGIT = 0.3
# team form (findings 281-283, 289, 292, handoffs 340-342, 344): additive on each game's dress chance this week.
# Depth = healthy, dressed his team's last game, under DEPTH_TOI minutes in it. After 4+ straight games without a win depth
# skaters sit more (340); after a loss by 4+ goals more still (341, the larger of the two, not both); a team on 3+ straight
# wins keeps skaters out 1-4 games out (342, -0.07 per game, findings 296)
DEPTH_TOI = {"F": 13.0, "D": 16.0}
SLUMP_GAMES, SLUMP_DEPTH = 4, {"F": -0.03, "D": -0.065}
BLOWOUT_GOALS, BLOWOUT_DEPTH = 4, -0.07
WIN_RUN_GAMES, WIN_RUN_OUT, WIN_RUN_OUT_GAMES = 3, -0.07, (1, 4)
# low minutes in his last game (not left early): defensemen under 12 / 12-14 / 14-16 min (findings 292, 294, handoff 346) and
# forwards under 8 / 10-12 min (findings 294, handoff 348) dress less than the lineup model says; stacks with the team form
DEPTH_D_SHIFT = ((12.0, -0.025), (14.0, -0.055), (16.0, -0.025))
DEPTH_F_SHIFT = ((8.0, -0.02), (10.0, 0.0), (12.0, -0.03))
# a team's first EARLY_DEPTH_GAMES games: depth skaters (under 12 F / 16 D minutes) sit a further 3 points (findings 295, handoff 349)
EARLY_DEPTH_GAMES, EARLY_DEPTH_TOI, EARLY_DEPTH = 15, {"F": 12.0, "D": 16.0}, -0.03
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


def logit(q):
    q = min(max(q, 1e-4), 1 - 1e-4)
    return math.log(q / (1 - q))


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


# rest-of-season dress share for a healthy skater on an NHL roster yet to play this season, by his games last season (20-39,
# 40-59, 60+) and his team's games so far (0-3, 4-7, 8-12, 13-20, 21-35, 36+): the longer his team plays without him, the
# less of the rest he plays. 2022-26 Mondays; it replaced a flat 0.38 for 60+ GP and the 0.1 idle cap (findings 350 (1),
# handoff 310)
# 1-19 GP last season: findings 350 (4), handoff 313
UNPLAYED_GP = (1, 20, 40, 60)
UNPLAYED_TEAM_GAMES = (3, 7, 12, 20, 35)
UNPLAYED_SHARE = ((0.119, 0.097, 0.087, 0.074, 0.065, 0.043),
                  (0.275, 0.145, 0.118, 0.095, 0.064, 0.030),
                  (0.434, 0.196, 0.156, 0.110, 0.081, 0.046),
                  (0.717, 0.349, 0.272, 0.195, 0.130, 0.081))
# hurt before playing (PRESEASON_INJURED) or just cleared (CLEARED_NEW_SEASON): their share holds through 7 team games,
# then falls as the 60+ row does (its value over the 4-7 cell's 0.349)
UNPLAYED_NOTED_MULT = (1.0, 1.0, 0.78, 0.56, 0.37, 0.23)


def unplayed_cell(team_played):
    """Column of UNPLAYED_SHARE for a team that has played this many games."""
    return next((i for i, top in enumerate(UNPLAYED_TEAM_GAMES) if team_played <= top), len(UNPLAYED_TEAM_GAMES))


def unplayed_share(prev_gp, team_played):
    """Rest-of-season dress share of a healthy skater yet to play, or None with no games last season (findings 350 (1), (4))."""
    band = sum(prev_gp >= g for g in UNPLAYED_GP) - 1
    return UNPLAYED_SHARE[band][unplayed_cell(team_played)] if band >= 0 else None
# an ESPN status (injury, suspension) in the injury log within CLEARED_DAYS that ESPN has now dropped, and he hasn't played since
CLEARED_DAYS = 7
CLEARED_NEW_DRESS, CLEARED_NEW_NEXT, CLEARED_NEW_SEASON = 0.8, 0.75, 0.72  # no games yet this season (findings 172)
CLEARED_LOGIT, CLEARED_SEASON_LOGIT = 0.9, 0.4  # games this season, missed the last one (findings 173)
# DAY_TO_DAY and dressed for the team's last game: cap by that game's TOI over his season average (findings 176)
DTD_PLAYED_CAPS = ((0.55, 0.15), (0.8, 0.5), (float("inf"), 0.85))  # full minutes 0.85, not 0.75 (handoff 630)
OUT_LEFT_EARLY_CAP = 0.15  # OUT and dressed for the last game but under 0.55 of his TOI: 0.17-0.21 actual (findings 176)
DFO_LINEUP_NEXT = 0.9  # sat the last game, no ESPN status, in Daily Faceoff's lines: next-game floor (findings 174)
PPS_PRIOR = 15  # starts of league-average points blended into each goalie's own points per start
# the Season rating's pts/start only: the shrink K by NHL starts over the last three seasons, under 50 / 50-100 / over 100
# (hunt14/gseason, findings 485, handoff 641); the Week keeps PPS_PRIOR
SEASON_PPS_K = ((50, 8), (100, 15), (float("inf"), 30))
# older goalies lose Season start share with games left: share - 0.009 x (team games left / 82) x years over 30
# (findings 484, handoff 640); not handed to partners
GOALIE_AGE_SEASON = 0.009
NEWS_CAP = 25
OFFSEASON_MOVER_MULT = 0.965  # skater on a new team since last season: leave-one-season-out 0.963-0.970 (findings 230)
OFFSEASON_MOVER_MULT_WEEK = 0.98  # the Week's: movers score 0.973-0.990 of the current Week ridge (findings 464, handoff 667)
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
                **{k: r.get(k) or 0 for k in ("goals", "assists", "ppPoints", "shPoints", "shots")},
                "pim": r.get("penaltyMinutes") or 0}
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
                 "fp": goalie_fp(r), "home": r["homeRoad"] == "H" if r.get("homeRoad") in ("H", "R") else None}
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


def relief_points(s, season):
    """{goalie id: fantasy points in his relief appearances}. ESPN scores relief appearances 0 in this league (since Oct 11
    2026, retroactively), so a per-start rate counts started games only (handoff 486). Empty if the report can't be read."""
    try:
        rows = nhl_get(s, f"{NHL_STATS}/goalie/summary", isAggregate="false", isGame="true", start=0, limit=-1,
                       cayenneExp=f"seasonId={season} and gameTypeId=2 and gamesStarted=0").json()["data"]
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"Relief appearances {season} unavailable: {e!r}", file=sys.stderr)
        return {}
    out = {}
    for r in rows:
        out[r["playerId"]] = out.get(r["playerId"], 0.0) + goalie_fp(r)
    return out


def load_prev_goalie_season(s, season):
    """Last season per goalie: (games started, fantasy points in started games, saves, shots against, last team). The
    teams are listed in the order he played for them; saves and shots include relief appearances."""
    relief = relief_points(s, season)
    return {r["playerId"]: (r.get("gamesStarted") or 0, goalie_fp(r) - relief.get(r["playerId"], 0.0), r.get("saves") or 0,
                            r.get("shotsAgainst") or 0, (r.get("teamAbbrevs") or "").split(",")[-1].strip() or None)
            for r in stats_report(s, "goalie/summary", season, teams=True)}


def prev_points_pct(s, prev_season):
    """Each team's points% at the end of last season, (2W + OTL) / (2 x games). Empty if the standings can't be read."""
    try:
        end = next(x["standingsEnd"] for x in nhl_get(s, f"{NHL_WEB}/standings-season").json()["seasons"] if x["id"] == prev_season)
        return {r["teamAbbrev"]["default"]: (2 * r["wins"] + r["otLosses"]) / (2 * max(r["gamesPlayed"], 1))
                for r in nhl_get(s, f"{NHL_WEB}/standings/{end}").json().get("standings", [])}
    except (requests.RequestException, KeyError, StopIteration, ValueError, TypeError) as e:  # a null field: handoff 508
        print(f"Last season's standings unavailable, goalie team changes not priced: {e!r}", file=sys.stderr)
        return {}


def opp_last_ga(s, prev_season):
    """{team: last season's goals against per game, shrunk OPP_LAST_SHRINK games toward the league's, / league - 1}. Empty if
    the standings can't be read (handoff 504)."""
    try:
        end = next(x["standingsEnd"] for x in nhl_get(s, f"{NHL_WEB}/standings-season").json()["seasons"] if x["id"] == prev_season)
        rows = nhl_get(s, f"{NHL_WEB}/standings/{end}").json().get("standings", [])
        lg = sum(r["goalAgainst"] for r in rows) / max(sum(r["gamesPlayed"] for r in rows), 1)
        return {r["teamAbbrev"]["default"]: (r["goalAgainst"] + OPP_LAST_SHRINK * lg) / (r["gamesPlayed"] + OPP_LAST_SHRINK) / lg - 1
                for r in rows} if lg else {}
    except (requests.RequestException, KeyError, StopIteration, ValueError, TypeError) as e:  # a null field: handoff 508
        print(f"Last season's goals against unavailable, no opponent term: {e!r}", file=sys.stderr)
        return {}


def opponent_strength(goalie_games, prev_pct):
    """{team: (its points% to date shrunk OPP_SHRINK_GAMES games toward last season's, OPP_CENTER without one) - OPP_CENTER,
    per OPP_SCALE, clipped at +-OPP_CLIP}, from the decisions in this season's goalie rows (findings 262)."""
    pts, games = {}, {}
    for g in goalie_games:
        games.setdefault(g["team"], set()).add(g["game"])
        pts[g["team"]] = pts.get(g["team"], 0) + 2 * g["wins"] + g["otl"]
    out = {}
    for team in set(games) | set(prev_pct):
        n, last = len(games.get(team, ())), prev_pct.get(team, OPP_CENTER)
        pct = (pts.get(team, 0) + 2 * OPP_SHRINK_GAMES * last) / (2 * (n + OPP_SHRINK_GAMES))
        out[team] = min(max((pct - OPP_CENTER) / OPP_SCALE, -OPP_CLIP), OPP_CLIP)
    return out


def nonwin_streaks(goalie_games):
    """{goalie id: his straight starts without a win, latest first} (findings 262)."""
    out = {}
    for g in sorted((g for g in goalie_games if g["started"]), key=lambda g: (g["date"], g["game"]), reverse=True):
        if g["id"] not in out:
            out[g["id"]] = [0, True]
        run = out[g["id"]]
        if run[1]:
            if g["wins"]:
                run[1] = False
            else:
                run[0] += 1
    return {gid: n for gid, (n, _) in out.items()}


def team_form(goalie_games):
    """{team: (straight games without a win, straight wins, last game's goal margin or None)}, latest game first, from
    this season's goalie rows (findings 281-283). A game is won when one of the team's goalies has the win; goals for are
    the other team's goals against in the same game."""
    games = {}
    for g in goalie_games:
        t = games.setdefault((g["team"], g["game"]), {"date": g["date"], "win": False, "ga": 0})
        t["win"] = t["win"] or bool(g["wins"])
        t["ga"] += g.get("ga", 0)
    teams_in = {}
    for team, gid in games:
        teams_in.setdefault(gid, []).append(team)
    out = {}
    for team in {t for t, _ in games}:
        seq = sorted(((t["date"], gid, t) for (tm, gid), t in games.items() if tm == team), key=lambda x: (x[0], x[1]), reverse=True)
        nonwins = next((k for k, (_, _, t) in enumerate(seq) if t["win"]), len(seq))
        wins = next((k for k, (_, _, t) in enumerate(seq) if not t["win"]), len(seq))
        _, gid, last = seq[0]
        opp = [tm for tm in teams_in[gid] if tm != team]
        margin = games[(opp[0], gid)]["ga"] - last["ga"] if opp else None
        out[team] = (nonwins, wins, margin)
    return out


def left_early_teammates(by_player, team_games):
    """{(team, is D): {NHL id: band}} for skaters with TEAMMATE_LEFT_GP+ games who played the team's last game on under
    a TEAMMATE_LEFT_BANDS share of their average minutes; band 0 = under 40%, 1 = 40-55% (findings 307-308)."""
    out = {}
    for pid, gl in by_player.items():
        tg = team_games.get(gl[-1]["team"]) if gl else None
        if not tg or len(gl) < TEAMMATE_LEFT_GP or gl[-1]["game"] != tg[-1]:
            continue
        avg = sum(g["toi"] for g in gl) / len(gl)
        band = next((k for k, (cut, _) in enumerate(TEAMMATE_LEFT_BANDS) if gl[-1]["toi"] < cut * avg), None) if avg else None
        if band is not None:
            out.setdefault((gl[-1]["team"], gl[-1].get("pos") == "D"), {})[pid] = band
    return out


def lowest_toi(by_player, team_games):
    """{(team, is D): fewest minutes among the team's dressed forwards (or D) in its last game} (handoff 360)."""
    out = {}
    for gl in by_player.values():
        tg = team_games.get(gl[-1]["team"]) if gl else None
        if tg and gl[-1]["game"] == tg[-1]:
            key = (gl[-1]["team"], gl[-1].get("pos") == "D")
            out[key] = min(out.get(key, gl[-1]["toi"]), gl[-1]["toi"])
    return out


def form_shift(pos, depth_toi, out_games, form, team_played=99):
    """Shift on each game's dress chance this week (handoffs 340-342, 346, 348 and 349, stacked per 344), and the reasons.
    pos 'F' or 'D'; depth_toi: last-game minutes for a healthy skater who dressed his team's last game, else None;
    out_games: his team's straight games he has missed (not long-term injured), else 0; form: team_form's tuple or None;
    team_played: his team's games so far this season."""
    shift, why = 0.0, []
    nonwins, wins, margin = form or (0, 0, None)
    if depth_toi is not None and depth_toi < DEPTH_TOI[pos]:
        drop = min(SLUMP_DEPTH[pos] if nonwins >= SLUMP_GAMES else 0.0,
                   BLOWOUT_DEPTH if margin is not None and margin <= -BLOWOUT_GOALS else 0.0)
        if drop:
            shift += drop
            why.append("blowout" if drop == BLOWOUT_DEPTH and margin is not None and margin <= -BLOWOUT_GOALS else "slump")
    if depth_toi is not None:
        d = next((x for top, x in (DEPTH_D_SHIFT if pos == "D" else DEPTH_F_SHIFT) if depth_toi < top), 0.0)
        if d:
            shift += d
            why.append("depth_d" if pos == "D" else "depth_f")
        if team_played < EARLY_DEPTH_GAMES and depth_toi < EARLY_DEPTH_TOI[pos]:
            shift += EARLY_DEPTH
            why.append("early")
    if WIN_RUN_OUT_GAMES[0] <= out_games <= WIN_RUN_OUT_GAMES[1] and wins >= WIN_RUN_GAMES:
        shift += WIN_RUN_OUT
        why.append("win_run")
    return shift, why


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


def displaced_skaters(by_player, team_games, healthy, pickable=None):
    """When a regular (dressed 80%+ of his team's 5-20 games before the gap) has missed 3+ straight team games and
    ESPN now lists him healthy or day-to-day, the team's skater at his position (F or D) with the least ice time in
    the last game is the one who sits, among those ESPN lists healthy too (handoff 403). healthy: NHL ids ESPN lists
    without a longer injury. pickable: NHL ids the cut may land on (handoff 620: healthy plus skaters ESPN's pool here
    doesn't carry, who have no status), healthy when None.
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
        depth = [(h["toi"], hid) for hid, (h, htg, _, hs) in last.items() if hs == 0 and h["team"] == g["team"]
                 and h["game"] == tg[-1] and ("D" if h["pos"] == "D" else "F") == grp and hid in (healthy if pickable is None else pickable)]
        if depth:
            out[min(depth)[1]] = (DISPLACED_CUT[grp], g["name"])
    return out


def roster_moves(by_player, team_games, listed, healthy, available=None):
    """Trades and waiver claims, from ESPN's NHL team for each skater (listed: {nhl id: NHL team}) against his game log.
    Arrival (findings 269, 272, handoff 337): listed on a team he has played under ARRIVAL_GAMES games for, after games for
    another NHL team this season; the team's skater at his position (F or D) with the least ice time in its last game, not
    the newcomer, sits more by DISPLACED_CUT. Departure (findings 270-272, handoff 338): a defenseman who dressed in one of
    the team's last DEPARTED_D_GAMES games and is now listed on another team lifts the team's D listed with it and healthy
    (healthy: no ESPN status at all) who missed the last 1-4 team games. The cut only lands on a skater in available (no
    ESPN status or day-to-day; handoff 403), all of them when None.
    Returns ({nhl id: (cut, newcomer's name)}, {nhl id: departed D's name})."""
    last, streaks = {}, {}
    for pid, gl in by_player.items():
        tg = team_games.get(gl[-1]["team"], [])
        if tg:
            played = {g["game"] for g in gl}
            last[pid] = gl[-1]
            streaks[pid] = next((i for i, g in enumerate(reversed(tg)) if g in played), len(tg))
    grp = lambda g: "D" if g["pos"] == "D" else "F"
    available = set(last) if available is None else available
    cuts, lifts = {}, {}
    for pid, team in listed.items():
        gl = by_player.get(pid)
        if not gl or not team_games.get(team):
            continue
        here = sum(g["team"] == team for g in gl)
        if here < ARRIVAL_GAMES and any(g["team"] != team for g in gl):
            tg = team_games[team]
            depth = [(h["toi"], hid) for hid, h in last.items() if hid != pid and streaks[hid] == 0 and h["team"] == team
                     and h["game"] == tg[-1] and grp(h) == grp(gl[-1]) and listed.get(hid, team) == team and hid in available]
            if depth:
                cuts.setdefault(min(depth)[1], (DISPLACED_CUT[grp(gl[-1])], gl[-1]["name"]))
        # his last game for a team other than the one he is listed on now, so the lift outlasts his first game for the new
        # team (handoff 402)
        prev = next((g for g in reversed(gl) if g["team"] != team), None)
        old = prev["team"] if prev else None
        if old and team in team_games and grp(prev) == "D" and prev["game"] in team_games.get(old, [])[-DEPARTED_D_GAMES:]:
            for hid, h in last.items():
                if h["team"] == old and grp(h) == "D" and listed.get(hid) == old and hid in healthy \
                        and DEPARTED_D_OUT[0] <= streaks[hid] <= DEPARTED_D_OUT[1]:
                    lifts.setdefault(hid, prev["name"])
    return cuts, lifts


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


TEAMMATE_REP, TEAMMATE_K = {"F": 12.0, "D": 17.0}, 2.0  # usual minutes shrunk 2 games to these (handoff 678)
PP_TEAM_MIN, PP_RATE_K = 0.25, 5.0  # a game counts for his PP share when the team had over 0.25 PP min; rate shrunk 5 games


def teammate_inputs(by_player, team_games, boost_x=None, boost_week=None):
    """The forecast ridges' teammate and PP-share inputs per skater (handoff 678, findings 498; model/train.py add_teammates).
    Usual minutes m = (TOI with that team this season + 2 x 12 min F / 17 D) / (games with it + 2). His team = his last
    game's. C10 = mean over his last 10 games with the team of the summed m of same-position (F or D) teammates who dressed;
    C_hat = sum over same-position teammates whose latest game was for the team of m x their next-week dress share (the
    boosted Week dress model's raw output, model/season_boost_week.json, before the per-game lifts; a teammate it doesn't
    cover: his share of the team's last 10 games); sh_hat = (C10 - C_hat) x m / (C_hat + m), None without the Week model.
    ps_l5 / l10 / std: his PP TOI / the team's PP time (the most PP TOI of any of its skaters that game, games over 0.25
    min, capped at 1) over his last 5 / 10 / season games; ppx_w = ps_w (missing 0) x the team's PP minutes per game
    shrunk 5 games to the league's."""
    present, team_pp, tot, is_d = {}, {}, {}, {}
    for pid, gl in by_player.items():
        if not gl:
            continue
        is_d[pid] = gl[-1].get("pos") == "D"
        for g in gl:
            k = (g["team"], g["game"])
            present.setdefault(k, []).append(pid)
            team_pp[k] = max(team_pp.get(k, 0.0), g.get("pptoi") or 0.0)
            t = tot.setdefault((pid, g["team"]), [0.0, 0])
            t[0] += g["toi"]
            t[1] += 1

    def usual(pid, team):
        s, n = tot.get((pid, team), (0.0, 0))
        return (s + TEAMMATE_K * TEAMMATE_REP["D" if is_d[pid] else "F"]) / (n + TEAMMATE_K)
    pp_sum, pp_n = {}, {}
    for team, tg in team_games.items():
        pp_sum[team], pp_n[team] = sum(team_pp.get((team, g), 0.0) for g in tg), len(tg)
    league = sum(pp_sum.values()) / sum(pp_n.values()) if sum(pp_n.values()) else 0.0
    rate = {t: (pp_sum[t] + PP_RATE_K * league) / (pp_n[t] + PP_RATE_K) for t in pp_sum}
    roster, q = {}, {}
    for pid, gl in by_player.items():
        if not gl:
            continue
        team = gl[-1]["team"]
        roster.setdefault((team, is_d[pid]), []).append(pid)
        if boost_week:
            if pid in (boost_x or {}):
                q[pid] = min(max(season_boost.predict(boost_week, boost_x[pid]), 0.0), 1.0)
            else:
                last10, mine = team_games.get(team, [])[-10:], {g["game"] for g in gl}
                q[pid] = sum(g in mine for g in last10) / len(last10) if last10 else 0.0
    out = {}
    for pid, gl in by_player.items():
        if not gl:
            continue
        team, d = gl[-1]["team"], is_d[pid]
        m = usual(pid, team)
        f = {"sh_hat": None}
        mine = [g["game"] for g in gl if g["team"] == team][-10:]
        if boost_week and mine:
            c10 = mean([sum(usual(i, team) for i in present.get((team, g), ()) if i != pid and is_d[i] == d) for g in mine])
            c_hat = sum(q[i] * usual(i, team) for i in roster.get((team, d), ()) if i != pid)
            f["sh_hat"] = (c10 - c_hat) * m / (c_hat + m)
        f["sh_hat_D"] = None if f["sh_hat"] is None else f["sh_hat"] * d
        tpp = [team_pp.get((g["team"], g["game"]), 0.0) for g in gl]
        shares = [min((g.get("pptoi") or 0.0) / t, 1.0) if t > PP_TEAM_MIN else None for g, t in zip(gl, tpp)]
        for w, n in (("l5", 5), ("l10", 10), ("std", None)):
            tail = [v for v in (shares[-n:] if n else shares) if v is not None]
            f[f"ps_{w}"] = mean(tail) if tail else None
            f[f"ppx_{w}"] = (f[f"ps_{w}"] or 0.0) * rate[team] if team in rate else None
        out[pid] = f
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


def load_goalie_starts(s, seasons):
    """{NHL id: NHL games started over these seasons}, from the goalie summary report; a season it can't read counts 0."""
    out = {}
    for season in seasons:
        try:
            rows = stats_report(s, "goalie/summary", season)
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"NHL goalie summary {season} unavailable: {e}", file=sys.stderr)
            continue
        for r in rows:
            if r.get("playerId"):
                out[r["playerId"]] = out.get(r["playerId"], 0) + (r.get("gamesStarted") or 0)
    return out


def load_birthdates(s, seasons, bios=None):
    """NHL id: birth date, from the stats API's skater bios report for these seasons. Empty if it can't be read: the
    Season rating then goes without its age adjustment. bios, if given, is filled with {id: (birth date, draft overall,
    first NHL season)} for the boosted Season models (findings 360 (3))."""
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
            if bios is not None and r.get("playerId") and r["playerId"] not in bios:
                bios[r["playerId"]] = (out.get(r["playerId"]), r.get("draftOverall"), r.get("firstSeasonForGameType"))
    for season in seasons:  # goalies too, for the Season start share's age cut (handoff 640)
        try:
            rows = stats_report(s, "goalie/bios", season)
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"NHL goalie bios {season} unavailable: {e}", file=sys.stderr)
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
            # the boosted Season FP/G's MoneyPuck rates (season_boost.mp_rates, findings 360 (4))
            row.update(icetime=num(f, "icetime"), hdxg=num(f, "I_F_highDangerxGoals"), gamescore=num(f, "gameScore"),
                       a1=num(f, "I_F_primaryAssists"), takeaways=num(f, "I_F_takeaways"), oz=num(f, "I_F_oZoneShiftStarts"),
                       dz=num(f, "I_F_dZoneShiftStarts"), oxg_pct=num(f, "onIce_xGoalsPercentage") if "onIce_xGoalsPercentage" in ix else None)
        elif pre == "pp_":
            row["pp_icetime"] = num(f, "icetime")
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


def nhl_xfp(x, games):
    """expected_fp's note on the NHL's own points per game: MoneyPuck's 5-on-4 / 4-on-5 split misses PP and SH points
    scored in other strengths (PPP -9%, SHP -14%), so the shown pts/game uses his NHL games, and the expected number moves
    by the same amount, keeping the gap that decides hot or cold (handoff 487)."""
    if not x or not games:
        return x
    nhl = sum(g.get("fp") or 0 for g in games) / len(games)
    return {**x, "fpg": round(nhl, 2), "xfpg": round(x["xfpg"] + nhl - x["fpg"], 2)}


def week_schedule(s, monday, games=None, weeks=1, starts=None, results=None, names=None):
    """NHL abbrev -> sorted list of game dates (ISO) in the Monday-Sunday week(s) from monday (2 in a playoff round).
    games, if given, is filled with NHL abbrev -> [(date, opponent, home)]; starts with (NHL abbrev, date) -> the game's
    start time (UTC ISO), when the schedule gives one (findings 241); results with (home abbrev, date) -> its state and
    score, and names with abbrev -> team name, for the Predicted winners tab (findings 450)."""
    out = {}
    days = [d for k in range(weeks) for d in nhl_get(s, f"{NHL_WEB}/schedule/{(monday + timedelta(days=7 * k)).isoformat()}").json().get("gameWeek", [])]
    for day in days:
        for g in day.get("games", []):
            if g.get("gameType") == 2 and g.get("gameScheduleState", "OK") not in ("PPD", "CNCL"):  # postponed or cancelled: not played on this date (handoff 660)
                for side, other in (("awayTeam", "homeTeam"), ("homeTeam", "awayTeam")):
                    out.setdefault(g[side]["abbrev"], []).append(day["date"])
                    if games is not None:
                        games.setdefault(g[side]["abbrev"], []).append((day["date"], g[other]["abbrev"], side == "homeTeam"))
                    if starts is not None and g.get("startTimeUTC"):
                        starts[(g[side]["abbrev"], day["date"])] = g["startTimeUTC"]
                    if names is not None and (g[side].get("commonName") or {}).get("default"):
                        names[g[side]["abbrev"]] = g[side]["commonName"]["default"]
                if results is not None:
                    results[(g["homeTeam"]["abbrev"], day["date"])] = {
                        "state": g.get("gameState"), "home_score": g["homeTeam"].get("score"), "away_score": g["awayTeam"].get("score"),
                        "last": (g.get("gameOutcome") or {}).get("lastPeriodType")}
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
    except (requests.RequestException, KeyError, StopIteration, ValueError, TypeError) as e:  # a null field: handoff 508
        print(f"Standings unavailable, goalie starts not priced by schedule: {e!r}", file=sys.stderr)
        return {}


def team_xgd_rows(s, year):
    """{team: (xGF, xGA, games)} from MoneyPuck's team file, all situations; None if it can't be read."""
    try:
        lines = nhl_get(s, MONEYPUCK_TEAMS.format(year=year)).text.splitlines()
    except requests.RequestException as e:
        print(f"MoneyPuck teams {year} unavailable: {e}", file=sys.stderr)
        return None
    head = lines[0].split(",") if lines else []
    need = ("team", "situation", "games_played", "xGoalsFor", "xGoalsAgainst")
    if any(c not in head for c in need):
        print(f"MoneyPuck teams {year} returned no table", file=sys.stderr)
        return None
    ix = {c: head.index(c) for c in need}  # the file names "team" twice; the first is the abbreviation
    out = {}
    try:
        for line in lines[1:]:
            f = line.split(",")
            if len(f) >= len(head) and f[ix["situation"]] == "all":
                out[f[ix["team"]]] = tuple(float(f[ix[c]] or 0) for c in ("xGoalsFor", "xGoalsAgainst", "games_played"))
    except ValueError as e:
        print(f"MoneyPuck teams {year} unreadable: {e}", file=sys.stderr)
        return None
    return out


def team_xgd(s, year):
    """Expected-goal differential per game, shrunk over 15 games toward half of last season's, for the Predicted winners
    picks (handoff 449, findings 402). Empty when either season's file can't be read: the picks then use goal differential."""
    now, last = team_xgd_rows(s, year), team_xgd_rows(s, year - 1)
    if now is None or not last:
        return {}
    prior = {t: (f - a) / g for t, (f, a, g) in last.items() if g}
    return {t: (f - a + SCHED_SHRINK * 0.5 * prior.get(t, 0.0)) / (g + SCHED_SHRINK)
            for t, (f, a, g) in {**{t: (0.0, 0.0, 0.0) for t in prior}, **now}.items()}


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
        # the Season FP/G's boosted next-week TOI/G and PP TOI/G forecasts (handoff 668); None without both files
        tb = {k: season_boost.load(k) for k in ("toi", "pptoi")}
        self.toi_boost = tb if all(tb.values()) else None

    def season_range(self, season, is_d, gp_last, gp_before, age=None, gp_now=0):
        """80% range of the Season rating and his experience group (findings 345): rookies and second-year players finish
        further above their Season rating than veterans do. gp_last / gp_before: NHL games last season / two seasons ago; age in
        years (28+ is a veteran whatever his recent games: back from the AHL, Europe or injury). gp_now: his NHL games this
        season; late in the season both ends widen, and early on a low-tier player's low end comes in (handoff 619)."""
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
        ph = t.get("phase") or {}
        if ph and gp_now >= ph["late_gp"]:
            lo, hi = lo * ph["late_scale"], hi * ph["late_scale"]
        elif ph and 1 <= gp_now <= ph["early_gp"] and tier == "low":
            lo *= ph["early_low_tier_lo_scale"]
        elif ph and exp == "rookie" and ph.get("rookie_mid_gp", [0, -1])[0] <= gp_now <= ph.get("rookie_mid_gp", [0, -1])[1]:
            lo, hi = lo * ph["rookie_mid_scale"], hi * ph["rookie_mid_scale"]  # handoff 663
        return [round(max(season + lo, 0.0), 2), round(season + hi, 2)], exp

    def season_dress(self, x, shift=0.0):
        """Share of the team's remaining games he dresses (rest-of-season logistic, findings section 56)."""
        return 1 / (1 + math.exp(-self.season_dress_logit(x, shift)))

    def season_dress_logit(self, x, shift=0.0):
        m = self.season_lr
        z = m["intercept"] + sum(c * x[k] for k, c in m["coef"].items()) + shift
        if x.get("os_long"):
            z += DURABLE_OUT[0] * x["prev_dress_rate"] + DURABLE_OUT[1] * x["fpg"]
        stuck = min(x.get("gp_now", 0), 40) / 40
        if x.get("no_prev"):
            z += NEW_PLAYER_SEASON * stuck + FEW_GAMES_SEASON * (x.get("gp_now", 0) <= 5)
        elif x.get("prev_dress_rate", 1.0) < 0.5:
            z += PART_TIMER_SEASON * stuck
        return z

    def fpg(self, feats, season=False):
        """Points a game: the Week rating's ridge, or with season=True the Season rating's (no hinge curves, findings 320 (2);
        decomposed scoring forecasts, findings 320 (3); boosted ice-time forecasts, handoff 668)."""
        feats = {**feats, **forecast_features(feats, self.sk, self.toi_boost if season else None, season)}
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


def skater_features(games, prev, xg_now, xg_prev, team_games, is_d=False, p2=None, context=None, age=None, two_season=False):
    """Inputs for one skater. games: his games this season, oldest first. team_games: his team's game ids, oldest first.
    p2: his per-game rates two seasons ago (findings 310 (4)), None if he didn't play then. context: his lineup_context
    entry (None before his first game). age: his age in years on the matchup Monday, None if unknown. two_season: with no
    game yet, the this-season windows fall back to last season and two seasons ago, each game counting equally (the
    Week's FP/G, handoff 429), instead of last season alone."""
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
        if two_season and p2 and p2.get("gp") and prev.get("gp"):  # handoff 429: both seasons, GP-weighted
            both = lambda r: (prev[r] * prev["gp"] + p2[r] * p2["gp"]) / (prev["gp"] + p2["gp"]) \
                if prev.get(r) is not None and p2.get(r) is not None else prev.get(r)
            for r in TWO_SEASON_RATES:
                for w in ("l5", "l10", "l20", "std"):
                    f[f"{w}_{r}"] = both(r)
            f["last_toi"] = f["toi_l3"] = both("toi")
            f["pp_l1"] = f["pp_l3"] = both("pptoi")
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
    # why the note is long (handoff 657): "words" (season-level, surgery, indefinitely, ...), "ltir" when LTIR is the only
    # such word, "wtw" when week-to-week is the only one, "timeline" when only a stated 14+ days or "a few weeks"
    kinds = set()
    for m in [*LONG_ABSENCE.finditer(short), *LONG_OWN.finditer(mine)]:
        w = m.group(0).lower()
        kinds.add("wtw" if w == "week-to-week" else "ltir" if "injured reserve" in w or w == "ltir"
                  else "timeline" if w.endswith("weeks") else "unclear" if w.startswith("unclear") else "words")
    if left is not None and left >= 14:
        kinds.add("timeline")
    # "unclear when he'll return" is the only long reason: the Week reads the note as not long (hunt14/audit5, t_site_long3.py)
    k2 = kinds - {"unclear"}
    # LTIR wins over the other long words but surgery and the season, as the replay grouped it (hunt14/audit5, t_ltir_mix.py)
    surgery = bool(re.search(r"surgery|surgical|\boperation\b", short, re.I))
    why = None if not long_abs else "unclear" if not k2 and not season \
        else "words" if season or surgery else "ltir" if "ltir" in k2 \
        else "words" if "words" in k2 else "wtw" if "wtw" in k2 else "timeline"
    return {"long": long_abs, "season": season, "days_left": left, "long_why": why}


def note_words(note):
    """Illness and return words in ESPN's note (handoffs 676-677, hunt14/phrases/p10b_subject.py): (illness, return), each
    from the headline or his own sentences of the longer comment (naming him, no other "Name (injury)"). A match preceded by
    a negation or hedge ("not", "unclear", "hopes", "before", ...) doesn't count; a return match also needs his last name or
    "he" in the 70 characters before it, with no clause break."""
    short, long_ = note.get("short") or "", note.get("long") or ""
    last = (note.get("name") or "").split()[-1:]
    ln = last[0] if last else None
    long_ = long_.replace(short, "", 1).strip() if short else long_
    own = " ".join(x for x in re.split(r"(?<=[.!?])\s+", long_)
                   if ln and re.search(r"\b" + re.escape(ln) + r"\b", x) and not re.search(r"[A-Z][\w'.-]+ \([a-z -]+\)", x.replace(ln, "")))

    def hit(text, words, name=None):
        for m in words.finditer(text):
            pre = text[max(0, m.start() - 70):m.start()]
            if RETURN_NEG.search(pre[-50:]):
                continue
            if name and not re.search(r"(?:\b" + re.escape(name) + r"\b|\b[Hh]e\b)(?:'s)?(?: \([^)]*\))?[^.;,]{0,55}$", pre):
                continue
            return True
        return False
    ill = hit(short, ILLNESS_WORDS) or hit(own, ILLNESS_WORDS)
    ret = bool(ln) and not ill and (hit(short, RETURN_WORDS, ln) or hit(own, RETURN_WORDS, ln))
    return ill, ret


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


def suspension_caps(note, team_game_dates, n, starts=None):
    """Per-game dress caps for a suspended skater's next n team games (handoff 631), or None without a dated note. The
    team's played games after the listing count as served; a stated length N caps the games until N are served at 0 and
    the rest not at all, else game j since the listing takes SUSP_UNKNOWN_CAPS."""
    when = parse_utc((note or {}).get("date"))
    if when is None:
        return None
    m = SUSP_LEN.search(f"{note.get('short') or ''} {note.get('long') or ''}")
    k = (m.group(1) or m.group(2)).lower() if m else None
    length = (int(k) if k.isdigit() else NUM_WORDS[k]) if k else None
    day = when.astimezone(ET).date().isoformat()
    served = sum(game_after(when, day, d[:10], (starts or {}).get(d[:10])) for d in team_game_dates)
    return [(0.0 if served + i < length else 1.0) if length else SUSP_UNKNOWN_CAPS[min(served + i, len(SUSP_UNKNOWN_CAPS) - 1)]
            for i in range(n)]


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
    after = sorted(d[:10] for d in team_game_dates if game_after(when, day, d[:10], (starts or {}).get(d[:10])))
    if not after:
        return False
    # ESPN listed him OUT again after that game (the bare 'out' that carried this note is dated later): a new absence,
    # not the one game (handoff 616)
    listed = parse_utc(note.get("listed"))
    if listed is not None:
        start = parse_utc((starts or {}).get(after[0]))
        if (listed > start) if start is not None else listed.astimezone(ET).date().isoformat() > after[0]:
            return False
    return True


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
        notes[aid] = {**note, **{k: old.get(k) for k in ("date", "short", "long")}, "name": note.get("name") or old.get("name"),
                      "listed": note.get("date")}  # the bare word's own time (handoff 616)
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
    return {"INJURY_RESERVE": {"games": "10"}, "OUT": {"games": "2" if missed else "4"},  # OUT and missed: median 2 left (671)
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


def after_trip(venue, dates):
    """For each date: his team's first home game after WEEK_TRIP_MIN+ straight road games. venue: {date: home?} of the
    team's games played and scheduled (handoff 500)."""
    seq = sorted(venue.items())
    out = []
    for d in dates:
        road = 0
        for prev, home in reversed([x for x in seq if x[0] < d]):
            if home is not False:
                break
            road += 1
        out.append(venue.get(d) is True and road >= WEEK_TRIP_MIN)
    return out


def week_missing_regulars(prev, by_player, team_games, dfo_news, now_utc, now_team):
    """{(team, isD): expected players} for week_slack: last season's regulars with no game yet this season, on his current
    team (now_team), who are in recent Daily Faceoff news (handoff 510)."""
    floor = (now_utc - timedelta(days=WEEK_MISSING_NEWS_DAYS)).strftime("%Y-%m-%dT%H:%M")
    inj, mv = (dfo_news or {}).get("injuries") or {}, (dfo_news or {}).get("roster-moves") or {}
    out = {}
    for pid, pv in prev.items():
        team = now_team.get(pid)
        if not team or pv.get("season_label") or (pv.get("gp") or 0) < WEEK_MISSING_GP or by_player.get(pid) or pv.get("pos") == "G":
            continue
        ts = max((inj.get(str(pid)) or {}).get("ts", ""), (mv.get(str(pid)) or {}).get("ts", ""))
        tg = len(team_games.get(team, []))
        if ts < floor or tg < WEEK_MISSING_MIN_TG:
            continue
        isD = pv.get("pos") == "D"
        w = (pv.get("toi") or 0) / (20 if isD else 15) * WEEK_MISSING_A * WEEK_MISSING_K / (WEEK_MISSING_K + tg)
        out[(team, isD)] = out.get((team, isD), 0.0) + w
    return out


def week_slack(rows, boost_x, boost_week, missing=None):
    """Open lineup spots per team game in the Week (handoff 489, findings 402): per NHL team, position group and remaining
    game date, LINEUP_SPOTS minus the summed per-game Week chances of its skaters with games this season (team = his
    latest). Skaters not on the site count with the boosted next-week share on each of those dates. Each listed skater's
    chance for that game moves by WEEK_SLACK[0] x min(s, 0) + WEEK_SLACK[1] x max(s, 0) on the logit, s = open spots / 3,
    under his caps; chances at or under 0.001 stay. One pass, from the chances before this step; then games, the week's
    average, the next game and the Week rating follow."""
    sums, counts, seen = {}, {}, set()
    for r in rows:
        if r["pid"] in seen:
            continue
        seen.add(r["pid"])
        counts[(r["team"], r["isD"])] = counts.get((r["team"], r["isD"]), 0) + 1
        # home after a road trip, and a long absence, first (handoff 500): the sums below use these chances
        for j, q in enumerate(r["per_game"]):
            if q <= 0.001:
                continue
            dz = 0.0
            if r.get("after_trip", ())[j:j + 1] == [True]:
                dz += WEEK_HOME_AFTER_TRIP[0] if r.get("missed", 0) >= 1 else WEEK_HOME_AFTER_TRIP[1] * (1 - q)
            if r.get("missed", 0) >= WEEK_OUT_LONG_GAMES:
                dz += WEEK_OUT_LONG * (j + r.get("j0", 0))
            if dz:
                r["per_game"][j] = min(1 / (1 + math.exp(-(logit(q) + dz))), r["caps"][j])
        for d, q in zip(r["dates"], r["per_game"]):
            sums[(r["team"], r["isD"], d)] = sums.get((r["team"], r["isD"], d), 0.0) + q
        # his team's games still ahead that don't count for him (a free agent's game tonight after the first puck, a
        # waiver player's before he clears, a dropped player's after today) still take a lineup spot (hunt14/puck)
        for d, q, c, trip, j in r.get("others", ()):
            dz = 0.0
            if q > 0.001 and trip:
                dz += WEEK_HOME_AFTER_TRIP[0] if r.get("missed", 0) >= 1 else WEEK_HOME_AFTER_TRIP[1] * (1 - q)
            if q > 0.001 and r.get("missed", 0) >= WEEK_OUT_LONG_GAMES:
                dz += WEEK_OUT_LONG * j
            q = min(1 / (1 + math.exp(-(logit(q) + dz))), c) if dz else q
            sums[(r["team"], r["isD"], d)] = sums.get((r["team"], r["isD"], d), 0.0) + q
    if boost_week:
        dates = {}
        for team, isD, d in sums:
            dates.setdefault((team, isD), set()).add(d)
        for pid, x in boost_x.items():
            key = (x["team"], x["isD"] == 1.0)
            if pid in seen or key not in dates:
                continue
            b = min(max(season_boost.predict(boost_week, x), BOOST_WEEK_CLIP[0]), BOOST_WEEK_CLIP[1])
            counts[key] = counts.get(key, 0) + 1
            for d in dates[key]:
                sums[key + (d,)] += b
    for key in list(sums):  # regulars due back soon (handoff 510): in the sums, not the counts
        sums[key] += (missing or {}).get(key[:2], 0.0)
    for r in rows:
        p = r["p"]
        # handoffs 355 and 356, not in the sums above
        reg = (HEALTHY_REG_LOGIT if r.get("regular") else FIGHT_LOGIT if r.get("fought") else 0.0) \
            + ST_ROLE_LOGIT.get(r.get("st_role"), 0.0) + PP_SCRATCH_LOGIT * bool(r.get("pp_scratch")) \
            + DEPTH_RANK_LOGIT.get(r.get("toi_rank"), 0.0) \
            + NO_STATUS_OUT_LOGIT * bool(r.get("no_status_out")) + LOW_SCORER_LOGIT * bool(r.get("low_scorer"))  # 358-360, 623, 362
        # handoff 624: his team's next game only
        regs = [reg + (DTD_NEXT_LOGIT if j + r.get("j0", 0) == 0 and r.get("dtd_next") else 0.0)
                + (BEFORE_RETURN_LOGIT if j < len(r.get("before_return") or ()) and r["before_return"][j] else 0.0)  # handoff 670
                + RETURN_LOGIT * bool(r.get("return_words"))  # handoff 677
                for j in range(len(r["per_game"]))]
        # fewer players than spots means missing game logs, not open spots: leave that group's slack alone
        if counts[(r["team"], r["isD"])] < LINEUP_SPOTS[r["isD"]]:
            if any(regs):
                r["per_game"] = [q if q <= 0.001 else min(1 / (1 + math.exp(-(logit(q) + z))), c) for q, z, c in zip(r["per_game"], regs, r["caps"])]
            if r["per_game"] != r.get("before", r["per_game"]):  # ... but keep the trip and absence shifts
                p["cr_games"] = round(sum(r["per_game"]), 2)
                p["cr"] = round(p["cr_fpg"] * p["cr_games"], 2)
                p["cr_dress"] = round(sum(r["per_game"]) / len(r["per_game"]), 2)
                p["cr_dress_next"] = round(r["per_game"][0], 2)
            continue
        slack = [(LINEUP_SPOTS[r["isD"]] - sums[(r["team"], r["isD"], d)]) / 3 for d in r["dates"]]
        per_game = [q if q <= 0.001 else min(1 / (1 + math.exp(-(logit(q) + WEEK_SLACK[0] * min(s, 0) + WEEK_SLACK[1] * max(s, 0) + z))), c)
                    for q, s, z, c in zip(r["per_game"], slack, regs, r["caps"])]
        games = sum(per_game)
        p["cr_why"]["week_slack"] = round(3 * slack[0], 1)
        p["cr_games"] = round(games, 2)
        p["cr"] = round(p["cr_fpg"] * p["cr_games"], 2)
        p["cr_dress"] = round(games / len(per_game), 2)
        p["cr_dress_next"] = round(per_game[0], 2)


def lineup_slack(lineup, boost_x, boost_dress, model, missing=None):
    """Open lineup spots (findings 370, handoff 350): per NHL team and position group, LINEUP_SPOTS minus the summed Season
    dress shares of its skaters with games this season (team = his latest). Skaters not on the site count with the boosted
    model's share (within 0.015 of the final one on average). Each listed skater's logit then moves by
    a x max(slack, 0)/3 x (1 - p) + b x min(slack, 0)/3 x (1 - p): depth players gain when regulars are out, lose when the
    lineup is full. One pass, from the shares before this step; Season, Proj GP and the Season range follow."""
    sums, counts, seen = {}, {}, set()
    def add(key, share):
        sums[key] = sums.get(key, 0.0) + share
        counts[key] = counts.get(key, 0) + 1
    for e in lineup:
        if e["pid"] in seen:
            continue
        seen.add(e["pid"])
        add((e["team"], e["isD"]), e["share"])
    if boost_dress:
        for pid, x in boost_x.items():
            if pid not in seen:
                add((x["team"], x["isD"] == 1.0), min(max(season_boost.predict(boost_dress, x), 0.0), 1.0))
    for e in lineup:
        if not e["pts"]:
            continue
        back = (missing or {}).get(e["team"], 0.0)  # regulars yet to play this season (handoff 509)
        # fewer players than spots means missing game logs, not open spots: leave that group's slack alone
        slack = None if counts[(e["team"], e["isD"])] < LINEUP_SPOTS[e["isD"]] else LINEUP_SPOTS[e["isD"]] - sums[(e["team"], e["isD"])]
        # a skater Daily Faceoff reports sent down since his last game doesn't fill his team's open spots (handoff 420)
        if slack is not None and slack > 0 and "sent down" in (e["p"]["cr_why"].get("season_news") or ()):
            slack = None
        if slack is None and not back and not e.get("rookie_pick"):
            continue
        q = min(max(e["share"], 0.001), 0.999)
        z = math.log(q / (1 - q)) + back + ROOKIE_PICK_SEASON_LOGIT * bool(e.get("rookie_pick"))  # handoff 632
        if slack is not None:
            z += (LINEUP_SLACK[0] * max(slack, 0) + LINEUP_SLACK[1] * min(slack, 0)) / 3 * (1 - q)
        share = 1 / (1 + math.exp(-z))
        p, why = e["p"], e["p"]["cr_why"]
        if back:  # what the regulars yet to play cost his share, for the page (handoff 604)
            why["missing_regs_pp"] = round(share - 1 / (1 + math.exp(-(z - back))), 3)
        season = share * e["pts"]
        p["cr_season"] = round(season, 2)
        p["proj_gp"] = round(e["gp"] + share * e["left"])
        if slack is not None:
            why["lineup_slack"] = round(slack, 1)
        if back:
            why["missing_regs"] = round(back, 2)
        rng, _ = model.season_range(season, *e["range"])
        if rng:
            why["season_range"] = rng


def news_season_logit(news, nhl_id, last_game, out_games, gp_now, now_utc):
    """Season dress logit shift from Daily Faceoff news (findings 360 (1)) and its labels for the why panel. An item counts
    as since his last game when posted after 03:00 UTC on that game's date, as in the research's fit."""
    if not news or not nhl_id:
        return 0.0, []
    since = f"{last_game[:10]}T03:00" if last_game else ""
    inj, mv = (news.get("injuries") or {}).get(str(nhl_id)), (news.get("roster-moves") or {}).get(str(nhl_id))
    z, why = 0.0, []
    down = bool(mv and mv.get("down") and mv["ts"] > since and out_games >= 1)
    if down:
        z += NEWS_SENT_DOWN[0] + NEWS_SENT_DOWN[1] * math.log1p(out_games)
        why.append("sent down")
        if mv.get("cond"):
            z += NEWS_CONDITIONING_SEASON
            why.append("conditioning loan")
    elif mv and mv.get("up") and out_games == 0 \
            and mv["ts"] >= (now_utc - timedelta(days=NEWS_RECALL_DAYS)).strftime("%Y-%m-%dT%H:%M"):
        z += NEWS_CALLED_UP[0] + NEWS_CALLED_UP[1] * min(gp_now, 40) / 40
        why.append("called up")
    if inj and inj["ts"] > since:
        # the latest item since his last game, or any item since then with that wording (findings 371)
        flag = lambda k: inj.get(k) or inj.get(f"{k}_ts", "") > since
        if flag("season"):
            z += NEWS_SEASON_OUT
            why.append("season-ending")
        elif flag("indefinite"):
            z += NEWS_INDEFINITE
            why.append("indefinite")
        if inj.get("surgery"):
            z += NEWS_SURGERY
            why.append("surgery")
    return z, why


def season_drift(rows, model):
    """Handoff 405 (idea 1118, findings 400; hunt13/season/s26.py, drift.py): a league-wide shift in forward or defenseman
    scoring this season. Over skaters with DRIFT_MIN_GP+ games: e_std = season-to-date FP/G - Season FP/G (weight GP) and
    e_l10 = last-10 FP/G - Season FP/G (weight min(GP, 10)); for F and D, gap = the group's weighted mean minus everyone's,
    drift = (gap_std + gap_l10) / 2. Every skater in the group gets Season FP/G max(fp + DRIFT_W x drift, 0.05) before his
    share; Week untouched. rows: [{p, D, pts, share, fp, range, lineup}]."""
    sums = {}
    for r in rows:
        n = len(r["fp"])
        if n < DRIFT_MIN_GP:
            continue
        e_std, e_l10 = sum(r["fp"]) / n - r["pts"], sum(r["fp"][-10:]) / 10 - r["pts"]
        for key in (r["D"], "all"):
            t = sums.setdefault(key, [0.0, 0.0, 0.0, 0.0])
            t[0] += e_std * n; t[1] += n; t[2] += e_l10 * 10; t[3] += 10
    if "all" not in sums:
        return
    mean = lambda t, i: t[i] / t[i + 1] if t[i + 1] else 0.0
    drift = {g: (mean(sums[g], 0) - mean(sums["all"], 0) + mean(sums[g], 2) - mean(sums["all"], 2)) / 2 if g in sums else 0.0
             for g in (False, True)}
    for r in rows:
        if not drift[r["D"]]:
            continue
        p, why = r["p"], r["p"]["cr_why"]
        pts = max(r["pts"] + DRIFT_W * drift[r["D"]], 0.05)
        season = r["share"] * pts
        p["cr_season"] = round(season, 2)
        why["season_pts"] = round(pts, 2)
        why["drift"] = round(DRIFT_W * drift[r["D"]], 3)
        if r["lineup"]:
            r["lineup"]["pts"] = pts
        rng, _ = model.season_range(season, *r["range"])
        if rng:
            why["season_range"] = rng


def week_news_logit(news, nhl_id, last_game, sat_last, espn_status, game_dates, last_start=None):
    """Per-game Week dress logit shifts from Daily Faceoff items since his last game (posted after 03:00 UTC on its date, as
    in the research), whether he is practising (his OUT / IR cap becomes day-to-day's), and labels for the why panel.
    Out now (handoff 406): practising, skating, could-return, return / day-to-day / week-to-week, long wording and a stated
    timeline running 7+ days past the game, or already run out (428). Sent down by his latest roster move (407). Dressed last game, no ESPN status,
    and news since says he is out or day-to-day (408). "Since" = after that game's puck drop (last_start, UTC ISO), or 23:00
    UTC on its date when the schedule doesn't give it (handoff 438: a morning game-time decision before a game he played
    no longer counts)."""
    items = ((news or {}).get("recent") or {}).get(str(nhl_id)) if nhl_id and last_game else None
    since = (last_start or "").rstrip("Z")[:19] or (f"{last_game[:10]}T23:00" if last_game else "")
    items = [r for r in items or () if r["ts"] > since]
    inj = [r for r in items if r["cat"] == "inj"]
    moves = [r for r in items if r["cat"] == "move"]
    shifts, why, practising = [0.0] * len(game_dates), [], False
    any_f = lambda k: any(k in r["f"] for r in inj)
    z = 0.0
    if sat_last and inj:
        practising = any_f("full") or any_f("limited")
        if practising:
            z += WEEK_NEWS["practice"]; why.append("practising")
        elif any_f("skate"):
            z += WEEK_NEWS["skate"]; why.append("skating")
        if any_f("gtd"):
            z += WEEK_NEWS["gtd"]; why.append("could return")
        if "ret" in inj[-1]["f"]:
            z += WEEK_NEWS["ret"]; why.append("set to return")
        if any_f("dtd"):
            z += WEEK_NEWS["dtd"]; why.append("day-to-day")
        if any_f("wtw"):
            z += WEEK_NEWS["wtw"]; why.append("week-to-week")
        if any_f("season") or any_f("indefinite") or any_f("ltir") or any_f("mtm"):
            z += WEEK_NEWS["long"]; why.append("long absence")
        tl = [r["tl"] for r in inj if r.get("tl") is not None]
        if tl:
            first = date.fromisoformat(inj[0]["ts"][:10])
            for k, d in enumerate(game_dates):  # timeline left at the game: 7+ days, or run out (handoff 428)
                rem = max(tl) - (date.fromisoformat(d) - first).days
                shifts[k] += WEEK_NEWS["timeline"] if rem > WEEK_NEWS_TIMELINE_DAYS else WEEK_NEWS["timeline_up"] if rem <= 0 else 0.0
            if any(x < 0 for x in shifts):
                why.append("timeline")
            if any(x > 0 for x in shifts):
                why.append("timeline up")
    down = 0.0
    if moves and "down" in moves[-1]["f"] and "up" not in moves[-1]["f"]:
        down = WEEK_NEWS["down_out" if sat_last else "down_in"]; why.append("sent down")
    elif not sat_last and not espn_status and inj:
        if any_f("out"):
            z += WEEK_NEWS["hurt_out"]; why.append("hurt since")
        elif any_f("dtd"):
            z += WEEK_NEWS["hurt_dtd"]; why.append("day-to-day since")
    return [x + z for x in shifts], down, practising, why


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
             gp_now=len(games), games_left=left, os_long=float(k >= 6), os_raw=min(k, 40), os_k=k, mk_logit=math.log(share / (1 - share)), mk_p_out=p_out)
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


def forecast_features(f, sk, toi_boost=None, season=False):
    """Ridge inputs built on the others (same arithmetic as model/train.py): next week's TOI and PP TOI forecast, the
    usage x efficiency inputs with the forecasts in place of last-5 ice time (findings 310 (1)), and the products of the
    12 strongest inputs, each missing factor filled with its training mean (findings 310 (2)). The Week's forecast ridges
    have wider inputs (handoff 669); season=True uses the Season ridge's narrower ones. toi_boost (the Season rating only,
    handoff 668): {"toi": model, "pptoi": model}, boosted trees (model/train_toi_boost.py) that read f plus the inputs
    above built on the narrower linear forecasts (no hinges or products) and his team's games this week
    (f["team_games"]); their forecasts then replace the linear ones in the fc_ inputs."""
    out = week_extras(f)
    fc = ((sk.get("season") or {}).get("toi_forecast") if season else None) or sk.get("toi_forecast")
    if fc:
        x = dict(f, gpw=min((f.get("std_gp") or 0) / 20, 1)) | context_gaps(f)
        if any(k.endswith("_sq") for m in fc.values() for k in m["inputs"]):  # the wider ridges (handoff 669)
            x |= {f"{c}_sq": None if f.get(c) is None else f[c] ** 2 for c in FC_WIDE_SQ}
            x |= {f"D_{c}": (f.get("is_D") or 0.0) * (f.get(c) or 0.0) for c in FC_WIDE_D}
            x["age_m"] = 27.0 if f.get("age_m") is None else f["age_m"]
            x["prev_gp"] = None if f.get("no_prev") else f.get("prev_gp")  # no last season: the training mean, as trained
        val = {tgt: m["intercept"] + sum(c * (m["fill"][k] if x.get(k) is None else x[k]) for k, c in zip(m["inputs"], m["coef"]))
               for tgt, m in fc.items()}
        out |= fc_inputs(f, val, sk)
        if toi_boost:
            xb = {**f, **out}
            out |= fc_inputs(f, {"y_toi": season_boost.predict(toi_boost["toi"], xb),
                                 "y_pptoi": season_boost.predict(toi_boost["pptoi"], xb)}, sk)
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


FC_WIDE_SQ = ["std_toi", "l5_toi", "l10_toi", "last_toi", "std_pptoi", "l5_pptoi", "pp_l1"]  # model/train.py (handoff 669)
FC_WIDE_D = ["std_toi", "l5_toi", "l10_toi", "last_toi"]


def fc_inputs(f, val, sk):
    """The ice-time forecasts val and the usage x efficiency inputs with them in place of last-5 ice time and PP time."""
    u = ux_features(dict(f, l5_toi=val["y_toi"], l5_pptoi=max(val["y_pptoi"], 0.0)), "D" if f.get("is_D") else "F", sk.get("ux_means"))
    return {f"fc_{k}": v for k, v in u.items()} | {"fc_toi": val["y_toi"], "fc_pptoi": val["y_pptoi"]}


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
                  injury=None, last_start=None, movers=(), relief=(), season_out=(), by_date=None, save_pcts=None, pulled=(), road=(),
                  opp=None, nonwins=None, start_wins=None, likely_dfo=None, ages=None, team_left=0, dfo_inj=None):
    """Expected starts in the rest of this week for each goalie on the team.

    starts_by_team: this season's starter per team game, oldest first, as (date, goalie id).
    confirmed: {date: goalie id} of Daily Faceoff confirmed starters; those games count as 1 for him, 0 for the others.
    today_p: if given, filled with each goalie's chance of starting today's game.
    season_share: if given, filled with each goalie's long-run share of team starts (this season's starts
    blended with last season's split over SEASON_SHARE_K games), for the Season rating.
    injury: {goalie id: ESPN injury factor}; scales his chance before each game is split among the team's goalies,
    so an injured starter's games go to his partner (findings section 48).
    dfo_inj: {goalie id: (next game, later games)} caps on that factor from Daily Faceoff injury news (handoff 679).
    last_start: (goalie id, (shift next game, shift game after next, ...)) for whoever started the team's last game:
    his start logit drops after a bad start or a loss and rises after a win (findings 131-133); after he left without
    a decision it stays down for every game in the window, until a Daily Faceoff confirmation names him (findings 164).
    movers: goalies who started for another NHL team this season; with 1-3 starts for this team their logit rises,
    and their season_share counts only this team's games since their first start for it.
    relief: goalies who came on in relief in the team's last game; their start logit rises (findings 170).
    season_out: goalies ESPN says are out for the season: no season_share, and theirs goes to the others (findings 216, 234).
    by_date: if given, filled with {goalie id: {date: chance he starts}} for each game from today on (findings 236).
    pulled: goalies pulled from their latest start; their season_share drops PULLED_SEASON unless the no-decision cut
    already applies (findings 330 (3)).
    road: dates of the team's road games this week (findings 257).
    opp: {date: opponent strength} (opponent_strength); nonwins: {goalie id: straight starts without a win}. With nonwins
    given, the no. 1's logit moves by opponent, form and days of rest (findings 262-263).
    start_wins: whether each start in starts_by_team[team] was a win; with it the no. 1's next game moves after a split
    back-to-back and after a backup's last result (findings 264-267)."""
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
                # a healthy goalie keeps at least min(his share, GOALIE_SEASON_FLOOR): a weak save% can't zero a working
                # backup (handoff 446, Spencer Martin NYR)
                floor = min(season_share[gid], GOALIE_SEASON_FLOOR) if injury.get(gid, 1.0) > 0 else 0.0
                season_share[gid] = min(max(season_share[gid] + SAVE_GAP_SEASON[0] + SAVE_GAP_SEASON[1] * gap, floor), 1.0)
        # older goalies lose share with games left, not handed to partners (handoff 640); ages: {id: years}. A healthy
        # one keeps the same floor as the save% term, so a working backup the Week expects to start isn't zeroed (rule G4)
        for gid in cands:
            if (ages or {}).get(gid) is not None:
                floor = min(season_share[gid], GOALIE_SEASON_FLOOR) if injury.get(gid, 1.0) > 0 else 0.0
                season_share[gid] = max(season_share[gid] - GOALIE_AGE_SEASON * (team_left / 82) * max(ages[gid] - 30, 0), floor)
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
        # a healthy taker with any share weighs at least GOALIE_TAKER_MIN (handoff 446)
        wt = {g: season_share[g] * INJURED_TAKER_WEIGHT if injury.get(g, 1.0) <= 0
              else max(season_share[g], GOALIE_TAKER_MIN) if season_share[g] > 0 else 0.0 for g in takers}
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
    # an even tandem (top two within one start of the last 20): the no. 1's back-to-back shifts at half size (handoff 451)
    top2 = sorted((recent20.count(g) for g in set(recent20)), reverse=True) + [0, 0]
    b2b_scale = EVEN_TANDEM_B2B_SCALE if recent20 and top2[0] - top2[1] <= 1 else 1.0
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
                # the pair's other night already confirmed by Daily Faceoff: the rule's guess about it is settled (handoff 447)
                if n1 and nxt not in confirmed:  # every night 1 still ahead this week, not only the next game (findings 277, handoff 339)
                    z += B2B_AHEAD_NIGHT1 * b2b_scale
                if n2 and prv[-1] not in confirmed:
                    z += (B2B_AHEAD_NIGHT2_NEXT if ahead == 1 else B2B_AHEAD_NIGHT2_LATER) * b2b_scale
                if not n1 and not n2 and not (ahead == 0 and gap == 1):
                    z += NO1_ORDINARY_LOGIT
                if ahead == 0 and gap == 2 and day.weekday() in (6, 0):
                    z += SUNMON_REST1_NO1_LOGIT
                elif ahead == 0 and d in road and gap != 1:
                    z += ROAD_NO1_LOGIT
            if gid == no1 and nonwins is not None:
                rest = gap if ahead == 0 else (day - prev_d).days if prev_d else 0
                form = NO1_NONWIN[0] + NO1_NONWIN[1] * min(nonwins.get(gid, 0), NO1_NONWIN_CAP)
                z += NO1_OPP_LOGIT * (opp or {}).get(d, 0.0) + form * (1.0 if ahead == 0 else NO1_NONWIN_LATER) \
                    + (NO1_REST[1] if rest >= NO1_REST[0] else 0.0)
            if gid == no1 and ahead == 0 and start_wins is not None and len(start_wins) == len(seq) >= 2:
                backup = [i for i, x in enumerate(seq) if x != no1 and x in cands]
                split = (log[-1][0] - log[-2][0]).days == 1 and gap == 2 and (seq[-1] == no1) != (seq[-2] == no1)
                z += NO1_SPLIT_B2B * split \
                    + NO1_AFTER_BACKUP_LOSS * bool(backup and backup[-1] == len(seq) - 1 and not start_wins[-1]) \
                    + NO1_AFTER_BACKUP_WIN * bool(backup and 2 <= len(seq) - backup[-1] <= 3 and start_wins[backup[-1]])
            if ahead == 0 and gid in idle3:
                z += THREE_GOALIE_IDLE_LOGIT
            # three or more goalies started the last 10: the no. 1 gets more of the games after the next (handoff 626; the
            # next game is the idle cut's)
            if ahead >= 1 and gid == no1 and len(set(seq[-10:])) >= 3:
                z += THREE_GOALIE_NO1_LOGIT
            if seq and gid == seq[-1] and (rotating and ahead == 2 or plain and ahead == 1):
                z += ROTATION_THIRD_LOGIT if rotating else PLAIN_SECOND_LOGIT
            if gid in relieved:
                z += RELIEF_LOGITS[min(ahead, 1)] + relieved[gid]
            if len(seq) < EARLY_SHARE_GAMES and gid not in seq:  # no start for this team yet, early season (handoff 450)
                z += EARLY_NO_START_LOGIT
            if z and 0 < raw[gid] < 1:
                raw[gid] = 1 / (1 + math.exp(-(math.log(raw[gid] / (1 - raw[gid])) + z)))
            # Daily Faceoff injury news: the factor is min(ESPN's, 0.2 next game, 0.35 later) (handoff 679)
            cut = (dfo_inj or {}).get(gid)
            raw[gid] *= min(injury.get(gid, 1.0), cut[min(ahead, 1)]) if cut else injury.get(gid, 1.0)
        tot = sum(raw.values()) or 1
        p = {gid: v / tot for gid, v in raw.items()}
        early = B2B_NIGHT2_NO1_EARLY if night2_no1 and len(seq) <= EARLY_GAMES else 0.0
        if early and 0 < p.get(seq[-1], 0) < 1:
            was = p[seq[-1]]
            now = 1 / (1 + math.exp(-(math.log(was / (1 - was)) + early)))
            p = {gid: now if gid == seq[-1] else v * (1 - now) / (1 - was) for gid, v in p.items()}
        ahead += 1
        prev_day, prev_d = prev_d, day
        q = p
        if ahead > 1:  # games after the next one are softened and split across the team's goalies again; the chain keeps p
            q = {gid: v if v <= 0 or v >= 1 else 1 / (1 + math.exp(-LATER_GAME_TEMP * math.log(v / (1 - v)))) for gid, v in p.items()}
            tot = sum(q.values()) or 1
            q = {gid: v / tot for gid, v in q.items()}
        night2 = (ahead == 1 and gap == 1) or (ahead >= 2 and prev_day is not None and (day - prev_day).days == 1)
        if no1 in q and 0 < q[no1] < 1 and not night2:  # handoff 363
            was = q[no1]
            now = 1 / (1 + math.exp(-(math.log(was / (1 - was)) + RESTED_NO1_LOGIT)))
            q = {gid: now if gid == no1 else v * (1 - now) / (1 - was) for gid, v in q.items()}
        if seq and seq[-1] != no1 and no1 in q and 0 < q[no1] < 1:  # handoff 364
            was = q[no1]
            now = 1 / (1 + math.exp(-(math.log(was / (1 - was)) + NO1_AFTER_OTHER_LOGIT)))
            q = {gid: now if gid == no1 else v * (1 - now) / (1 - was) for gid, v in q.items()}
        lk = (likely_dfo or {}).get(d)
        if lk in q and q[lk] < LIKELY_START_FLOOR:
            # Daily Faceoff's "Likely" starter: at least LIKELY_START_FLOOR for that game, the rest split by the model's
            # odds; later games and the chain unchanged (handoff 617)
            rest = sum(v for gid, v in q.items() if gid != lk)
            q = {gid: LIKELY_START_FLOOR if gid == lk else (v / rest * (1 - LIKELY_START_FLOOR) if rest else 0.0)
                 for gid, v in q.items()}
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
        except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError):  # a line with no start time (handoff 661)
            continue
        out[g["home"]["team"]], out[g["away"]["team"]] = ph, 1 - ph
    return out


def predicted_games(players, by_player, prev, rosters, team_games, team_dates, sched, goalie_nights, save_pcts, strength,
                    week_games, week_starts, week_results, team_names, lines, now_utc, xgd=None, line_date=None):
    """The week's NHL games with the home side's win chance, rolled up from the player projections (findings 450):
    each skater's points per game x his chance to dress (the site's own, or DEPTH_DRESS for depth players it doesn't rate),
    the starter's save% weighted by each goalie's chance to start, goal and expected-goal differential and back-to-backs."""
    rate, depth = winners_model.skater_rates(by_player, prev)
    dress = {}
    for p in players:
        if p.get("nhl_id") and p["pos"] != "G":
            d = p.get("cr_dress") if p.get("cr_dress") is not None else p.get("cr_dress_next")
            if d is not None:
                dress[p["nhl_id"]] = d
    sv, lg = save_pcts
    lineups, goalie_sv = {}, {"_league": lg}
    for team, r in rosters.items():
        skaters = [(pid, "D" if pos == "D" else "F") for pid, _, pos in r if pos != "G"]
        for pid, _ in skaters:
            if pid not in dress:
                dress[pid] = winners_model.depth_dress(by_player.get(pid), team_games.get(team))
        lineups[team] = winners_model.lineup_points(skaters, rate, dress, depth)
        by_day = {}
        for pid, _, pos in r:
            for d, q in (goalie_nights.get(pid) or {}).items():
                t = by_day.setdefault(d, [0.0, 0.0])
                t[0] += q * sv.get(pid, lg); t[1] += q
        goalie_sv[team] = {d: a / b for d, (a, b) in by_day.items() if b > 0}
    play_dates = {t: set(team_dates.get(t, ())) | set(sched.get(t, ())) for t in set(team_dates) | set(sched)}
    return winners_model.nhl_games(week_games, week_starts, week_results, team_names, strength, play_dates, lineups,
                                   goalie_sv, lines, now_utc, xgd, line_date)


def add_ratings(players, espn_season, today, monday, starters=(), odds=None, injuries=None, weeks=1, reschedule=None,
                dfo_news=None, winners=None):
    """Add Claude Rating fields to each player dict from fetch.py (in place).

    starters: Daily Faceoff starting-goalie pages (leads.fetch_starters) for today and later this week.
    odds: today's betting lines (leads.fetch_odds); they price tonight's goalie starts (research section 30).
    injuries: ESPN's injury notes (leads.fetch_injuries); a long absence takes a skater's week to 0 (findings section 58).
    reschedule(p, nhl team): fetch.py's, gives a player his NHL team's games: one ESPN lists as 'FA' (findings 230), or one
    the NHL moved before ESPN did (handoff 501).
    dfo_news: Daily Faceoff injury and roster-move news (leads.fetch_news), for the Season rating (findings 360 (1)).
    winners: if given, filled with {"games": [...]}: the week's NHL games with each one's win chance (findings 450)."""
    notes = {i["athlete_id"]: i for i in injuries or [] if i.get("athlete_id")}
    now_utc = utcnow()
    if dfo_news and (dfo_news.get("fetched") or "") < (now_utc - timedelta(days=NEWS_STALE_DAYS)).strftime("%Y-%m-%dT%H:%MZ"):
        dfo_news = None
    flagged = recently_flagged(today)  # read before this run rewrites the log
    stored_notes = load_injury_notes()
    s = requests.Session()
    if hasattr(s, "mount"):  # enough pooled connections for the loads below running side by side
        s.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=32))
    season, prev_season = season_ids(espn_season)
    model = Model()
    week_end = (monday + timedelta(days=7)).isoformat()  # sched covers 2 weeks in a playoff round
    week_games, week_starts, week_results, team_names = {}, {}, {}, {}
    bios = {}  # {id: (birth, draft, first season)}, filled by load_birthdates for the boosted models
    # the feeds don't depend on each other: load them side by side (each still loads as it did one by one)
    games, goalie_games, prev, prev2, prev_goalies, xg_now, xg_prev, births, sched, strength, prev_pct, xgd, opp_ga, g_starts3 = pmap(lambda f: f(), [
        lambda: load_skater_games(s, season, today),
        lambda: load_goalie_games(s, season, today),
        lambda: load_prev_season(s, prev_season),
        lambda: load_prev_season(s, prev_season - 10001),  # two seasons ago, a ridge input (findings 310 (4))
        lambda: load_prev_goalie_season(s, prev_season),
        lambda: load_xg(s, season // 10000),
        lambda: load_xg(s, prev_season // 10000),
        lambda: load_birthdates(s, (season, prev_season), bios),
        lambda: week_schedule(s, monday, week_games, weeks, week_starts, week_results, team_names),
        lambda: team_strength(s, prev_season),
        lambda: prev_points_pct(s, prev_season),
        lambda: team_xgd(s, season // 10000) if winners is not None else {},
        lambda: opp_last_ga(s, prev_season),
        lambda: load_goalie_starts(s, (prev_season, prev_season - 10001, prev_season - 20002)),  # handoff 641
    ], workers=4)
    prev_gs = {gid: t[0] for gid, t in prev_goalies.items()}
    save_pcts = shrunk_save_pcts(goalie_games, prev_goalies)
    opp_str, nonwins = opponent_strength(goalie_games, prev_pct), nonwin_streaks(goalie_games)
    forms = team_form(goalie_games)  # streaks and last margin, for the lineup shifts (handoffs 340-342)
    news = load_news()

    # team game order this season (every game has goalie rows) and the starter of each
    team_games, starts_by_team, last_start, start_wins = {}, {}, {}, {}
    for g in sorted(goalie_games, key=lambda g: g["date"]):
        if g["game"] not in team_games.setdefault(g["team"], []):
            team_games[g["team"]].append(g["game"])
        if g["started"]:
            starts_by_team.setdefault(g["team"], []).append((g["date"], g["id"]))
            start_wins.setdefault(g["team"], []).append(bool(g["wins"]))
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
    # the Season's pts/start: the same blend, shrunk by his last three seasons' starts (handoff 641)
    season_k = lambda gid: next(k for top, k in SEASON_PPS_K if g_starts3.get(gid, 0) < top)
    pps_season = {gid: (fp + prior.get(gid, base) * season_k(gid)) / (n + season_k(gid)) for gid, (fp, n) in own.items()}
    # the league's points per start this season, for this week's pricing (findings 231 (8))
    league_pps = (sum(fp for fp, _ in own.values()) + base * LEAGUE_PPS_PRIOR) / (sum(n for _, n in own.values()) + LEAGUE_PPS_PRIOR)
    by_player = {}
    for g in sorted(games, key=lambda g: g["date"]):
        by_player.setdefault(g["id"], []).append(g)
    context = lineup_context(by_player, team_games)  # who missed each team's last game (findings 320 (1))
    left_teammates = left_early_teammates(by_player, team_games)  # for the scratches who fill in (handoff 352)
    fewest = lowest_toi(by_player, team_games)  # for the depth skaters' minutes rank (handoff 360)
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
    # the boosted Season models (findings 360 (3)-(4)) read last season as it was, before the stand-in below
    boost_dress, boost_fpg, boost_week = season_boost.load("dress"), season_boost.load("fpg"), season_boost.load("week")
    boost_x = season_boost.features(by_player, team_games, prev, prev2, bios, xg_prev, season, today, SEASON_GAMES) \
        if boost_dress or boost_fpg or boost_week else {}
    if boost_week:  # the Week model also reads his latest Daily Faceoff injury item (handoff 413)
        for pid, x in boost_x.items():
            gl_ = by_player.get(pid) or []
            x.update(season_boost.news_inputs(((dfo_news or {}).get("injuries") or {}).get(str(pid)),
                                              max(g["date"] for g in gl_) if gl_ else None, now_utc))
    teammates = teammate_inputs(by_player, team_games, boost_x, boost_week)  # the forecast ridges' (handoff 678)
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
    confirmed, tonight, likely = {}, {}, {}
    for page in starters:
        for g in page.get("games", []):
            cands = [pid for pid, (t, pos) in index.get(norm(g.get("goalie") or ""), {}).items() if pos == "G" and t == g.get("team")] \
                or [pid for pid, (t, pos) in nickname_cands(index, g.get("goalie"), g.get("team")).items() if pos == "G"] \
                or [pid for pid in index.get(norm(g.get("goalie") or ""), {})
                    if pid in {q for q, _, qpos in rosters.get(g.get("team"), ()) if qpos == "G"}]
            # ... "Matthew" Murray (findings 175); the last: traded or claimed, no game for his new team yet (handoff 454)
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
            elif g.get("status") == "Likely":  # handoff 617
                likely.setdefault(g["team"], {})[page["date"]] = cands[0]
    # ESPN lists him as a free agent ('FA') but he has played this season and an NHL roster has him: his team's games
    # (Bunting, findings 230)
    roster_team = {pid: team for team, r in rosters.items() for pid, _, _ in r}
    played_for = {}  # (date, team) of every NHL game this season, skaters' and goalies'
    for pid, gl in by_player.items():
        played_for.setdefault(pid, []).extend((g["date"][:10], g["team"]) for g in gl)
    for g in goalie_games:
        played_for.setdefault(g["id"], []).append((g["date"][:10], g["team"]))
    for p in players:
        if not reschedule:
            break
        pid = match(p)
        if p["pos"] != "G" and p.get("team") == "FA":
            if pid in by_player and roster_team.get(pid):
                reschedule(p, roster_team[pid])
        elif pid in played_for and p.get("team") != "FA" and p.get("owner") != "waivers":
            # the NHL moved him before ESPN did: his latest game is for a new team, after games for the one ESPN lists.
            # He plays the new team's games from now on (handoff 501)
            espn = ESPN_TO_NHL.get(p["team"], p["team"])
            last = max(played_for[pid])[1]
            if last != espn and any(t == espn for _, t in played_for[pid]) and roster_team.get(pid, last) == last:
                reschedule(p, last)
    # a goalie ESPN lists on another team than the NHL roster's (no game for the new one yet): his starts come from the
    # team whose games he is given, so his starts never outnumber his games (handoff 501: P3, G1 and G6 blocked publishing)
    for p in players:
        pid = match(p) if p["pos"] == "G" and p.get("team") != "FA" else None
        espn = ESPN_TO_NHL.get(p["team"], p["team"]) if pid else None
        if pid and roster_team.get(pid) and roster_team[pid] != espn and espn in (set(sched) | set(team_games)):
            entry = next(e for e in rosters[roster_team[pid]] if e[0] == pid)
            rosters[roster_team[pid]] = [e for e in rosters[roster_team[pid]] if e[0] != pid]
            rosters.setdefault(espn, []).append(entry)
            roster_team[pid] = espn
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
    # Daily Faceoff injury news since his last appearance (handoff 679): posted after 20:00 UTC on that date, at most 14 days old
    goalie_dfo, goalie_dfo_day = {}, {}
    last_app = {gid: max(d for d, _ in v) for gid, v in goalie_dates.items()}
    oldest = f"{today - timedelta(days=GOALIE_DFO_INJ_DAYS)}T13:00:00"
    for team_r in rosters.values():
        for gid, _, pos in team_r:
            items = ((dfo_news or {}).get("recent") or {}).get(str(gid)) or ()
            since = max(f"{last_app[gid]}T20:00:00" if gid in last_app else "", oldest)
            inj = [r for r in items if r.get("cat") == "inj" and r["ts"] > since]
            if pos == "G" and inj and not {"ret", "full"} & set(inj[-1].get("f") or ()):
                goalie_dfo[gid] = GOALIE_DFO_INJ_CAPS
                goalie_dfo_day[gid] = inj[-1]["ts"][:10]  # for his details (handoff 695)
    goalie_exp, goalie_today, goalie_season, goalie_nights = {}, {}, {}, {}
    lines = line_win_probs(odds, today)
    g_ages = {gid: (today - b).days / 365.25 for gid, b in births.items() if b}  # the goalies' age cut (handoff 640)
    # every team, not just those with a game this week: no game this week keeps the Season rating (findings 230)
    for team in sorted(set(sched) | set(team_games)):
        dates = sched.get(team, [])
        roster_g = [pid for pid, _, pos in rosters.get(team, []) if pos == "G"]
        tod, sea, nights = {}, {}, {}
        exp = goalie_starts(model, team, roster_g, starts_by_team, prev_gs, dates, today, confirmed.get(team), tod, sea, goalie_inj,
                            last_start.get(team), movers, relief.get(team, ()), goalie_gone, nights, save_pcts, pulled,
                            {d for d, _, home in week_games.get(team, []) if not home},
                            {d: opp_str.get(o, 0.0) for d, o, _ in week_games.get(team, [])}, nonwins, start_wins.get(team, []),
                            likely.get(team), g_ages, max(SEASON_GAMES - len(team_games.get(team, [])), 0), goalie_dfo)
        # a goalie gets his numbers from the team whose roster lists him, not whichever team ran last (findings 160)
        mine = set(roster_g) | set((confirmed.get(team) or {}).values())
        # (decided once: deciding per output let the first one claim the goalie and dropped his Season, findings 230)
        kept = {gid for gid in set(exp) | set(sea) if (gid in mine if roster_g else gid not in goalie_exp)}
        for src, dst in ((nights, goalie_nights), (exp, goalie_exp), (tod, goalie_today), (sea, goalie_season)):
            dst.update({gid: v for gid, v in src.items() if gid in kept})

    unmatched = 0
    lineup = []  # skaters with games this season, for the lineup-slot step (findings 370)
    week_rows = []  # skaters' per-game Week chances by date, for the per-game lineup step (handoff 489)
    venue = {}  # NHL team: {date: home?} for its games played (goalie logs) and this week's schedule (handoff 500)
    for g in goalie_games:
        if g.get("home") is not None:
            venue.setdefault(g["team"], {})[g["date"][:10]] = g["home"]
    for team, gs in week_games.items():
        for d, _, home in gs:
            venue.setdefault(team, {})[d] = home
    drift_rows = []  # every skater's Season FP/G, for the league-wide scoring drift (handoff 405)
    season_open = min((g["date"] for g in games), default=None)
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
            # or he's on waivers until a later day, so today's game isn't his: its start comes off before the waiver
            # scaling below (a confirmed starter on waivers put his team's goalies above its games)
            started = started or (p.get("owner") == "waivers" and not p.get("today_start") and today.isoformat() in sched.get(team, []))
            if started:
                starts = max(0.0, starts - goalie_today.get(pid, 0.0)) if pid else 0.0
            # on waivers he misses his team's games before he clears (findings 122): keep that share of the starts
            left_dates = [d for d in sched.get(team, []) if d > today.isoformat() or (d == today.isoformat() and not started)]
            team_left = len(left_dates)
            # on waivers, only the nights after he clears are his (handoff 492)
            playable = (set(left_dates[max(team_left - p["games_left"], 0):]) if p["games_left"] else set()) \
                if p.get("owner") == "waivers" else None
            if playable is not None and team_left > p["games_left"]:
                # his share of the starts on those nights, by each night's start chance
                by_night = goalie_nights.get(pid, {}) if pid else {}
                total = sum(by_night.get(d, 0.0) for d in left_dates)
                starts *= (sum(by_night.get(d, 0.0) for d in playable) / total) if total > 0 else p["games_left"] / team_left
            # dropped after today's first puck: his team keeps him only until ESPN's overnight rollover (findings 241 (3))
            if p.get("pending_drop"):
                starts = min(starts, goalie_today.get(pid, 0.0) if pid and p["games_left"] else 0.0)
            if n.get("apply") and n.get("starts") is not None:
                starts = max(0.0, min(float(n["starts"]), p["games_left"]))
            fpg = pps.get(pid, prior.get(pid, base)) if pid else base
            why["own_pps"] = round(fpg, 2)  # his own, before tonight's line and the schedule (findings 232)
            # changed teams since last season: the last-season part of his Season pts/start moves by the teams' points% gap
            # (findings 330 (5))
            old_team = prev_goalies[pid][4] if pid in prior else None
            fpg_own_s = pps_season.get(pid, prior.get(pid, base)) if pid else base  # handoff 641
            if old_team and old_team != team and old_team in prev_pct and team in prev_pct:
                fpg_s = fpg_own_s + MOVER_PPS_BETA * (prev_pct[team] - prev_pct[old_team]) * PPS_PRIOR / (own.get(pid, (0.0, 0))[1] + PPS_PRIOR)
                why["mover_pps"] = round(fpg_s, 2)
            else:
                fpg_s = fpg_own_s
            raw_season = goalie_season.get(pid, 0.0) * fpg_s if pid else 0.0
            if pid:
                why["g_season_share"] = round(goalie_season.get(pid, 0.0), 2)  # for the page's Season line (handoff 605)
                if pid in goalie_dfo_day:  # Daily Faceoff injury news cut his starts (handoffs 679, 695)
                    why["dfo_inj"] = goalie_dfo_day[pid]
                why["season_pps"] = round(fpg_s, 2)  # the Season's pts/start (handoff 641)
            season = GOALIE_SEASON_SCALE[0] + GOALIE_SEASON_SCALE[1] * raw_season if raw_season > 0 else 0.0
            # Proj GP: NHL starts so far + his share of his team's starts still to play (the Season rating's share)
            proj_gp = (own.get(pid, [0, 0])[1] if pid else 0) \
                + (goalie_season.get(pid, 0.0) if pid else 0.0) * max(SEASON_GAMES - len(team_games.get(team, [])), 0)
            # start chance per game from today, for the page's open-slot count (findings 236)
            p["cr_g_nights"] = {d: round(q, 2) for d, q in sorted(goalie_nights.get(pid, {}).items())
                                if (not p.get("pending_drop") or d <= today.isoformat()) and (playable is None or d in playable)} if pid else {}
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
            f.update(ux_features(f, "D" if p["pos"] == "D" else "F", model.sk.get("ux_means")), **teammates.get(pid, {}))
            # his team's games this Monday-Sunday week, an input of the Season's boosted ice-time forecasts (handoff 668)
            f["team_games"] = sum(d < week_end for d in sched.get(team, []))
            fpg = model.fpg(f) if pid else model.sk["replacement_fpg"]["D" if p["pos"] == "D" else "F"]
            fpg_s = model.fpg(f, season=True) if pid else fpg  # the Season rating's ridge (findings 320 (2))
            if pid and not gl and p2 and prev.get(pid):  # no game yet: the Week leans on both past seasons (handoff 429)
                fw = skater_features(gl, prev.get(pid), xg_now.get(pid), xg_prev.get(pid), tg, is_d=p["pos"] == "D", p2=p2,
                                     context=context.get(pid), age=age, two_season=True)
                fw.update(ux_features(fw, "D" if p["pos"] == "D" else "F", model.sk.get("ux_means")))
                fpg = model.fpg(fw)
            # opening games cut short: average with his points a game leaving those games out (findings 171)
            kept = short_opening(gl, prev.get(pid)) if pid else None
            if kept is not None:
                f2 = skater_features(kept, prev.get(pid), xg_now.get(pid) if kept else None, xg_prev.get(pid), tg,
                                     is_d=p["pos"] == "D", p2=p2, context=context.get(pid), age=age)
                f2.update(ux_features(f2, "D" if p["pos"] == "D" else "F", model.sk.get("ux_means")), team_games=f["team_games"],
                          **teammates.get(pid, {}))
                fpg = (fpg + model.fpg(f2)) / 2
                fpg_s = (fpg_s + model.fpg(f2, season=True)) / 2
            # with a new team since last season, and with one team this season: rated ~3.5% high (findings 230)
            last_team = (prev.get(pid) or {}).get("team") if pid else None
            if last_team and team in rosters and last_team != team and not traded and len({g["team"] for g in gl}) <= 1:
                fpg *= OFFSEASON_MOVER_MULT_WEEK
                fpg_s *= OFFSEASON_MOVER_MULT
            # 0-3 games: part of the way to ESPN's preseason projection (handoff 429)
            early_w = next((w for top, w in EARLY_WEEK_ESPN if len(gl) <= top), 0.0) if pid else 0.0
            proj_pre = p.get("proj_ppg_pre", p.get("proj_ppg"))
            if early_w and proj_pre is not None:
                fpg += early_w * (proj_pre - fpg)
            # early season: opponents who leaked goals last season, faded as they play this season (handoff 504); off until
            # his team has played a game (handoff 506: the opening week was worse with it in all 6 seasons)
            opp_left = [o for d, o, _ in week_games.get(espn_team) or week_games.get(team, [])
                        if d > today.isoformat() or (d == today.isoformat() and not game_started(p))]
            opp_last, toi_chg = None, None
            if pid and tg and opp_ga and opp_left:
                opp_move = OPP_LAST_C * sum(OPP_LAST_K / (OPP_LAST_K + len(team_games.get(o, []))) * opp_ga.get(o, 0.0)
                                            for o in opp_left) / len(opp_left)
                fpg *= 1 + opp_move
                # for the details: how many more (fewer) goals a game this week's opponents allowed last season (handoff 508)
                opp_last = {"ga": round(sum(opp_ga.get(o, 0.0) for o in opp_left) / len(opp_left), 3), "move": round(opp_move, 3)}
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
            mv = ((dfo_news or {}).get("roster-moves") or {}).get(str(pid)) if pid and not gl else None
            called_up = bool(mv and mv.get("up") and not mv.get("down") and not p.get("injury") and p["team"] != "FA"
                             and mv.get("ts", "") >= (now_utc - timedelta(days=RECALL_NEW_DAYS)).strftime("%Y-%m-%dT%H:%M"))
            if called_up:  # just called up, yet to play: about 1 game in 4, not the 0.10 for a player who isn't playing
                base_dress, cap, p_dress = None, 1.0, RECALL_NEW_DRESS
            elif cleared_new:
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
            season_share, news_why, boost_q = None, [], None
            if df_ and model.season_lr:
                sx = season_dress_features(gl, team, tg, f, df_, prev.get(pid), fpg_s, model.season_lr["markov"],
                                           played=len(team_games.get(team, [])))
                news_z, news_why = news_season_logit(dfo_news, pid, gl[-1]["date"] if gl else None, sx["os_k"], sx["gp_now"], now_utc)
                # missed his team's last game with an ESPN status: ESPN's stated timeline or out-for-the-season note (658-659)
                tl = injury_note(notes[p["id"]], today) if sat_last and p.get("injury") and p["id"] in notes else None
                if tl and tl["season"]:
                    news_z += ESPN_SEASON_OUT if "season-ending" not in news_why else 0.0
                elif tl and tl["days_left"] is not None:
                    news_z += SEASON_TIMELINE_C * min(max(tl["days_left"], 0) / 2.2 / max(SEASON_GAMES - len(team_games.get(team, [])), 1), 1)
                # still listed after playing his team's last game, from a note around that game (handoff 687)
                if gl and not sat_last and p.get("injury") in ("DAY_TO_DAY", "OUT", "INJURY_RESERVE") and p["id"] in notes:
                    n_ = notes[p["id"]]
                    nd_ = note_date(n_.get("date"))
                    if nd_ and nd_ >= date.fromisoformat(gl[-1]["date"][:10]) - timedelta(days=1) \
                            and not NOT_INJURY_NOTE.search(f"{n_.get('short') or ''} {n_.get('long') or ''}"):
                        news_z += LISTED_PLAYED_SEASON
                z = model.season_dress_logit(sx, NEW_TEAM_SEASON_LOGIT * new_team_season + LEFT_EARLY_SEASON_LOGIT * bool(early)
                                             + CLEARED_SEASON_LOGIT * cleared_out)
                # part of the way to the 16-season boosted share (findings 360 (3)); not for a skater ESPN lists on a team he
                # hasn't played for yet, whose inputs count his old team's games since the trade as absences (handoff 442)
                if boost_dress and pid in boost_x and not traded:
                    q = min(max(season_boost.predict(boost_dress, boost_x[pid]), 0.01), 0.99)
                    z += BOOST_DRESS_W * (math.log(q / (1 - q)) - z)
                    boost_q = q
                season = rest_share = 1 / (1 + math.exp(-(z + news_z)))
                season *= fpg_s
            elif cleared_new:
                season_share = CLEARED_NEW_SEASON * UNPLAYED_NOTED_MULT[unplayed_cell(len(team_games.get(team, [])))]
            elif not gl and pid and p.get("injury") in ("DAY_TO_DAY", "OUT", "INJURY_RESERVE", "SUSPENSION"):
                # hurt or suspended before playing this season: such regulars dress for ~41% of the rest, not the idle cap's
                # 10% (findings 74; suspensions take the short-injury share, findings 118)
                pre = injury_note(notes[p["id"]], today) if p["id"] in notes else None
                season_share = PRESEASON_INJURED["season" if pre and pre["season"] else "long" if pre and pre["long"] else "short"] \
                    * UNPLAYED_NOTED_MULT[unplayed_cell(len(team_games.get(team, [])))]
            elif not gl and pid in on_roster and tg and not p.get("injury") \
                    and unplayed_share((prev.get(pid) or {}).get("gp", 0), len(tg)) is not None:
                # games last season, on a current NHL roster (so not retired or overseas), yet to play while his team has:
                # his share falls the longer that lasts (findings 118, 350 (1), (4))
                season_share = unplayed_share(prev[pid]["gp"], len(tg))
            else:
                season, rest_share = fpg_s * p_dress, p_dress
            if season_share is not None:
                season, rest_share = fpg_s * season_share, season_share
            # Proj GP: NHL games so far + the Season share of his ESPN team's games still to play (his NHL team's for an ESPN
            # free agent with games, handoff 443)
            games_left_season = max(SEASON_GAMES - len(team_games.get(espn_team) or team_games.get(team, [])), 0)
            proj_gp = len(gl) + rest_share * games_left_season
            age_adj = season_age_adj(births.get(pid), today) if pid else 0.0
            if fpg_s > 0:
                season *= max(fpg_s + age_adj, 0.0) / fpg_s
            # ESPN's preseason projection pulls the Season FP/G, most at 0 games and fading by 40 (findings 360 (2))
            # ... and the 16-season boosted FP/G, most at 0 games and fading with GP (findings 360 (4)): both measured from
            # the same Season FP/G
            espn_w, boost_f = None, None
            if rest_share > 0:
                g = min(len(gl), 40) / 40
                fpg_t = season / rest_share
                shift = 0.0
                proj = p.get("proj_ppg_pre", p.get("proj_ppg"))  # his first-seen projection this season (handoff 445)
                if proj is not None:
                    espn_w = ESPN_PROJ_BLEND[0] + ESPN_PROJ_BLEND[1] * g + ESPN_PROJ_BLEND[2] * g * g
                    shift += (proj - fpg_t) * espn_w
                if boost_fpg and pid in boost_x:
                    boost_f = season_boost.predict(boost_fpg, boost_x[pid])
                    shift += (boost_f - fpg_t) * (BOOST_FPG_BLEND[0] + BOOST_FPG_BLEND[1] * g)
                # early season: ice time changed vs last season, shrunk while it rests on few games (handoff 507)
                pv = (prev.get(pid) or {}) if pid else {}
                # not after a game he left hurt, which isn't lost ice time (handoff 512); the change capped at 5 minutes
                if (1 <= len(gl) <= SEASON_TOI_CHG_GAMES and pv.get("toi") and (pv.get("gp") or 0) >= 10 and kept is None
                        and not early and not pv.get("season_label") and f.get("std_toi") is not None):
                    d_toi = min(max(f["std_toi"] - pv["toi"], -SEASON_TOI_CHG_CAP), SEASON_TOI_CHG_CAP)
                    toi_chg = SEASON_TOI_CHG_C * d_toi * len(gl) / (len(gl) + 3) \
                        * (SEASON_TOI_CHG_K / (SEASON_TOI_CHG_K + len(gl))) ** 3
                    shift += toi_chg
                season = rest_share * max(fpg_t + shift, 0.0)
            season_pts = season / rest_share if rest_share > 0 else None
            if n.get("apply"):
                if n.get("dress") is not None:
                    p_dress = max(0.0, min(float(n["dress"]), 1.0))
                    base_dress = None  # the news call replaces the lineup model
                if n.get("fpg_mult") is not None:
                    fpg *= max(0.8, min(float(n["fpg_mult"]), 1.2))
            note = injury_note(notes[p["id"]], today) if p.get("injury") and p["id"] in notes else None
            if note and note["long_why"] == "unclear":  # only "unclear when he'll return": dressed 25-30% of games (audit5)
                note = {**note, "long": False, "long_why": None}
            long_caps = LONG_NOTE_CAPS.get(note.get("long_why")) if note and note["long"] else None
            inj = (max(long_caps) if long_caps else 0.0) if note and note["long"] else SKATER_INJURY_CAP.get(p.get("injury") or "", 1.0)
            # OUT for one named game that his team has since played: day-to-day, as ESPN has him by the next morning (findings 236)
            one_game_over = p.get("injury") == "OUT" and p["id"] in notes and not (note and note["long"]) \
                and one_game_out_over(notes[p["id"]], team_dates.get(team, ()),
                                      {d: st for (tm, d), st in week_starts.items() if tm == team})
            if one_game_over:
                inj = SKATER_INJURY_CAP["DAY_TO_DAY"]
            # per-game caps by days to the game (IR, handoff 629) or by games left to serve (suspension, handoff 631)
            game_caps = None
            if p.get("injury") == "INJURY_RESERVE" and not (note and note["long"]):
                inj = IR_DAY_CAPS[2]
                game_caps = "ir"
            elif long_caps:
                game_caps = "long"
            elif p.get("injury") == "SUSPENSION" and p["id"] in notes and p["games_left"]:
                game_caps = suspension_caps(notes[p["id"]], team_dates.get(team, ()), p["games_left"],
                                            {d: st for (tm, d), st in week_starts.items() if tm == team})
                # a long note with no stated length ("suspended indefinitely") keeps 0 (handoff 664)
                if game_caps is not None and note and note["long"] \
                        and not SUSP_LEN.search(f"{notes[p['id']].get('short') or ''} {notes[p['id']].get('long') or ''}"):
                    game_caps = None
                if game_caps is not None:
                    inj = 1.0
            if p.get("injury") in ("DAY_TO_DAY", "OUT") and gl and not sat_last and not (note and note["long"]) and f.get("std_toi"):
                share = gl[-1]["toi"] / f["std_toi"]
                if p["injury"] == "DAY_TO_DAY":
                    inj = next(c for top, c in DTD_PLAYED_CAPS if share < top)
                elif share < DTD_PLAYED_CAPS[0][0]:
                    inj = min(inj, OUT_LEFT_EARLY_CAP)
            # a fresh illness note (handoff 676) or return words (677) lift the cap; IR's game today keeps its cap
            word_floor, return_words = None, False
            # behind a bare status word ('out'), his last real note while he hasn't played since, as the research read it
            wn = notes.get(p["id"])
            if wn and PLACEHOLDER_NOTE.match(wn.get("short") or ""):
                wn = stored_notes.get(str(p["id"]))
                wn = wn if wn and not played_since(p["id"], str(wn.get("date") or "")) else None
                # ... unless that note itself reads long ("unclear when" alone isn't long): as the research's not-long rows (audit6)
                wl = injury_note(wn, today) if wn else None
                wn = wn if wl and not (wl["long"] and wl["long_why"] != "unclear") else None
            if p.get("injury") in ("DAY_TO_DAY", "OUT", "INJURY_RESERVE") and wn and not (note and note["long"]):
                ill, return_words = note_words(wn)
                nd = note_date(wn.get("date"))
                word_floor = ILLNESS_CAP if ill and nd and (today - nd).days <= ILLNESS_DAYS else RETURN_CAP if return_words else None
                if word_floor is not None:
                    inj = max(inj, word_floor)
            if p.get("injury") in SKATER_INJURY_CAP:
                injury_log.append({"date": today.isoformat(), "espn_id": p["id"], "nhl_id": pid, "name": p["name"],
                                   "status": p["injury"], "long_note": bool(note and note["long"]),
                                   "p_dress_table": round(p_dress, 3), "p_dress": round(min(p_dress, inj), 3)})
            # Daily Faceoff news since his last game, per remaining game (handoffs 406-408)
            gn = p["games_left"]
            wk_dates = [d for d in sched.get(espn_team, []) if d >= today.isoformat()][-gn:] if gn else []
            wk_dates = (wk_dates + wk_dates[-1:] * gn)[:gn] if wk_dates else [today.isoformat()] * gn
            # his team's games still to start that don't count for him (a free agent's game tonight after the first puck, a
            # waiver player's games before he clears) keep their place in his per-game list, so his own games keep their
            # game numbers (hunt14/puck); they are cut out again below
            tonight_ahead = today.isoformat() in sched.get(espn_team, []) and \
                (parse_utc(week_starts.get((espn_team, today.isoformat()))) or now_utc) > now_utc
            lead = [d for d in sched.get(espn_team, []) if (d > today.isoformat() or d == today.isoformat() and tonight_ahead)
                    and (not wk_dates or d < wk_dates[0])]
            skip, tail = len(lead), []
            # dropped after the first puck: his own games are his team's next ones, not the week's last (hunt14/puck)
            if p.get("pending_drop") and gn:
                ahead = [d for d in sched.get(espn_team, []) if d > today.isoformat() or d == today.isoformat() and tonight_ahead]
                if len(ahead) >= gn:
                    wk_dates, tail, lead, skip = ahead[:gn], ahead[gn:], [], 0
            own_n = gn
            wk_dates = lead + wk_dates + tail
            gn = gn + skip + len(tail)
            # no game yet this season: news since the season's first game counts (handoff 427)
            wk_news, wk_down, practising, wk_why = week_news_logit(dfo_news, pid, gl[-1]["date"] if gl else season_open, sat_last,
                                                          p.get("injury"), wk_dates,
                                                          week_starts.get((gl[-1]["team"], gl[-1]["date"][:10])) if gl else None)
            if practising and p.get("injury") in ("OUT", "INJURY_RESERVE") and not (note and note["long"]):
                inj = max(inj, SKATER_INJURY_CAP["DAY_TO_DAY"])  # practising: day-to-day's cap (handoff 406)
            p_dress = min(p_dress, inj)
            # sat the last game, but Daily Faceoff's projected lines have him: 14 of 14 dressed next time (findings 174)
            in_lines = sat_last and not p.get("injury") and p.get("dfo_out") is False
            # straight team games he has missed (any reason), for the long-scratch turn and the win-run shift
            missed = next((k for k, g in enumerate(reversed(tg)) if g in {x["game"] for x in gl}), len(tg)) if gl and sat_last else 0
            # healthy-scratch lifts, added after the boosted Week model (handoffs 515-516)
            scratch_z = 0.0
            post_z = (SCRATCH_LONG_LOGIT if missed >= SCRATCH_LONG_GAMES else SCRATCH_LOGIT["D" if p["pos"] == "D" else "F"]) * scratch
            # a scratch whose same-position teammate left the last game early fills in (handoff 352)
            bands = [b for q, b in (left_teammates.get((team, p["pos"] == "D")) or {}).items() if q != pid]
            filled = scratch and bool(bands)
            if filled:  # the teammate who played least of his minutes sets the lift (handoff 353)
                post_z += TEAMMATE_LEFT_BANDS[min(bands)][1]["D" if p["pos"] == "D" else "F"]
            # a depth forward's special-teams role in his last game (handoff 358)
            st_role = None
            if gl and pid and p["pos"] != "D" and not sat_last and not traded and not early and not p.get("injury") \
                    and base_dress is not None and gl[-1]["toi"] < DEPTH_TOI["F"]:
                st_role = "pk" if gl[-1].get("shtoi", 0) >= ST_PK_MIN else "none" if not gl[-1].get("shtoi") and not gl[-1].get("pptoi") else None
            # a depth skater's minutes rank among his team's dressed forwards (or D) in its last game (handoff 360)
            toi_rank = None
            if gl and pid and not sat_last and not traded and not early and not p.get("injury") and base_dress is not None \
                    and gl[-1]["toi"] < DEPTH_TOI["D" if p["pos"] == "D" else "F"] and tg and gl[-1]["game"] == tg[-1]:
                low = fewest.get((gl[-1]["team"], gl[-1].get("pos") == "D"))
                toi_rank = "lowest" if low is not None and gl[-1]["toi"] <= low else "other"
            # a scratch who had power-play time in his last game, within 10 days (handoff 359); not one Daily Faceoff's
            # lines have back, whose next game is floored at DFO_LINEUP_NEXT
            pp_scratch = bool(scratch and not in_lines and gl and base_dress is not None and gl[-1].get("pptoi", 0) >= PP_SCRATCH_MIN
                              and (today - date.fromisoformat(gl[-1]["date"][:10])).days <= PP_SCRATCH_DAYS)
            # his team's last result, by game (handoffs 354, 357)
            by_res = SCRATCH_LAST_RESULT_BY_GAME["win" if forms[team][1] > 0 else "other"] if scratch and forms.get(team) else None
            # a depth skater who scored in the last game keeps his spot more (handoff 351)
            scored = bool(gl) and not sat_last and not traded and not early and not p.get("injury") \
                and gl[-1]["toi"] < DEPTH_TOI["D" if p["pos"] == "D" else "F"] and gl[-1].get("points", 0) > 0
            if scored:
                scratch_z += DEPTH_POINT_LOGIT
            per_game = dress_by_game(base_dress, gn, sat_last and not in_lines, min(cap, inj),
                                     scratch_z + NEW_TEAM_LOGIT * new_team + comeback + CLEARED_LOGIT * cleared_out, early) \
                if base_dress is not None else [CLEARED_NEW_NEXT] + [p_dress] * (gn - 1) if cleared_new and gn else [p_dress] * gn
            # team form and depth D (handoffs 340-342, 346): healthy depth skaters after a slump or a blowout, D on low
            # minutes, and skaters out 1-4 games while their team keeps winning
            pos2 = "D" if p["pos"] == "D" else "F"
            depth_toi = gl[-1]["toi"] if gl and not sat_last and not traded and not early and not p.get("injury") else None
            out_games = missed if not traded and not (note and note["long"]) and p.get("injury") != "INJURY_RESERVE" else 0
            fshift, fwhy = form_shift(pos2, depth_toi, out_games, forms.get(team), len(tg)) if pid else (0.0, [])
            if fshift and per_game:  # clipped to [0.001, 0.999]; a player already at 0 stays there (findings 296)
                per_game = [q if q <= 0.001 else min(max(q + fshift, 0.001), 0.999) for q in per_game]
                why_form = {"shift": round(fshift, 3), "why": fwhy}
            else:
                why_form = None
            # the 16-season boosted next-week dress share (handoff 411), measured against his chances before the news, then
            # Daily Faceoff injury news since his last game (406, 408) and a send-down (407) on top, then the caps (handoff
            # 434: measured after the news, the boost took back ~90% of it). Not for a skater ESPN lists on a team he hasn't
            # played for yet, whose inputs describe his old team
            boost_wk = boost_move = news_move = None
            if boost_week and base_dress is not None and pid in boost_x and per_game and not traded:
                live = [q for q in per_game if q > 0.001]
                if live:
                    b = min(max(season_boost.predict(boost_week, boost_x[pid]), BOOST_WEEK_CLIP[0]), BOOST_WEEK_CLIP[1])
                    boost_wk = BOOST_WEEK_W * (logit(b) - logit(sum(live) / len(live)))
            has_news = bool(wk_news) and (any(wk_news) or bool(wk_down))
            post_g = [post_z + (by_res[min(k, len(by_res) - 1)] if by_res else 0.0) for k in range(len(per_game))]
            if per_game and base_dress is not None and (boost_wk is not None or has_news or any(post_g)):
                shift = lambda news, down, extra=post_g: [
                    q if q <= 0.001 else min(1 / (1 + math.exp(-(logit(q) + (boost_wk or 0.0) + e + z + down))), cap, inj)
                    for q, z, e in zip(per_game, news, extra)]
                mean = lambda xs: sum(xs) / len(xs)
                boosted = shift([0.0] * len(per_game), 0.0)  # the boost and the scratch lifts (handoffs 515-516)
                final = shift(wk_news, wk_down) if has_news else boosted
                if boost_wk is not None:
                    boost_move = mean(shift([0.0] * len(per_game), 0.0, [0.0] * len(per_game))) - mean(per_game)  # the boost alone (425)
                if has_news:
                    news_move = mean(final) - mean(boosted)  # the news's real effect after the caps (handoff 435)
                per_game = final
            dress_next = per_game[0] if per_game else CLEARED_NEW_NEXT if cleared_new else p_dress
            if in_lines and per_game:  # no game left: nothing to lift (findings 182)
                dress_next = max(dress_next, DFO_LINEUP_NEXT)
                per_game = [dress_next] + per_game[1:]
            # ESPN has him OUT on the day of his team's game, still to start: that game counts ~0 (findings 235)
            if p.get("injury") == "OUT" and not one_game_over and per_game and \
                    ((p.get("today_start") and not game_started(p)) if not skip else (wk_dates[0] == today.isoformat() and tonight_ahead)):
                per_game[0] = min(per_game[0], OUT_TODAY_CAP)
                dress_next = per_game[0]
            if game_caps == "ir":
                game_caps = None if practising else [
                    (lambda k: max(IR_DAY_CAPS[k], word_floor) if k and word_floor is not None else IR_DAY_CAPS[k])(
                        min((date.fromisoformat(d) - today).days, 2)) for d in wk_dates]
            elif game_caps == "long":
                game_caps = [long_caps[0] if (date.fromisoformat(d) - today).days <= 1 else long_caps[1] for d in wk_dates]
            if game_caps and per_game:
                per_game = [min(q, c) for q, c in zip(per_game, game_caps)]
                dress_next = per_game[0]
            out_idx = list(range(skip)) + list(range(skip + own_n, gn))  # his team's games that aren't his
            all_dates = wk_dates
            lead_q = [per_game[i] for i in out_idx] if per_game else []
            lead_game_caps = [game_caps[i] for i in out_idx] if game_caps else None
            if out_idx:
                per_game, wk_dates, gn = per_game[skip:skip + own_n], wk_dates[skip:skip + own_n], own_n
                game_caps = game_caps[skip:skip + own_n] if game_caps else game_caps
                dress_next = per_game[0] if per_game else dress_next
            exp_games = sum(per_game)
            if gn:
                p_dress = exp_games / gn  # the week's average, shown as "to dress"
            # a depth skater who fought in his last game (handoff 356)
            fought = bool(gl) and not sat_last and not traded and not early and not p.get("injury") and base_dress is not None \
                and gl[-1]["toi"] < DEPTH_TOI["D" if p["pos"] == "D" else "F"] and gl[-1].get("pim", 0) >= FIGHT_PIM
            if gl and pid and per_game:
                caps = [min(cap, inj)] * len(per_game)
                if p.get("injury") == "OUT" and not one_game_over and p.get("today_start") and not game_started(p) and not skip:
                    caps[0] = min(caps[0], OUT_TODAY_CAP)
                if game_caps:
                    caps = [min(a, c) for a, c in zip(caps, game_caps)]
                lead_caps = [min(cap, inj, c) for c in (lead_game_caps or [1.0] * len(out_idx))]
                if out_idx and p.get("injury") == "OUT" and not one_game_over and all_dates[out_idx[0]] == today.isoformat() and tonight_ahead:
                    lead_caps[0] = min(lead_caps[0], OUT_TODAY_CAP)
                trips = after_trip(venue.get(espn_team, {}), all_dates)
                week_rows.append({"p": p, "pid": pid, "team": espn_team if traded else gl[-1]["team"], "isD": gl[-1].get("pos") == "D",
                                  "dates": wk_dates, "per_game": list(per_game), "before": list(per_game), "caps": caps,
                                  "missed": missed, "after_trip": trips[skip:skip + own_n], "j0": skip,
                                  # his team's games still ahead that aren't his: (date, chance, cap, home after a trip,
                                  # game number) for the lineup sums only (hunt14/puck)
                                  "others": [(all_dates[i], q, c, trips[i], i) for i, q, c in zip(out_idx, lead_q, lead_caps)
                                             ],
                                  # a healthy regular: dressed his team's last game at regular minutes, no injury listing or
                                  # Daily Faceoff news since (handoff 355)
                                  "regular": not sat_last and not traded and not early and not p.get("injury") and not wk_why
                                  and base_dress is not None
                                  and gl[-1]["toi"] >= DEPTH_TOI["D" if p["pos"] == "D" else "F"],
                                  "fought": fought, "st_role": st_role, "pp_scratch": pp_scratch,
                                  "toi_rank": toi_rank,
                                  # missed the last game with no listing, status or news (623); DFO day-to-day news (624)
                                  "no_status_out": sat_last and not p.get("injury") and not scratch and not (any(wk_news) or wk_down),
                                  "dtd_next": "day-to-day" in wk_why,
                                  # games before ESPN's stated return date, when it is 0-7 days away (handoff 670)
                                  "before_return": [(date.fromisoformat(d) - today).days < note["days_left"] for d in wk_dates]
                                  if p.get("injury") in ("DAY_TO_DAY", "OUT", "INJURY_RESERVE") and note and not note["long"]
                                  and note["days_left"] is not None and 0 <= note["days_left"] <= 7 else None,
                                  "return_words": return_words,  # ESPN return words, no illness word (handoff 677)
                                  "low_scorer": toi_rank == "lowest" and scored,  # handoff 362
                                  })
            why = {"toi5": f.get("l5_toi"), "toi": f.get("std_toi") if gl else None, "pp5": f.get("l5_pptoi"),
                   "sog": f.get("std_shots"), "blk": f.get("std_blockedShots"), "gp": len(gl),
                   "last_fpg": prev[pid]["fp"] if pid in prev else None, "xfp": nhl_xfp(expected_fp(xg_now.get(pid), xg_prev.get(pid), p["pos"] == "D"), gl) if pid else None}
            why = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in why.items()}
            if (prev.get(pid) or {}).get("season_label"):  # last season's line is from the season before (findings 234)
                why["last_season"] = prev[pid]["season_label"]
            why["season_fpg"] = round(fpg_s, 2)  # the Season rating's points a game, its own ridge (findings 320 (2))
            if opp_last:
                why["opp_last"] = opp_last  # this week's opponents' goals against last season (handoffs 504, 508)
            if toi_chg is not None:
                why["toi_chg"] = round(toi_chg, 3)  # the Season pull-back on an early ice-time change (handoff 507)
            if called_up:
                why["called_up"] = True  # just called up, no NHL game yet: 0.25 a game (handoff 502)
            if filled and base_dress is not None:
                why["fills_in"] = True  # a same-position teammate left the last game early (handoff 352)
            if scored and base_dress is not None:
                why["depth_point"] = True  # a depth player who scored last game (handoff 351)
            if toi_rank:
                why["toi_rank"] = toi_rank  # fewest minutes of his team's F (or D) last game, or another depth skater (360)
            if pp_scratch:
                why["pp_scratch"] = True  # a scratch who had power-play time in his last game (handoff 359)
            if st_role:
                why["st_role"] = st_role  # a depth forward who killed penalties, or had no special teams, last game (handoff 358)
            if fought:
                why["fought"] = True  # a depth player who fought last game (handoff 356)
            if wk_why and news_move is not None:  # handoffs 406-408; its effect on his chance, shown from 2 points (435)
                why["week_news"] = {"shift": round(sum(wk_news) / len(wk_news) + wk_down, 2), "move": round(news_move, 3), "why": wk_why}
            if boost_wk is not None:
                why["boost_week"] = round(boost_wk, 2)  # the 16-season boosted next-week dress shift (handoff 411)
            if boost_move is not None and abs(boost_move) >= 0.05:
                why["boost_week_move"] = round(boost_move, 2)  # its effect on his chance to play, for the details (handoff 425)
            if why_form:
                why["form"] = why_form  # team form / depth D lineup shift on this week's games (handoffs 340-342, 346)
            if season_pts is not None:  # after age, ESPN and the boosted model: Season = this x his share (check_data P5)
                why["season_pts"] = round(season_pts, 2)
            if boost_q is not None:
                why["boost_dress"] = round(boost_q, 2)  # the 16-season boosted dress share (findings 360 (3))
            if boost_f is not None:
                why["boost_fpg"] = round(boost_f, 2)  # the 16-season boosted FP/G (findings 360 (4))
            if espn_w:
                why["espn_blend"] = round(espn_w, 2)  # share of the gap to ESPN's preseason projection (findings 360 (2))
                if p.get("proj_ppg_pre") is not None:
                    why["espn_proj"] = p["proj_ppg_pre"]  # the first-seen projection the blend used (handoff 445)
            if news_why:
                why["season_news"] = news_why  # Daily Faceoff news in the Season share (findings 360 (1))
            if season_share is not None:
                why["season_share"] = round(season_share, 2)
            if abs(age_adj) >= 0.03:
                why["age_adj"] = round(age_adj, 2)
            gp_last = 0 if (prev.get(pid) or {}).get("season_label") else (prev.get(pid) or {}).get("gp", 0)
            rng, exp = model.season_range(season, p["pos"] == "D", gp_last, (prev2.get(pid) or {}).get("gp", 0),
                                          (today - births[pid]).days / 365.25 if pid in births else None, len(gl or ())) if pid else (None, None)
            if gl and pid:  # his team's open lineup spots, once every share is known (findings 370, below)
                # a traded skater yet to play for his new team counts in the new team's lineup (handoff 442)
                lineup.append({"p": p, "pid": pid, "team": espn_team if traded else gl[-1]["team"], "isD": gl[-1].get("pos") == "D",
                               "share": rest_share, "pts": season_pts, "gp": len(gl), "left": games_left_season,
                               "range": (p["pos"] == "D", gp_last, (prev2.get(pid) or {}).get("gp", 0),
                                         (today - births[pid]).days / 365.25 if pid in births else None, len(gl)),
                               # a first-round rookie (handoff 632)
                               "rookie_pick": pid in bios and (bios[pid][1] or 999) <= ROOKIE_PICK_MAX and bool(bios[pid][2])
                               and bios[pid][2] // 10000 >= espn_season - 3
                               and gp_last + (prev2.get(pid) or {}).get("gp", 0) <= ROOKIE_PICK_GP})
            if pid and season_pts is not None:  # the league-wide F/D scoring drift, once every Season FP/G is known (handoff 405)
                drift_rows.append({"p": p, "D": p["pos"] == "D", "pts": season_pts, "share": rest_share, "fp": [g["fp"] for g in gl],
                                   "range": (p["pos"] == "D", gp_last, (prev2.get(pid) or {}).get("gp", 0),
                                             (today - births[pid]).days / 365.25 if pid in births else None, len(gl or ())),
                                   "lineup": lineup[-1] if gl else None})
            if rng:
                why["season_range"] = rng
                if exp != "veteran":
                    why["exp"] = exp
            if p.get("injury") in SKATER_INJURY_CAP and p["injury"] != "SUSPENSION":
                why["back"] = usually_back(p["injury"], sat_last, note)
            if word_floor is not None:  # the illness or return-word cap that lifted his (handoffs 676-677, 685)
                why["note_lift"] = "illness" if word_floor == ILLNESS_CAP and not return_words else "return"
        p["cr"] = round(fpg * exp_games, 2)
        p["cr_fpg"] = round(fpg, 2)
        p["cr_season"] = round(season, 2)
        p["proj_gp"] = round(proj_gp)
        p["cr_games"] = round(exp_games, 2)
        p["cr_dress"] = None if p_dress is None else round(p_dress, 2)
        p["cr_dress_next"] = None if p_dress is None else round(dress_next, 2)  # his team's next game: tonight cards
        p["sat_last"] = sat_last
        p["cr_matched"] = pid is not None
        p["nhl_id"] = pid
        p["cr_why"] = why
        if n:
            p["cr_news"] = {k: n.get(k) for k in ("flag", "source", "quote", "apply") if n.get(k) is not None}

    season_drift(drift_rows, model)
    # each team's last-season regulars with no NHL game yet this season, on his LAST-SEASON team, whatever his status now
    # (handoff 509 as researched; 513: current-team attribution and a roster filter tested worse)
    missing_w = {}
    for mid, pv in prev.items():
        tm = pv.get("team")
        if tm and (pv.get("gp") or 0) >= MISSING_REG_GP and mid not in by_player and not pv.get("season_label") \
                and pv.get("pos") != "G":
            missing_w[tm] = missing_w.get(tm, 0.0) + (pv.get("toi") or 0) / (20 if pv.get("pos") == "D" else 15)
    # the Week's regulars due back (handoff 510): his current team (NHL roster, else ESPN's), as tested
    now_team = {p["nhl_id"]: ESPN_TO_NHL.get(p["team"], p["team"]) for p in players
                if p.get("nhl_id") and p.get("team") and p["team"] != "FA"}
    now_team.update({pid: team for team, r in rosters.items() for pid, _, _ in r})
    missing = {tm: MISSING_REG_C * w * MISSING_REG_K / (MISSING_REG_K + len(team_games.get(tm, [])))
               for tm, w in missing_w.items() if len(team_games.get(tm, [])) >= MISSING_REG_MIN_TG}
    lineup_slack(lineup, boost_x, boost_dress, model, missing)
    week_slack(week_rows, boost_x, boost_week, week_missing_regulars(prev, by_player, team_games, dfo_news, now_utc, now_team))

    # a regular is back: the least-used skater at his position sits more than the dress model sees (findings section 63)
    by_nhl = {p["nhl_id"]: p for p in players if p.get("nhl_id") and p["pos"] != "G"}
    healthy = {pid for pid, p in by_nhl.items() if p.get("injury") in (None, "", "DAY_TO_DAY")}
    # the cut can land on a skater ESPN's pool here doesn't carry: he has no status (handoff 620)
    pickable = healthy | (set(by_player) - set(by_nhl))
    for pid, (cut, regular) in displaced_skaters(by_player, team_games, healthy, pickable).items():
        p = by_nhl.get(pid)
        if p and p["cr_dress"]:
            dress = max(0.0, p["cr_dress"] - cut)
            p["cr_games"] = round(p["games_left"] * dress, 2)
            p["cr"] = round(p["cr_fpg"] * p["cr_games"], 2)
            p["cr_dress"] = round(dress, 2)
            p["cr_dress_next"] = round(max(0.0, p["cr_dress_next"] - cut), 2)
            p["cr_why"]["displaced"] = regular
    # trades and claims: a newcomer at his position sits the least-used skater; a departed D brings a sitting D in
    listed = {pid: ESPN_TO_NHL.get(p["team"], p["team"]) for pid, p in by_nhl.items() if p.get("team")}
    cuts, lifts = roster_moves(by_player, team_games, listed, {pid for pid, p in by_nhl.items() if not p.get("injury")}, pickable)
    for pid, (cut, newcomer) in cuts.items():
        p = by_nhl.get(pid)
        if p and p["cr_dress"] and "displaced" not in p["cr_why"]:  # the same cut as a returning regular, not stacked
            dress = max(0.0, p["cr_dress"] - cut)
            p["cr_games"] = round(p["games_left"] * dress, 2)
            p["cr"] = round(p["cr_fpg"] * p["cr_games"], 2)
            p["cr_dress"] = round(dress, 2)
            p["cr_dress_next"] = round(max(0.0, p["cr_dress_next"] - cut), 2)
            p["cr_why"]["newcomer"] = newcomer
    for pid, gone in lifts.items():
        p = by_nhl.get(pid)
        if p and p["cr_dress"] is not None:
            dress = min(1.0, p["cr_dress"] + DEPARTED_D_LIFT)
            p["cr_games"] = round(p["games_left"] * dress, 2)
            p["cr"] = round(p["cr_fpg"] * p["cr_games"], 2)
            p["cr_dress"] = round(dress, 2)
            p["cr_dress_next"] = round(min(1.0, p["cr_dress_next"] + DEPARTED_D_LIFT), 2)
            p["cr_why"]["d_departed"] = gone

    # the top power-play D is out: the next one up gets his PP time this week, not in the Season number (findings 310 (5))
    out = {p["nhl_id"] for p in players if p.get("nhl_id") and p.get("injury") in ("OUT", "INJURY_RESERVE", "SUSPENSION")}
    for pid, top in next_up_pp_d(by_player, team_games, out).items():
        p = by_nhl.get(pid)
        if p and p["cr_matched"]:
            p["cr_fpg"] = round(p["cr_fpg"] + NEXT_UP_PP_D_FPG, 2)
            p["cr"] = round(p["cr_fpg"] * p["cr_games"], 2)
            p["cr_why"]["pp_d_out"] = top

    # no NHL match (on no current NHL roster, no NHL game this season), no ESPN games this season or last and no projection:
    # retired and minor-league names ESPN still lists with a team (Gorges, Suter, Fleury) aren't rated on its games (handoff 453)
    for p in players:
        if not p["cr_matched"] and not p.get("gp") and not p.get("last_gp") and not p.get("proj_ppg"):
            p["cr"], p["cr_season"], p["cr_games"], p["cr_dress"], p["cr_dress_next"] = 0.0, 0.0, 0.0, 0.0, 0.0
            p["cr_why"]["no_record"] = True

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
    if winners is not None:
        try:  # the Predicted winners tab never blocks the ratings
            winners["games"] = predicted_games(players, by_player, prev, rosters, team_games, team_dates, sched, goalie_nights,
                                               save_pcts, strength, week_games, week_starts, week_results, team_names, lines, now_utc, xgd,
                                               today.isoformat())
        except Exception as e:  # noqa: BLE001
            print(f"Predicted winners failed: {e!r}", file=sys.stderr)
    print(f"Claude Rating: {len(players)} players, {unmatched} not matched to NHL data, {len(news)} news entries")
    return unmatched
