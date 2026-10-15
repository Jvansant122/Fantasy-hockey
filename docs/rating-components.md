# The Claude Rating, piece by piece

What each part of the rating does, what it uses, how much it was measured to matter, and what it leaves alone. Written for a fantasy manager; the research behind every number is in [the research write-up](research.md), and every idea tried is in the [ideas ledger](ideas-ledger.md).

Current as of Oct 15, 2026 (site code through PR #187).

## The two numbers

The site shows two ratings for every player. Everything below feeds one or both.

- **Week** (the Claude Rating): projected fantasy points for the rest of this Monday to Sunday matchup (both weeks of a two-week playoff round).
  - Skater: points per game × the sum, over his team's games left, of his chance to dress in each one (moved by Daily Faceoff news and a 16-season lineup model, with ESPN's injury status as a cap).
  - Goalie: expected starts left × points per start.
- **Season**: projected points per team game for the rest of the season, ignoring this week's schedule and ESPN's injury caps.
  - Skater: Season points per game × share of his team's remaining games he's expected to play.
  - Goalie: 0.32 + 0.72 × (his share of his team's starts × points per start), which puts goalies on the skaters' scale (part 8).

The page also combines them into **keep value** (this week + 7 × Season), which ranks adds and drops.

```
SKATER                                                GOALIE
points per game ──┬── × Σ P(dress) over games left ─► Week     expected starts ── × points per start ─► Week
   (part 1)       │     (parts 2, 5)                             (parts 5, 6)        (part 7: tonight's line,
                  │           ▲                                       ▲               later games team strength)
                  │   ESPN injury cap (part 3)            ESPN injury, Daily Faceoff (parts 3, 6)
                  │
                  └── × rest-of-season share played ─► Season   0.32 + 0.72 × start share × pts/start ─► Season
                            (part 4)                                  (part 8)
                                                    Week + 7 × Season = keep value (part 9)
```

## At a glance

| # | Part | Changes | Measured effect | Doesn't touch |
| --- | --- | --- | --- | --- |
| 1 | Skater points per game | Week and Season (skaters) | Ranks next week 0.38 (F) / 0.36 (D) vs 0.33 / 0.30 for season points per game; short-opening fix cut error 0.41 → 0.28 | Games, availability, injuries, opponents |
| 2 | Chance he dresses, game by game | Week (skaters) | The biggest single gain: ~+0.5 pts per pickup per week; lineup history took weekly ranking 0.637 → 0.652; the 16-season model plus Daily Faceoff news cut per-game error 5-10% | Season, points per game, goalies |
| 3 | ESPN injury status | Week (skaters and goalies) | Removes the lag on newly injured players; softer caps +0.007 weekly ranking (+0.009 free agents); minutes-based day-to-day caps cut error by a third | Season, points per game |
| 4 | Rest-of-season availability | Season (skaters) | Rest-of-season ranking 0.773 → 0.797 (waiver pool 0.671 → 0.701); Daily Faceoff news, ESPN's projection and the boosted models a further +0.0049 (free agents +0.0062) | Week, goalies |
| 5 | Games left | Week (everyone) | A top pickup's first week is worth 6.1 vs ~4 in later weeks, mostly schedule | Season, points per game |
| 6 | Goalie expected starts | Week (goalies) | Weekly start error 1.78 → 0.56; wire goalie picks 3.0 → 4.9 pts a week | Points per start |
| 7 | Goalie points per start | Week and Season (goalies) | Betting line splits favorites (~4.1) from underdogs (~2.8); later-week pricing 0.273 → 0.297 | Starts |
| 8 | Goalie Season rating | Season (goalies) | Rest-of-season error 0.545 → 0.537 pts a game with the 0.88 scale; the 0.32 + 0.72 rescale fixes goalies running wider than skaters | Week |
| 9 | Keep value | Adds and drops on the page | +1.75 pts/roster/week over ranking by this week alone | The two ratings |
| 10 | Percentile badge | Display only | None by design | The rating and the sort |
| 11 | Advice cards | Display only | 2 Monday moves + swaps + streams: +5.6 pts a week; drop list doubles each move's gain | The rating |
| 12 | Predicted winners tab | Display only | NHL picks 60.5% right, each season predicted from the seasons before it; slightly ahead of the closing betting line on 1,651 games | The rating |

How good is it overall? Ranking skaters on next week's points per game, the rating scored about 92% of an Oct 9 noise estimate. On what actually gets scored, a whole week with scratches as zeros, its ranking correlation is 0.64-0.65. Gains so far came mostly from knowing who sits. It is possible to find ways to boost performance rating predictions.

---

## 1. Skater points per game

**What it uses:** two per-stat models (ridge regressions), one for the Week rating with 277 inputs and one for the Season rating with 216, all from the NHL stats API and MoneyPuck. The shared inputs:
- ice time and power-play time over the last 5, 10 and 20 games;
- shots, shot attempts, hits, blocks, goals, assists, power-play points and fantasy points per game over the same windows, the season to date and last season;
- expected goals (his own and on-ice) this season and last;
- scoring per minute (this season plus half of last, pulled toward the position average) × last-5-game ice time, so a role change counts more for an efficient scorer;
- games played, whether he's a defenseman, and whether he has a last season;
- where he ranks in his own team's power-play time and ice time each game (top-5 PP time, top-3 forward or top-2 defenseman minutes), with position-by-usage terms (25 inputs, PR #86).
- two seasons ago (PR #111), and forecasts of next week's ice time and power-play time from small models of his recent games, last season, who missed his team's last game and his line and PP-unit slot in it (PRs #110, #114);
- 66 products of the 12 strongest inputs, so a role change counts more for an efficient scorer and the model can bend (PR #110).

- Ice-time forecasts (handoffs 668-669): the Season rating forecasts next week's ice time and power-play time with boosted trees (`model/season_boost_toi.json`, `season_boost_pptoi.json`) before its per-minute scoring inputs are built; the Week rating keeps linear forecasts, with more recent-usage windows, squares and defenseman × ice-time inputs.
Week only (PR #115, #118, #122): the 12 strongest inputs also enter as bends at five levels each (60 hinge inputs), the goals target is 80% goals and 20% next week's expected goals, with "finishing luck" inputs (goals above expected this season and last), flags for a fresh top-line or PP1 promotion, bends for players under 24 and over 29, and a small cut for players back from missed games. Season only (PRs #110, #116): an age adjustment (+0.01 a game per year under 24, -0.009 per year over 29) and 24 inputs that build points from parts: per-minute scoring rates × forecast ice time, his share of on-ice goals × on-ice expected goals, and forecast penalty-kill time. Measured in two steps, each on top of the model before it and better in every season: the forecasts, products and two-seasons-ago inputs lifted the weekly ranking +0.0016 (free agents +0.0032), and the Week-only bends, luck, promotion, age and return terms a further +0.0018 (+0.0032). The Season decomposition is worth +0.16 points a week in the hold-or-drop simulation.

**Season points per game after the model.** Four more steps move the Season number (not the Week), each measured from the same starting value. Let g = his games this season (capped at 40) / 40:
- **ESPN's preseason projection (PR #145):** + (ESPN's projected points per game − his number) × (0.29 − 0.57 g + 0.35 g²): about 29% of the gap at 0 games, 17% at 10, 9% at 20 and 6-7% from 30 on. Skaters without an ESPN projection are left alone.
- **A boosted model trained on 16 seasons (PR #146):** + (that model's rest-of-season points per game − his number) × (0.60 − 0.37 g), so 60% of the gap at 0 games and 23% at 40. Its inputs are rebuilt from the run's NHL data and the model's trees are walked in plain Python (`scripts/season_boost.py`, `model/season_boost_fpg.json`).
- **Early ice-time changes (PRs #179, #181):** in his first 10 games, when his ice time differs from last season's (10+ games then), the Season number moves by −0.28 × the change in minutes (capped at 5) × n / (n + 3) × (3 / (3 + n))³, with n his games this season, so it leans on an early change less and fades to nothing by his 10th game. Skipped after a game he left early, which isn't lost ice time. +0.0002 rest-of-season ranking (free agents +0.0004), every test season.
- **League-wide drift (PR #165):** once skaters have 10+ games, the site compares forwards' and defensemen's actual points per game (season to date and last 10 games) with their Season numbers, against the same gap for all skaters. Everyone in the group gets half that difference added (never below 0.05 a game). +0.0002 rest-of-season ranking, +0.0005 among free agents, every season.

Together with Daily Faceoff news in part 4, the ESPN pull and the boosted models lifted rest-of-season ranking by 0.0049 (free agents 0.0062), better in every test season.

**Week points per game early in the season.** Two steps move the Week number (not the Season):
- **No games yet (PR #171):** a skater with no game this season draws on both of his last two seasons, each game counting equally, and moves 25% of the way to ESPN's preseason projection (19% after 1-3 games, none after that). It improved the first two Mondays' ranking in every season from 2023-24 to 2025-26.
- **Early-season opponents (PRs #177, #178):** Week points per game × (1 + 0.45 × the average, over his opponents left this week, of 5 / (5 + that team's games this season) × how far that team's goals against per game last season ran above the league's, shrunk 10 games toward the league). It fades as those teams play this season, comes after the ESPN pull, and stays off until his team has played a game, since it made the opening week slightly worse in all six seasons. +0.0007 Week ranking in weeks 2 to 8, every test season.

**Next man up on the power play (PR #109):** when a team's top power-play defenseman (1+ PP minute a game, 5+ games) missed the last game or ESPN lists him out, IR or suspended, the next healthy defenseman by PP time who played that game gets +0.15 Week points a game. The Season number is unchanged. Tap him to see whose minutes he picks up.

Special cases:
- **Opening games cut short (PR #73):** with 1-3 games this season and the latest under half of last season's ice time, the number is the average of the usual one and one that leaves the short games out. Error fell from 0.41 to 0.28 points a game in all four seasons tested (Sanderson went from 0.38 to about 1.3). From his 4th game on nothing changes.
- **Traded or claimed skaters:** his games for every team this season count, so his rate travels with him. Only his chance of dressing has trade rules (part 2).
- **Yet to play this season (PR #88):** the team-usage ranks come from last season's role, where he ranked in power-play and total ice time per game among his last team's regulars. Only a player with no NHL games last season gets the league average (it had pulled McAvoy down 0.19 a game and Bedard 0.12).
- **Lost season (PR #92):** a player with under 10 games last season (Barkov) uses the season before instead, if he is on an NHL roster or has games this season.

ESPN players are matched to NHL stats by name, team and position, which splits same-name teammates (Vancouver's two Elias Petterssons) and accepts short first names ("Zack" Bolduc). A player not matched gets a replacement level of 1.04 (F) / 1.17 (D).

**What it changes:** `cr_fpg`, the points-per-game number that multiplies into both Week and Season.

**How much it matters:**
- Ranking wire players on next week's points per game: 0.38 (F) / 0.36 (D), against 0.33 / 0.30 for season points per game and 0.29 / 0.27 for last 10 games. It wins in all three test seasons.
- Recent ice time is the strongest single input (0.34 for forwards on its own). A hot last 5 games is the weakest (0.23).
- Expected goals: worth including, but a custom xG model on top added only +0.003. The per-minute × ice-time inputs added +0.002 (more in the first six weeks of a season).
- Projections are calibrated: no bias up or down.

**What it doesn't affect:** whether he plays, how many games his team has, injuries. A day-to-day player's minutes in his last game change his chance of dressing (part 3), not his rate. Players back from injury come back at their old rate. Apart from the early-season opponent term, it ignores opponent strength, home ice, back-to-backs, PDO, team pace and PP1 labels (all tested, all under 0.02 points a week). Age now enters both models (PRs #116, #122).

**Shown on the site:** tap a row for ice time, PP time, shots, blocks, last season's rate, and expected vs actual points per game ("running hot/cold" is a note only).

## 2. Chance he dresses (skaters)

**What it uses:** a model of his lineup history on his current team this season: how many games in a row he has missed, how often he sits, whether past absences looked like scratches (1-2 games) or injuries (3+), how many of the team's last 3 and 10 games he played, his ice time in his last game against his average, days since his last game, and whether he's new to the team. That gives a weekly chance, which is then set game by game and adjusted:

| Situation | What the site does | Measured |
| --- | --- | --- |
| Every player, game by game (PR #54) | Chance shifts by game number: down for the next game and up later for a player who is out, the reverse for a regular | Out players: 12% next game actual vs 18% shown before; regulars 94.5% vs 91% |
| Healthy scratch last game (PRs #55, #153, #158, #185) | +0.15 on the log-odds scale for forwards, +0.325 for defensemen, and +0.375 for either after 5+ straight games out, from the NHL's scratch list; no list readable means no ESPN status counts as a scratch. Since PR #185 it is added after the 16-season weekly model, at half its old size (before, that model took back about 90% of it) | Actual 34.5% against 25% before the bump; the position split made scratch forecasts about 0.5% more accurate in each of the last two seasons |
| Healthy scratch, and a teammate at his position (5+ games) left the last game early (PRs #160, #172, #185) | A further lift, by how much of his minutes the teammate played: under 40%, +0.5 (F) or +1.8 (D); 40-55%, +0.25 (F) or +1.2 (D). He fills in. Added after the 16-season weekly model since PR #185 | Scratched D in that spot dressed about 60% against the site's 30% |
| Depth skater (under 13 min F, 16 min D) with a point last game (PR #160) | +0.3: coaches keep a fourth-liner who just scored | Better in each of the last two seasons, fit on one and tested on the other |
| Healthy scratch, by his team's last result (PRs #172, #186, #187) | After a win: −0.35 for the team's next game, −0.15 for the second and −0.10 after that. After a game his team didn't win: +0.20, then +0.15, then nothing. Added after the 16-season weekly model | Mostly a next-game effect, gone by about the third game; the by-game version was more accurate in both 2024-25 and 2025-26 |
| Depth skater (under 13 min F, 16 min D) who fought last game (PR #184) | +0.5 for 5+ penalty minutes in his last game, unless he left it early: coaches tend to keep him in | Such players dressed 7 to 9 points more often than the site gave them, about 410 games a season |
| Depth forward's special-teams role last game (PR #189) | +0.2 when he killed penalties for 1:00+, −0.1 with no power-play or penalty-kill time, applied with the lineup fit | Both held-out seasons improved, at about half the first test's size |
| Healthy scratch who had power-play time in his last game, within 10 days (PR #191) | −0.1, not when Daily Faceoff's lines have him back in | Small gain in both seasons and every half-season |
| Depth skater with the fewest minutes at his position on his team last game (PR #192) | −0.1; other depth skaters +0.1 | Both seasons and every half-season improved |
| Missed his team's last game with no healthy-scratch listing, no ESPN status and no Daily Faceoff news since (handoff 623) | −0.5 every game this week, applied with the lineup fit | Such players dressed 6% of the time against the site's 10%; better in both held-out seasons and every half |
| Daily Faceoff lists him day-to-day (handoff 624) | A further −0.4 on his team's next game only | The news shift over-called the next game: 24% predicted against 18% dressed; better in both seasons |
| Injured reserve, by days to the game (handoff 629) | Capped at 5% for a game the same day, 15% the next day, 25% after that (was 15% for every game) | IR players dressed 5% / 7% / 19%; better in both held-out seasons and every half |
| Day-to-day after full minutes (handoff 630) | Capped at 85% (was 75%) | Such players dressed about 85% |
| Suspended (handoff 631) | 0 for the games still to serve, from the note's stated length; with none stated, 0 / 17% / 44% / 61% / 68% / 82% for games 1-6+ since the listing | 25 suspensions 2021-26: forecast error cut by more than half |
| Lowest-minutes depth player who had a point last game (routine row 362) | +0.2 back on top of the fewest-minutes drop | Kept his spot more often than predicted; better in both seasons and both positions |
| First-round rookie, Season share (handoff 632) | +0.3 on the rest-of-season log-odds for a top-24 pick in his first two seasons with 60 or fewer NHL games | Dressed 64% of the rest against 58% predicted; better in every season tested |
| Healthy regular (PR #179) | +0.5 when he dressed for the last game on regular minutes (13+ F, 16+ D), with no ESPN status and no Daily Faceoff news since: about 95% → 97% | These players dressed for 98% of games over Oct 9-13 while the site gave them about 95% |
| Fresh call-up (PR #176) | Called up in the last week, no NHL game yet this season, no ESPN status: 25% a game (not the 10% for a player who isn't playing) | The 657 call-ups of 2021-26 played 31% of their team's next-week games; 25% beat 10% in every season |
| Home after a road trip, and long absences (PR #176) | On a team's first home game after 2+ straight road games, skaters who missed games +0.1 and the rest −0.6 × (1 − his chance); a skater out 3+ straight games +0.05 for each game further into the week | Cut the Week's dress-chance error in both held-out seasons |
| Per-game lineup fit (PRs #175, #181, #186) | For each team game, open spots = 12 forwards or 6 defensemen minus the sum of the listed skaters' chances (skaters ESPN doesn't list count at the 16-season model's share). With room, each chance moves +1.0 × open spots / 3 on the log-odds scale; over the limit, −2.8 × extra players / 3. Last season's regulars with no game yet who have been in Daily Faceoff's injury or roster news in the last 30 days count as part of a spot, from the team's 3rd game | Cut the dress-chance error about half a point in both held-out seasons (117,700 player-games), more for players out now |
| Just traded or claimed (PR #56) | +0.45 (about 78% → 85%) until his 4th game for the new team | Actual 86% |
| Traded, yet to play for the new team (PR #60) | Old team's games after his last one stop counting as misses; the trade boost applies from his first new game | 40-50% for the week before, about 88% after |
| Barely played his last game, no ESPN flag (PR #65) | Under 40% of his usual ice time: hard cut for the next game, smaller for the one after, still clear for the rest of the week; 40-55%: smaller cut for two games | Under 40%: 18% next game actual vs ~70% shown; Brier 0.43 → 0.16 |
| Just back in the lineup (PR #68) | Back 1-2 games from a 5+ game injury absence: +0.4; back 1-3 games from a 1-4 game scratch spell: −0.4 | 81% vs 73% shown; 77% vs 83% shown |
| ESPN status cleared in the last week, hasn't played since (PR #71) | No games this season yet: 75% next game, 80% later; has played: a clear boost (+0.9) | Cleared in season: 35% actual vs 18% shown |
| Sat last game, no ESPN status, in Daily Faceoff's lines (PR #72) | At least 90% next game, rest of week priced as if he had played | 14 of 14 dressed (early live read, rechecked end of October) |
| A regular returns from injury (PR #33) | The least-used healthy skater at that position on his team loses 8 points (F) or 12 (D) | Measured over 351 returns |
| His team adds a skater at his position by trade or claim (PRs #151, #162) | The least-used healthy skater at that position (fewest minutes last game, no ESPN status or day-to-day) loses 8 points (F) or 12 (D) until the newcomer has played 3 games for the team; not stacked with the returning-regular cut | Sized against the site's own lineup model, 2023-26 |
| A defenseman who played one of the team's last 3 games is traded or claimed away (PRs #151, #162) | The team's healthy defensemen (no ESPN status) who sat the last 1-4 games gain 10 points, for the old team's next 3 games | Forwards showed no clear lift, so they are left alone |
| Idle 15+ days while his team kept playing | Capped at 50% (15-21 days), 20% (22-35), 10% (longer) | |

**Team form and low minutes (PRs #156, #158).** These add to each game's chance this week (in points of chance, not log-odds), and they stack. A depth skater here is healthy, dressed his team's last game and played under 13 minutes (F) or 16 (D) in it.
- After 4+ straight games without a win, depth skaters drop 3 points (F) or 6.5 (D); after a loss by 4+ goals they drop 7 instead (the larger of the two, not both).
- When the team has won 3+ straight, its skaters who missed the last 1-4 games drop 7 points.
- Low minutes in his last game: defensemen −2.5 under 12 minutes, −5.5 at 12-14, −2.5 at 14-16; forwards −2 under 8 and −3 at 10-12.
- In a team's first 15 games, depth skaters (under 12 minutes F, 16 D) drop a further 3 points.

On a game-by-game replay of the last two seasons these made dress forecasts 0.58% more accurate, better in both seasons, and players with two or more shifts stay calibrated (77.5% predicted, 78.1% actual).

**Daily Faceoff news this week (PRs #164, #166, #169, #170).** Each run reads Daily Faceoff's injury and roster-move items (`data/dfo_news.json`, from `scripts/leads.py`). Items posted after his last game's puck drop shift each game's chance on the log-odds scale:
- He missed the last game: practising (full or no-contact) +1.55, and while he is practising an ESPN OUT or IR cap becomes day-to-day's 50%; otherwise skating on his own +1.19. Game-time decision or could return +0.37; his latest item says he will return +0.5; day-to-day +0.16; week-to-week −0.21; season-ending, indefinite, LTIR or month-to-month wording −0.7. A stated timeline with more than 7 days still to run at a game cuts that game by 1.25; one that has already run out adds 0.17. Stat lines ("52 points in 58 games") are no longer read as timelines (PR #169).
- Sent down to the AHL in his latest roster move: −1.1 if he sat the last game, −1.4 if he played it.
- He played the last game, ESPN has no status yet, and news since says he is out (−2.1) or day-to-day (−1.9).

On 2024-26 replays, fit on one season and scored on the other, the news cut the error on out-now players' games by 5.2% and 5.8%. A player who hasn't played yet this season gets news counted from the season's first game. The details mention the news when it moves his chance by 2 points or more.

**The 16-season weekly lineup model (PRs #163, #165, #170).** A boosted model trained on 2010-26 predicts the share of next week's team games he plays, from the same inputs as the Season dress model (part 4) plus his latest Daily Faceoff injury item (`model/season_boost_week.json`). The order on each game's chance:
1. The lineup-history chance with the shifts that come before the model (depth point, trade, comeback and cleared-status shifts, held under ESPN's cap), then team form and low minutes.
2. The boosted model, measured against that chance before any news: each game's log-odds move 0.9 × (logit of the model's share, clipped to 0.5%-99.5%, − logit of his average chance this week).
3. After it, together: this week's Daily Faceoff news, any send-down, and the healthy-scratch lifts (the basic scratch lift, a teammate who left early, and the last result), all held under ESPN's injury caps (part 3).
4. The Daily Faceoff lineup floor and the 3% for an OUT player's same-day game.
5. The road-trip and long-absence shifts, then the per-game lineup fit with the healthy-regular, fight, special-teams, power-play-scratch and fewest-minutes lifts, the missed-with-no-status cut and the day-to-day next-game cut.
6. The returning-regular, newcomer and departed-defenseman changes to the week's average.

Measuring the model after the news (as PRs #165-#169 did) let it take back about 90% of what the news said; the PR #170 order wins back 30-50% of the news gain. PR #185 moved the scratch lifts after the model for the same reason. With the news, per-game error fell 5.2% and 9.8% in 2024-25 and 2025-26, and weekly ranking rose 0.008 every season. Not used for a skater ESPN lists on a team he hasn't played for yet, since the inputs describe his old team. The details show the model's effect when it moves his chance by 5 points or more.

Fallbacks: before his team has played, 90% if he played last season (50% if not). A player not on any NHL roster or stat sheet gets 10%, and so does an unsigned free agent with no NHL team or games this season (Kane, Tarasenko; PR #92, before which they rated as 90% likely to dress). A fresh call-up yet to play gets 25% a game (PR #176). A name with no NHL match, no ESPN games this season or last and no ESPN projection (retired and minor-league players ESPN still lists with a team) is rated 0 (PR #173).

**What it changes:** expected games this week (the sum of the per-game chances), which scales the Week rating. Shown as `cr_dress` (the week's average) and `cr_dress_next` (next game, used by the Tonight card).

**How much it matters:** more than any modeling choice. Discounting for likely scratches added about +0.5 points per pickup per week, and cut picks who never played from 6-8% to 1-2%. The lineup-history version lifted the weekly ranking from 0.637 to 0.652 (waiver pool 0.573 to 0.594).

**What it doesn't affect:** points per game, the Season rating (part 4 has its own model, with matching trade, left-early and cleared rules), goalies.

## 3. ESPN injury status

**What it uses:** ESPN's status on each player at every update, the latest ESPN injury note, and for day-to-day and OUT skaters his ice time in his team's last game.

**Skater caps (a ceiling on each game's chance, not a second discount):**

| ESPN status | Cap |
| --- | --- |
| Day-to-day, missed the last game | 50% |
| Day-to-day, played the last game (PR #74) | 15% if under 55% of his usual ice time, 50% at 55-80%, 85% at 80%+ (PR #199) |
| OUT | 35% (15% if he played the last game but under 55% of his usual ice time, PR #77); 3% for his team's game the same day (PR #99); 50% once his team has played the one game the note named |
| Injured reserve | 5% for a game the same day, 15% the next day, 25% after that (PR #199) |
| OUT or IR, and Daily Faceoff says he is practising (PR #164) | 50%, like day-to-day |
| Suspended | 0 for the games still to serve (length from ESPN's note, counting his team's games since it); no stated length: 0 / 17% / 44% / 61% / 68% / 82% for games 1-6+ since the listing (PR #199); a note saying indefinitely or rest of the season with no length stays 0 |
| Note says surgery, season-ending, month-to-month, indefinitely or no timetable | 0 ("unclear when he'll return" alone keeps the status cap, handoff 672) |
| Note's only long word is week-to-week | 5% for a game today or tomorrow, 20% after that (handoff 657) |
| Note's only long reason is "several" or "a few weeks", or a return 14+ days away | 10% today or tomorrow, 30% after that (handoff 657) |
| Note says LTIR (with any other long word but surgery or season-ending) | 5% (handoff 673) |

The softer OUT and IR caps shipped in PR #24; before that OUT and IR were 0.

**Goalies:** OUT, IR and suspended scale his chance of starting each game to 0, day-to-day to half, and the starts he loses go to his healthy partner (part 6).

Tapping an injured player shows "usually back in about N games" (or the note's timeline). Every override is logged daily to `data/injury_log.jsonl`.

**How long players are out** (41,905 ESPN injury notes, 2024-25 and 2025-26; median further team games missed from a Monday):

| What's known | Usually misses |
| --- | --- |
| Day-to-day, hasn't missed a game yet | 0 more games (78% back within 4) |
| Day-to-day, missed 1-8 | 2-4 |
| OUT | 3-5 |
| Injured reserve | 9-14 |
| Surgery or LTIR | about 16 |

**How much it matters:** ESPN's flag removes the lag in the dress model, which is built from games already played (on Oct 8, six OUT players were still rated 87-95% likely to dress). The softer caps lifted the weekly ranking by +0.007 overall and +0.009 in the free-agent pool (an independent rebuild found +0.011 to +0.013). The minutes-based day-to-day caps cut prediction error by a third in both seasons (players who left their last game hurt dressed 8-24% of the next games; full minutes 79-97%). The gain is mostly in how your own injured players are projected: drop list, Mid-week swaps, who to bench.

**What it doesn't affect:** the Season rating (ESPN's status doesn't cut it, so an injured star still rates as a star there, though Daily Faceoff news of surgery, a season-ending injury or a send-down does; since PR #202 so does ESPN's own stated timeline or out-for-the-season note for a skater out now; part 4), points per game. A detailed model by body part and stated timeline did worse than the simple caps, and caps rising by game number tested worse too.

## 4. Rest-of-season availability (Season rating, skaters)

**What it uses:** everything part 2 uses, plus last season's games played, ice time, games his team has left out of 84, and a lineup chain: how often he drops out after a game he plays, and how likely players are to come back after k games out (forwards: 31% after one game out, 17% after three, 7% after seven). Adjustments:
- **Hurt before playing this season (PR #36):** 45% of remaining games, 30% after a long injury note, 0 if ESPN says out for the season (66 such regulars dressed for 41%).
- **Suspended before playing (PR #44):** 45%. **Healthy skater on an NHL roster yet to play (PRs #141, #142):** a share from a table by his games last season (1-19, 20-39, 40-59, 60+) that falls the longer his team plays without him (a 60+ game player 72% before his team's 4th game, 8% after 35 games). Players hurt before playing hold their share through 7 team games, then fall the same way.
- **First 10 games for a new team (PR #59):** +0.3 on the log-odds scale (about 6 points of dress share; actual 71-72% vs 62-67% shown).
- **Barely played his last game (PR #66):** −0.4 until he plays a normal game (60% actual vs 69% shown under 40% of usual ice time).
- **ESPN status just cleared (PR #71):** 72% of his points a game if he hasn't played this season; a +0.4 boost if he has.
- **Durable players out 6+ games (PR #138):** a skater who dressed for most of last season comes back sooner than the lineup chain expects; a high scorer slightly later.
- **New and part-time skaters who stick (PR #142):** a player with no NHL games last season, or 1-40, who has stuck this season keeps his spot (up to 40 games); one new to the NHL with 5 or fewer games is expected back down more often.

**Daily Faceoff news (PRs #144, #149).** On the log-odds scale, from Daily Faceoff items since his last game: sent down and out since, −0.82 + 0.18 × ln(1 + team games out); called up in the last 60 days and in the lineup, −0.18 + 0.18 × (games this season, capped at 40) / 40; season-ending wording −1.5 (otherwise "out indefinitely", "long-term" or "several months" −0.3); surgery −0.33. If the news can't be read for two days these switch off. Tested on 2023-26, the news ranks rest-of-season points 0.0018 better in every season (free agents 0.003).

**ESPN's note for a skater out now (PR #202).** He missed his team's last game with an ESPN status: −0.6 × min(stated days left / 2.2 / his team's games left, 1) on the log-odds; an out-for-the-season note −3.0 unless Daily Faceoff's season-ending item already applied (findings 488).

**A boosted model trained on 16 seasons (PRs #146, #165).** A gradient-boosted model fitted offline on 2010-26 game logs (games and ice time, recent absences, last season, age, draft slot, NHL seasons, teammates out at his position; `model/season_boost_dress.json`) predicts his share of the rest of the season. The Season log-odds move 56% of the way to its answer; the news above is added after this step. It also trains on each season's first two weeks, which lifted the early-season Season dress ranking from 0.452 to 0.471.

**Open lineup spots, or lineup slack (PRs #148, #165).** A team dresses 12 forwards and 6 defensemen. For each team and position, slack = spots − the sum of every skater's Season share (players ESPN doesn't list count through the boosted model's share). Each skater's log-odds then move by +1.34 × slack / 3 × (1 − his share) when spots are open, or −2.84 × the extra players / 3 × (1 − his share) when it runs over, so depth players move and regulars near a full share barely do. A player Daily Faceoff reports sent down gets no lift from open spots, and a group with fewer players than spots (missing logs) is left alone. On top of the news and boosted models: +0.0007 rest-of-season ranking, +0.0020 among free agents, positive every test season.

**Missing regulars (PRs #180, #182).** Last season's regulars (40+ games) with no NHL game yet this season, usually because they are hurt, take games back from the players filling in when they return, and the slack above can't see them. From his team's 3rd game, each skater's log-odds move by −0.08 × (those regulars' last-season ice time a game / 15 minutes for forwards or 20 for defensemen, summed) × 20 / (20 + team games), so the dip fades as the season goes on. A regular counts for last season's team, whatever his status now: moving him to his current team, or leaving out players no longer on an NHL roster, tested worse. +0.0003 rest-of-season ranking (free agents +0.0007), every test season.

**What it changes:** the share of his team's remaining games he's expected to play, which multiplies the Season points per game (part 1) into the Season rating. It also sets Proj GP.

**How much it matters:** rest-of-season ranking 0.773 → 0.797 (waiver pool 0.671 → 0.701), more with injured players included. Knowing each player's true share of games played would reach 0.91, against 0.89 for knowing his true points per game, so availability is where the Season rating's error is. Players out two weeks or more keep a realistic Season rating (about 27% of remaining games, not 7%).

**What it doesn't affect:** the Week rating, goalies, ESPN's injury caps.

## 5. Games left

**What it uses:** the NHL schedule for the matchup (the length comes from ESPN, so a two-week playoff round counts both weeks, PR #41), counting only games still to come:
- **Today's date comes from ESPN's scoring period (PR #79),** which rolls over at about 3:10-3:40 AM ET, not the clock. The publish check refuses a week whose nights don't line up.
- **Games already started (PR #75):** a data run doesn't count a game that has started, and the page, using your device's clock, takes it out of the player's games and Week points and out of tonight's cards.
- **Adds after the day's first puck (PR #80):** ESPN holds such an add until its overnight rollover, so for free agents and waiver players today's game is gone once the first game of the day starts (all 6 such logged adds joined the next day). Your own players count until their own game starts.
- **Waiver players** count only the games after they clear (tag: "waivers, clears Sun 11").
- **The schedule comes from ESPN's fantasy view (PR #97).** A misspelled view name had sent every run to ESPN's public scoreboard, which dates nights by the clock. Nights with no NHL games (Thanksgiving, Christmas, the Olympic break) now keep ESPN's date and don't count as light nights.
- The data refreshes at about 6 AM, 9 AM, 12:40 PM and 5 PM ET; when the page shows an earlier day's data, a note says so and tonight's cards are hidden.

**What it changes:** the Week rating for everyone (skaters via games × chance to dress, goalies via the games their starts are spread over). It's why a 4-game week outranks a better player with 2 games.

**Data-run safety (PRs #91, #97):** every update is checked before it publishes (games left match the nights, expected games within games left, Week = points a game × games, goalie starts within team games, nights on their dates; 6, 7 or 14 nights by matchup length). A run with fewer than 100 free agents or 10 injury statuses is blocked, and the site keeps the last good data. When ESPN's injury notes or Daily Faceoff fail, the run reuses the last copy from the past day.

**How much it matters:** a top pickup scores 6.1 in his first week and about 4 in later weeks; most of that edge is schedule. On Oct 10 the stream card had offered goalies whose games had already started. The light-nights dots and **Open** column are shown next to the rating but don't change it (an empty UTL counts as room for a forward or a defenseman, PR #176): matching pickups to open nights didn't beat taking the best projected weeks.

**What it doesn't affect:** the Season rating, points per game.

## 6. Goalie expected starts

**What it uses:** a start model (logistic) for each remaining game: his share of the team's last 5, 10 and 20 starts and the season, last season's share, whether he started the previous game, back-to-backs, his run of straight starts, days since his last start, and team games and his starts in the last 7 days. Only goalies on the team's current NHL roster (plus a Daily Faceoff confirmed starter) share the starts (PR #61). Adjustments on top:
- **Early season (PR #14):** until his team's 10th game, shares are blended with last season's split. The top goalie's prior share of last season's starts is capped at 70%, the rest going to his partners (PR #99, replacing a 70-start floor from PR #92), so a no. 1 whose old partner left (Swayman, Sorokin, Vejmelka) isn't treated as a near-100% starter.
- **Last game's result (PRs #46-#48, #63):** after a bad start (5+ against, or pulled after 3+) his next-game chance drops and part of the drop carries to the game after; after a win it rises (a bit more after a win allowing 0-1), after a loss it falls.
- **Left early with no decision (PRs #63-#64):** about 12% for the next game, and the cut lasts the rest of the matchup (easing after the second game), until he starts or is confirmed.
- **Came on in relief last game (PR #69):** higher chance next game and a smaller boost later, more for a goalie with 0-3 of the last 20 starts.
- **Long team break (PR #74):** the last starter gets a boost for the first game after 4+ days off, bigger after 7+.
- **Second night of a back-to-back, night 1 played (PR #84):** the night-1 starter's chance for night 2 goes down if he is the team's no. 1 (most starts in the team's last 20) and up if he isn't; through the team's 10th game a no. 1 gets an extra nudge toward resting (PR #88).
- **Five to seven straight starts (PR #85):** the starter's next-game chance is trimmed (about 9 points) and the backup's raised to match; runs of 8+ are left alone.
- **Traded or claimed (PR #57):** +1.0 on the log-odds scale until his 4th start for the new team.
- **Games 2+ ahead (PR #48):** pulled a little toward an even split, since the chained week ran overconfident.
- **Injured goalie (PR #17):** his chance shrinks (part 3) before each game is split, so his partner gets his starts.
- **Back-to-back the no. 1 hasn't played yet (PR #100):** his chance on night 1 dips a little and on night 2 more, later in the week too; and after he rests night 2 the other goalies are cut for the following game, so he is back in net (PR #105).
- **Losses graded by goals against (PR #109):** a regulation or overtime loss giving up 3 cuts his next-game chance a bit more, 4 more still (−0.35 / −0.65 on the log-odds scale; a bad start keeps its own bigger cut); since PR #196 a 3-4 goal loss also cuts his game after next (−0.2).
- **Strict A, B rotation (PR #112):** when the last 6+ starts alternate, the last starter's next-game chance drops (−1.2) and the goalie due next is trimmed for the game after (−0.6).
- **Repeating A A B or A A B B patterns (PR #113):** the last starter's next-game chance follows where the team is in the pattern (+0.8 when it keeps him in, −0.4 / −0.8 when it switches).
- **Telling last-six strings (PR #117):** with two goalies sharing the last 10 starts, the other goalie is cut next game when the last starter's string says the no. 1 is back after a spot start (−1.0), took over with three straight (−1.2), or the pair rotates after a double (−0.6).
- **Three-goalie teams (PR #119):** when three goalies started the team's last 10, any of them idle for the last 3 team games is cut (−1.0); the team's no. 1 gets +0.5 in every game after the next (PR #196), since such no. 1s started 1.45 games a week against 1.32 given.
- **Rested no. 1 (handoff 363):** on every night that isn't night 2 of a back-to-back, the team's no. 1 gets +0.05 on the log-odds scale of his shown chance (rested no. 1s started 0.615 of next games against 0.586 given; better in 15 of 15 seasons).
- **Later in the week (PR #126):** the last starter is cut in a strict rotation's third game (−0.9) and, when no rotation, pattern, last-six or three-goalie rule fires, in the game after next (−0.45).
- **The no. 1's schedule and form (PRs #140, #149, #151, #153):** a Sunday or Monday game after one day off (−0.6) and road games that aren't night 2 of a back-to-back (−0.3); up against strong opponents and down against weak ones, down after straight starts without a win, up after 3+ days off; back in net after splitting a back-to-back when the next game is 2 days later (+0.8) or after the backup started and didn't win (+0.8), a little less likely after the backup's recent win (−0.6); and his dip on the first night applies to every back-to-back still ahead this week.
- **No start yet, early season (PR #172):** through his team's 10th game, a roster goalie who hasn't started for the team yet is cut (−0.75) in every game this week (third goalies had read 25-33% for Oct 13 and started none).
- **Even tandems (PR #172):** when the team's top two goalies are within one start of each other over its last 20, the no. 1's back-to-back shifts are half size, since there is no clear no. 1 to rest.
- **Backup confirmed for night 1 (PR #171):** when Daily Faceoff has confirmed the backup for night 1 of a back-to-back, the no. 1 is not marked down for night 2.
- **Daily Faceoff:** a goalie listed as **Confirmed** for today or tomorrow gets that game as a certain start and his partner zero, overriding every shift above. Names spelled differently (Matt / Matthew Murray) still match (PR #70). A goalie tagged **Likely** gets at least 90% for that game and his partners split the rest (PR #194); unconfirmed is shown on the row but doesn't change the number. A confirmed starter the site can't match to NHL data (a call-up) still sits the team's other goalies that game (PR #92).

**What it changes:** expected starts (`exp_starts`), which multiplies points per start into the Week rating. It also gives tonight's start chance used by the stream card.

**How much it matters:** goalies are almost entirely about starts.
- Weekly start error: 1.78 if you assume every team game, 0.58 with the start model, 0.56 with rest and workload. Ranking wire goalies: 4.9 points a week by expected starts vs 3.0 by points per game × team games (random 2.9).
- Early-season blend: error in a team's first 4 games 0.84 → 0.67 starts per goalie. Injured starter: backup 0.98 → 2.38 projected starts per 4 games (actual 2.24).
- After a bad start the goalie started 31% of next games vs 44% shown (about 0.4 points per goalie). No decision: 12% actual vs ~45% shown, about 20 times a season; over a four-game week about one start moves to the partner. Relief: 58-75% next game vs 45-65% shown. After a 7+ day break the last starter started 63% vs 47%.
- Later-game softening cut no. 1 overstatement from 0.21 to 0.08 starts per four games. Traded goalies: 32% next start actual vs 20% shown.
- Back-to-backs: a no. 1 who played night 1 started night 2 11% of the time against 18% shown, a backup 8.5% against 4% (error down 2%, 5 of 5 seasons); in a team's first 10 games 3% against 7% shown. Five to seven straight starts: 56% next start actual against 70% shown (error down 2.6%, 5 of 5 seasons).
- Limiting the early-season prior (first a 70-start floor, now the 70% cap): error halved in a 2022-26 backtest (0.186 to 0.092, 4 of 4 seasons).
- Confirmed starters: worth about a third more points per streamed goalie (+0.76 per day); Confirmed was right 54 of 56 times in the first live days.

**What it doesn't affect:** points per start, skaters.

## 7. Goalie points per start

**What it uses:** his own fantasy points per start this season, blended over 15 starts with his last-season rate (itself pulled toward the league average of 2.9 over 15 starts; 2.9 if he has none, PR #32). Last season's rate counts started games only, since ESPN scores relief appearances 0 in this league (PR #174). Then:
- **This week's base (research section 105):** his own rate already carries his team's strength, so for this week's starts the site keeps only 0.4 of his gap to the league's rate this season (pulled toward 2.89 over 300 starts): base = league rate + 0.4 × (own rate − league rate).
- **Tonight's game:** base + 4.1 × (win chance from the DraftKings moneyline via the NHL's site − 50%) (PR #10). Morning updates already price tonight's start with ESPN's betting line when the NHL's isn't posted yet (PR #101).
- **Team strength counted once (PR #102):** this week's points per start no longer adds a team-strength term on top of the line or schedule pricing.
- **Later games this week:** base + 3.0 × (win chance − 0.5), with the win chance from each team's goal differential (shrunk to half of last season's over 15 games) and home ice (PR #35; the slope was 3.4 before the 0.4 keep). Tap a goalie to see "soft schedule" or "tough schedule".

**What it changes:** the points-per-start multiplier in the Week rating, and in the Season rating (own rate only, no line, schedule or 0.4 keep; a goalie who changed teams over the summer has the last-season part moved by 3 × the gap in points% between his new and old teams, PR #139).

**How much it matters:** points per start is mostly unpredictable; season win %, goals against and shots against don't predict it (all under 0.1). The betting line does: starts with a 55-65% win chance averaged 4.2 points, under 35% 2.8. The schedule pricing lifted the weekly goalie ranking 0.273 → 0.297, better in all five seasons. The 0.4 keep cut per-start error 1.5% (5 of 5 seasons) and weekly goalie error 0.3% over 13,120 starts, with the ranking flat. The last-season starting point added +0.022 rank correlation for the Season rating, mostly in a team's first 20 games. His own shrunk rate adds +0.14 points a week on the best streaming picks.

**What it doesn't affect:** starts.

## 8. Goalie Season rating

**What it uses:** his share of his team's starts: three quarters from this season (blended with last season's split over 10 games) and a quarter from recent form, each game counting half as much every 5 team games back (PR #37). Then:
- × **0.88** for every goalie (PR #76, was 0.93 in PR #62): starts that go to goalies not on the roster yet, and shares spreading over a whole season.
- A **traded or claimed** goalie's share counts only the team's games since his first start for it (PR #58).
- A goalie who **left his last start with no decision** gets × 0.68 until he starts again or is confirmed (PR #67). The share he loses goes to his healthy partners (PR #92), and a goalie ESPN already lists as out isn't cut twice.
- A goalie **out for the season** (from ESPN's longer note, in sentences that name him) gets a Season of 0, and his share goes to the team's healthy goalies (PR #92: Shesterkin had topped the waiver goalies at 1.41). A short-term injured partner takes his part at half weight, and a healthy partner with any share weighs at least 0.1, so he always gets some of it (PR #171).
- **Save-percentage lean (PR #131):** + 0.008 + 0.0485 × (his shrunk save% minus the start-weighted save% of the team's other starters this season, in percentage points). A healthy goalie keeps at least the smaller of his share and 0.03, so a weak save% can't push a working backup to zero (PR #171).
- **Pulled in his last start (PR #137):** under 40 minutes in his latest start, without the no-decision cut: −0.03 of share.

Times his points per start (part 7, own rate only, with the summer-mover adjustment). Then **rescaled onto the skaters' scale (PR #135):** Season = 0.32 + 0.72 × (share × points per start) for any goalie with a share. Goalie Season values had run wider than what goalies went on to score (top eighth 2.55 against 2.14, bottom 0.21 against 0.33).

**What it changes:** the goalie Season rating only.

**How much it matters:** the recent-form quarter made rest-of-season errors 11% smaller right after a starter change (+0.013 rank correlation). The 0.88 scale cut the rest-of-season error from 0.545 to 0.537 points a game and moves every goalie by the same factor (a no. 1 drops about 0.15 points a game), so it matters where goalies are weighed against skaters. The trade fix cut the error for movers by about a third (a 1-3 start mover went from 11% to 29-31% of starts). The no-decision trim is worth about 0.5 points a game of Season rating for a no. 1.

**What it doesn't affect:** the Week rating. ESPN's day-to-day, OUT and IR flags don't discount it; the only injury rule is the out-for-the-season 0 above.

## 9. Keep value

**What it uses:** Week + 7 × Season, for skaters and goalies (PRs #22, #43). In the final playoff round it is the Week rating alone, since nothing after it matters.

**What it changes:** how the Upgrades cards, Best moves, the drop list, the Goalies card and Trade check rank players. The Week and Season columns are unchanged.

**How much it matters:** +1.75 pts/roster/week (2 Monday moves) and +2.3 (6 moves) over 8-week stretches, against ranking adds and drops by this week alone. Goalies: in 828 three-goalie decisions keep value picked a different goalie 25% of the time, and the one it kept was worth about 1 point a week more. Other weights (this week × 0.5 to 2, or blending in weekly per-game value) all did worse.

## 10. Percentile badge

**What it uses:** where the player's rating falls among all forwards, defensemen or goalies on the site (rostered and free agents), separately for Week and Season. 90+ is shaded green, 70+ light green.

**What it changes:** nothing. The site sorts by projected points, not percentile.

## 11. The advice layer

These cards use the rating; none of them change it. All are for the team picked at the top of the page, in the order they appear.

| Card or advice | What it does | Measured effect |
| --- | --- | --- |
| **Move plan** (Upgrades header) | 7 moves a matchup (6 in the 6-day first one, 14 in a playoff round): on Monday add every skater worth 3.5+ keep points over your weakest at his position (usually 1-3), and keep 3 moves (2 in the 6-day first matchup, 6 in the two-week final) for injury swaps and goalie streams; carry 2 goalies plus a stream slot (PR #174) | 2 Monday moves plus swaps and streams: +5.6 pts a week over all 7 on Monday, and the re-tuned plan a further +0.9 a week in every season tested; 2 goalies plus a stream slot beats a third regular goalie by about 2 more |
| **Activate from IR** | A healthy player sitting in your IR slot, whose points don't count | Celebrini's 2.9 points were lost this way on Oct 10 |
| **Two-week playoff round** | One pool of moves for both weeks; on Monday add every skater worth 3.5+ keep points, and keep 3 moves (6 in the two-week final; PR #197) for injury swaps and goalie streams | A week with no moves ran about 20 pts behind |
| **Waiver claims** (PR #94) | A player on waivers gets "put in a claim": a losing claim uses no move. Shows your place in ESPN's waiver order; a drop with Season 1.5+ is tagged as one others will claim | Every losing claim in the league's logs was cancelled without using a move |
| **Mid-week swaps** | Your skaters who are out, suspended, or sat the last game (unless back in Daily Faceoff's lines), next to the best free agent at their position; IR-eligible noted when an IR slot is open | +1.1 to +1.4 pts/roster/week from holding moves for this |
| **Stream a goalie tonight** | When fewer than two of your goalies start tonight: free-agent goalies confirmed to start, ranked by tonight's line, with moves left. Before first puck it says the time to add by; after it, that a goalie added now plays from tomorrow. Waiver goalies and started games are left out | Each of the first 2-3 streams adds 2-4 pts; which confirmed goalie matters little (~0.4) |
| **Tonight** | On a crowded night, start by per-game rating × next-game dress chance, bench the rest; skaters not in Daily Faceoff's lineup or who sat last game are flagged. Started players in the lineup fill their slots first (PR #87), but not those on ESPN's bench (PR #96); F and D counted separately against 9 F and 5 D plus the UTL | +0.9 pts a week, mostly from benching flagged players; before PR #96, 21 of 84 Saturday checks benched players still to play |
| **Best moves** | Every free agent worth 3.5+ keep points over your weakest player at the same position, up to 4 a day, holding one move for each night still to come (up to 3; 2 in the 6-day first matchup, 6 in the two-week final; PR #197), never more than your moves left (PR #174). Skips anyone Mid-week swaps already drops and counts the swaps' adds against moves left (PR #96). On the matchup's last night before its first game, lists every move left and says unused moves expire tonight (PR #95) | +0.9 pts a week over the best free agent overall; holding 3 moves instead of 5 gained about 0.9 more on the move simulator (552 paired runs), every season tested; Sunday's extra moves were worth 2.8-9.1 keep points per team |
| **Forwards / Defense / Goalies** | Free agents whose keep value beats your weakest at that position by 0.5+ | The Monday plan beats daily streaming by ~2 pts a week |
| **Drop list** | Your five lowest keep values, each with the best free agent at his position and the gain, keeping at least 11 healthy F and 6 D (handoff 643) | Doubles each move's gain: +3.6 vs +1.7 dropping by season points |
| **IR stash** | With an open IR slot: up to 3 OUT or IR free agents at 1.5+ pts a game | 15-18 pts rest of season at 1.5-1.8, about 33 at 1.8+ |
| **Trade ideas** | Up to 5 offers where ESPN projects both players within 0.05 a game but Season differs by 0.3+, across positions if both your roster and the partner's stay at 11-13 F and 6-8 D (partner side, PR #87) | About 1 pt a week per trade, better player 70% of the time |
| **Goalies** | With 3 healthy goalies: the third spot is your stream slot, drop the lowest keep value for tonight's confirmed starter; with 4+, use the extra spot on a skater. After the day's first game, says a stream counts from tomorrow (PR #87) | Literal "spot on a skater" advice cost about a fifth of a matchup win every 8 weeks |
| **Trade check** (own panel) | Sums Season ratings for any trade; shows about 2.1 × gap pts a week (simulated trades, handoff 642) and how often a gap that size picked the right side (53% to 87%). Never drops a goalie to make room unless goalies are in the trade (PR #87) | Calibrated on 2023-26 skaters; goalies rougher |

**Labels and phones (PRs #93, #96):** gains say what they measure ("+7.4 keep", or "+1.1 Season" with no moves left). On a phone the table fits the screen, each card's explanation folds behind "why" (except the Stream, Tonight, Forwards, Defense and Goalies cards, whose lines carry the deadline and who to bench or drop), and the waiver, injury and team tags wrap on their own line under the name.

**Ties in the table (PR #87):** players tied on the sorted column (every free agent's Week is 0.0 from Sunday's first game until Monday's update) are listed by keep value. Data from a matchup that already ended no longer says no moves are left.

**No moves left:** when your team has used all its moves, the add cards say "No moves left this matchup; these are next Monday's targets" and rank free agents by 7 × Season, waiver players who clear before Monday are dropped from the list, the stream card is hidden, and the Goalies card says to stream from next Monday.

## 12. Predicted winners tab

A second tab on the page (Jack's ask, Oct 14 2026; `scripts/winners.py`, findings 450-451). It changes no rating.

**NHL games.** Each game this week gets the home team's win chance from a logistic model on home minus away. Since PR #172 it uses each team's expected-goal differential from MoneyPuck:

| Input | How it's built | Coefficient |
| --- | --- | --- |
| Lineup points | As in the table below | 0.157 per point |
| Expected-goal differential | MoneyPuck's xGF − xGA per game, shrunk over 15 games toward half of last season's: (xGF − xGA + 15 × half of last season's per game) / (games + 15) | 0.491 per expected goal |
| Goal differential | As below | 0.055 per goal |
| Goalie | As below | 0.215 per goal saved |
| Back-to-backs | Played the day before | home -0.267, away +0.261 |
| Home ice | Intercept | 0.148 |

Fit forward on 2011-26, each season predicted only from a model trained on the seasons before it, it was right 60.5% of the time against 60.1% for the earlier model (below) in the same test, sharper in all of the last five seasons and 11 of 12 since 2014-15. On the 1,651 games with archived closing lines its log loss was 0.645 against the market's 0.648, slightly ahead of the line. Expected goals take over most of goal differential's weight, and the starter's save% carries the goaltending.

When MoneyPuck can't be read, the picks fall back to the earlier model:

| Input | How it's built | Coefficient |
| --- | --- | --- |
| Lineup points | Sum over the team's NHL roster skaters of points per game (this season shrunk 40 games toward last season's; a depth skater's rate without 10 games last season) × the site's chance he dresses, scaled to 12 F and 6 D; depth players ESPN doesn't list get 0.85 / 0.4 / 0.1 by whether they played the team's last game, one of its last five, or neither | 0.178 per point |
| Goal differential | The goalie model's team strength: goal differential per game, shrunk over 15 games toward half of last season's | 0.351 per goal |
| Goalie | The starter's shrunk save% (the goalie model's), weighted by each goalie's chance to start that night, × 30 shots | 0.027 per goal saved |
| Back-to-backs | Played the day before | home -0.296, away +0.349 |
| Home ice | Intercept | 0.118 |

Fit on 6,560 games 2021-26, scored leave one season out: 60.0% right and log loss 0.662, against 53.7% and 0.691 for always picking the home team and 59.0% and 0.667 for goal differential alone. On the 1,651 games with archived closing moneylines (2021-22, 2022-23 to Nov 27) it was right 63.2% (log loss 0.649) against the betting favourite's 63.1% (0.648); its chances correlate 0.92 with the market's. Calibrated within 2 points in every band from 30% to 75%. The backtest used the actual dressed skaters and starter; on the site they're expected values, which Daily Faceoff confirms for nearly every game on the day.

Once a game starts its pick is frozen at the last pre-game value (`winners.freeze` reads the published file), so the week's record ("7 of 11 right") counts only real picks; a run that fails to get the NHL games keeps the week's locked picks (PR #170). Tonight's betting line is shown next to the pick for reference; it is not an input.

**Fantasy matchups.** Each league matchup: ESPN's points so far plus each roster's rest-of-week projection, filling the best lineup each night (9 F, 5 D, UTL, 2 G, IR left out) from the Week ratings (a skater's Week spread evenly over his nights left, a goalie's start chance by night × his points per start). The win chance assumes independent nights with a spread per player-game of 0.4 + 0.5 × FP per dressed game for skaters and 3.33 per goalie start (2021-26 player-games), widened 4% because whole matchups swing more than their players add up to (PR #178). Not backtested (no past league weeks); graded from the logs as the season goes.

## Built but switched off

A hand-written news hook (`data/news.json`) can adjust up to 25 players' games, starts or points per game (capped at ±20%). No news file is published, so it changes nothing today. Daily Faceoff covers what it was meant for: confirmed starters for goalies (part 6), and injury and roster news for skaters' Week and Season chances to play (parts 2 and 4).

## Tested and left out

Each was checked over past seasons and moved the ranking by no more than a rounding error, or made decisions worse. Some ideas once listed here have since shipped in a new form: two-seasons-ago stats (PR #111), age (PRs #116, #122), finishing luck (PR #118), next man up on the power play (PR #109), boosted models (PRs #146, #163) and open lineup spots in place of a roster-crunch input (PR #148).
- **Skater points per game:** opponent strength (beyond the early-season term on last season's goals against, PR #177) and home ice, back-to-backs, 3-in-4s and travel, the opponent's starting goalie, team shooting pace, PDO corrections, PP1 labels and PP-unit signals, separate forward and defense models, primary vs secondary assists, a boosted-tree blend (+0.002), leaving short games out of every window, a dip after returning from injury, teammate-based expected points.
- **Season rating experiments (Oct 11):** Marcel-style weighting, time-decayed game logs, role-based shrinkage, deployment trends, luck regression, ensembles, a rookie prior and recalibrating the dress share. None beat the ridge model. (Boosted models trained on about three seasons lost too; trained on 16 seasons they won and shipped, parts 1, 2 and 4.)
- **Availability:** a crowded-lineup correction on top of the weekly model (handoff 409; a per-game version shipped in PR #175), coach-specific scratch habits, centre vs winger, prior injury history, injury caps rising by game number.
- **Goalies:** team goalie rotation habits, pts/start built from save % and team strength, Marcel goalie weighting, recent form, shutout and blowup tendencies, rust after rest, recalibrating start chances.
- **Moves and display:** keep value counting the real schedule for weeks 2-3 (shipped in PR #38, reverted in PR #39: about −0.9 pts a week in a season-long replay), choosier goalie streams, an extra 6:40 PM data run, a confidence badge, overtime rates, arena scorekeeper bias.
