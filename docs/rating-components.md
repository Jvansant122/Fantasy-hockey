# The Claude Rating, piece by piece

What each part of the rating does, what it uses, how much it was measured to matter, and what it leaves alone. Written for a fantasy manager; the research behind every number is in [the research write-up](research.md), and every idea tried is in the [ideas ledger](ideas-ledger.md).

Current as of Oct 11, 2026 (site code through PR #97).

## The two numbers

The site shows two ratings for every player. Everything below feeds one or both.

- **Week** (the Claude Rating): projected fantasy points for the rest of this Monday to Sunday matchup (both weeks of a two-week playoff round).
  - Skater: points per game × the sum, over his team's games left, of his chance to dress in each one (with ESPN's injury status as a cap).
  - Goalie: expected starts left × points per start.
- **Season**: projected points per team game for the rest of the season, ignoring this week's schedule and ESPN's injury caps.
  - Skater: points per game × share of his team's remaining games he's expected to play.
  - Goalie: his share of his team's starts × points per start.

The page also combines them into **keep value** (this week + 7 × Season), which ranks adds and drops.

```
SKATER                                                GOALIE
points per game ──┬── × Σ P(dress) over games left ─► Week     expected starts ── × points per start ─► Week
   (part 1)       │     (parts 2, 5)                             (parts 5, 6)        (part 7: tonight's line,
                  │           ▲                                       ▲               later games team strength)
                  │   ESPN injury cap (part 3)            ESPN injury, Daily Faceoff (parts 3, 6)
                  │
                  └── × rest-of-season share played ─► Season   season start share × points per start ─► Season
                            (part 4)                                  (part 8)
                                                    Week + 7 × Season = keep value (part 9)
```

## At a glance

| # | Part | Changes | Measured effect | Doesn't touch |
| --- | --- | --- | --- | --- |
| 1 | Skater points per game | Week and Season (skaters) | Ranks next week 0.38 (F) / 0.36 (D) vs 0.33 / 0.30 for season points per game; short-opening fix cut error 0.41 → 0.28 | Games, availability, injuries, opponents |
| 2 | Chance he dresses, game by game | Week (skaters) | The biggest single gain: ~+0.5 pts per pickup per week; lineup history took weekly ranking 0.637 → 0.652 | Season, points per game, goalies |
| 3 | ESPN injury status | Week (skaters and goalies) | Removes the lag on newly injured players; softer caps +0.007 weekly ranking (+0.009 free agents); minutes-based day-to-day caps cut error by a third | Season, points per game |
| 4 | Rest-of-season availability | Season (skaters) | Rest-of-season ranking 0.773 → 0.797 (waiver pool 0.671 → 0.701) | Week, goalies |
| 5 | Games left | Week (everyone) | A top pickup's first week is worth 6.1 vs ~4 in later weeks, mostly schedule | Season, points per game |
| 6 | Goalie expected starts | Week (goalies) | Weekly start error 1.78 → 0.56; wire goalie picks 3.0 → 4.9 pts a week | Points per start |
| 7 | Goalie points per start | Week and Season (goalies) | Betting line splits favorites (~4.1) from underdogs (~2.8); later-week pricing 0.273 → 0.297 | Starts |
| 8 | Goalie Season share | Season (goalies) | Rest-of-season error 0.545 → 0.537 pts a game with the 0.88 scale | Week |
| 9 | Keep value | Adds and drops on the page | +1.75 pts/roster/week over ranking by this week alone | The two ratings |
| 10 | Percentile badge | Display only | None by design | The rating and the sort |
| 11 | Advice cards | Display only | 2 Monday moves + swaps + streams: +5.6 pts a week; drop list doubles each move's gain | The rating |

How good is it overall? Ranking skaters on next week's points per game, the rating sits at about 92% of the best any model could do (the rest is game-to-game noise). On what actually gets scored, a whole week with scratches as zeros, its ranking correlation is 0.64-0.65. The remaining headroom is knowing who sits, not who scores more per game.

---

## 1. Skater points per game

**What it uses:** a per-stat model (ridge regression) with 104 inputs, all from the NHL stats API and MoneyPuck:
- ice time and power-play time over the last 5, 10 and 20 games;
- shots, shot attempts, hits, blocks, goals, assists, power-play points and fantasy points per game over the same windows, the season to date and last season;
- expected goals (his own and on-ice) this season and last;
- scoring per minute (this season plus half of last, pulled toward the position average) × last-5-game ice time, so a role change counts more for an efficient scorer;
- games played, whether he's a defenseman, and whether he has a last season;
- where he ranks in his own team's power-play time and ice time each game (top-5 PP time, top-3 forward or top-2 defenseman minutes), with position-by-usage terms (25 inputs, PR #86).

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

**What it doesn't affect:** whether he plays, how many games his team has, injuries. A day-to-day player's minutes in his last game change his chance of dressing (part 3), not his rate. Players back from injury come back at their old rate. It also ignores opponent strength, home ice, back-to-backs, PDO, team pace, age and PP1 labels (all tested, all under 0.02 points a week).

**Shown on the site:** tap a row for ice time, PP time, shots, blocks, last season's rate, and expected vs actual points per game ("running hot/cold" is a note only).

## 2. Chance he dresses (skaters)

**What it uses:** a model of his lineup history on his current team this season: how many games in a row he has missed, how often he sits, whether past absences looked like scratches (1-2 games) or injuries (3+), how many of the team's last 3 and 10 games he played, his ice time in his last game against his average, days since his last game, and whether he's new to the team. That gives a weekly chance, which is then set game by game and adjusted:

| Situation | What the site does | Measured |
| --- | --- | --- |
| Every player, game by game (PR #54) | Chance shifts by game number: down for the next game and up later for a player who is out, the reverse for a regular | Out players: 12% next game actual vs 18% shown before; regulars 94.5% vs 91% |
| Healthy scratch last game (PR #55) | +0.5 on the log-odds scale (about 25% → 34% a game), from the NHL's scratch list; no list readable means no ESPN status counts as a scratch | Actual 34.5%, both test seasons; worth 0.3-0.4 pts a week for fringe players |
| Just traded or claimed (PR #56) | +0.45 (about 78% → 85%) until his 4th game for the new team | Actual 86% |
| Traded, yet to play for the new team (PR #60) | Old team's games after his last one stop counting as misses; the trade boost applies from his first new game | 40-50% for the week before, about 88% after |
| Barely played his last game, no ESPN flag (PR #65) | Under 40% of his usual ice time: hard cut for the next game, smaller for the one after, still clear for the rest of the week; 40-55%: smaller cut for two games | Under 40%: 18% next game actual vs ~70% shown; Brier 0.43 → 0.16 |
| Just back in the lineup (PR #68) | Back 1-2 games from a 5+ game injury absence: +0.4; back 1-3 games from a 1-4 game scratch spell: −0.4 | 81% vs 73% shown; 77% vs 83% shown |
| ESPN status cleared in the last week, hasn't played since (PR #71) | No games this season yet: 75% next game, 80% later; has played: a clear boost (+0.9) | Cleared in season: 35% actual vs 18% shown |
| Sat last game, no ESPN status, in Daily Faceoff's lines (PR #72) | At least 90% next game, rest of week priced as if he had played | 14 of 14 dressed (early live read, rechecked end of October) |
| A regular returns from injury (PR #33) | The least-used healthy skater at that position on his team loses 8 points (F) or 12 (D) | Measured over 351 returns |
| Idle 15+ days while his team kept playing | Capped at 50% (15-21 days), 20% (22-35), 10% (longer) | |

Fallbacks: before his team has played, 90% if he played last season (50% if not). A player not on any NHL roster or stat sheet gets 10%, and so does an unsigned free agent with no NHL team or games this season (Kane, Tarasenko; PR #92, before which they rated as 90% likely to dress).

**What it changes:** expected games this week (the sum of the per-game chances), which scales the Week rating. Shown as `cr_dress` (the week's average) and `cr_dress_next` (next game, used by the Tonight card).

**How much it matters:** more than any modeling choice. Discounting for likely scratches added about +0.5 points per pickup per week, and cut picks who never played from 6-8% to 1-2%. The lineup-history version lifted the weekly ranking from 0.637 to 0.652 (waiver pool 0.573 to 0.594).

**What it doesn't affect:** points per game, the Season rating (part 4 has its own model, with matching trade, left-early and cleared rules), goalies.

## 3. ESPN injury status

**What it uses:** ESPN's status on each player at every update, the latest ESPN injury note, and for day-to-day and OUT skaters his ice time in his team's last game.

**Skater caps (a ceiling on each game's chance, not a second discount):**

| ESPN status | Cap |
| --- | --- |
| Day-to-day, missed the last game | 50% |
| Day-to-day, played the last game (PR #74) | 15% if under 55% of his usual ice time, 50% at 55-80%, 75% at 80%+ |
| OUT | 35% (15% if he played the last game but under 55% of his usual ice time, PR #77) |
| Injured reserve | 15% |
| Suspended | 0 |
| Note says surgery, LTIR, season-ending, week-to-week, month-to-month, indefinitely, no timetable, or a return 14+ days away | 0 |

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

**What it doesn't affect:** the Season rating (an injured star still rates as a star there; part 4 prices injured players), points per game. A detailed model by body part and stated timeline did worse than the simple caps, and caps rising by game number tested worse too.

## 4. Rest-of-season availability (Season rating, skaters)

**What it uses:** everything part 2 uses, plus last season's games played, ice time, games his team has left out of 84, and a lineup chain: how often he drops out after a game he plays, and how likely players are to come back after k games out (forwards: 31% after one game out, 17% after three, 7% after seven). Adjustments:
- **Hurt before playing this season (PR #36):** 45% of remaining games, 30% after a long injury note, 0 if ESPN says out for the season (66 such regulars dressed for 41%).
- **Suspended before playing (PR #44):** 45%. **Healthy regular (60+ games last season, on an NHL roster) yet to play:** 38%.
- **First 10 games for a new team (PR #59):** +0.3 on the log-odds scale (about 6 points of dress share; actual 71-72% vs 62-67% shown).
- **Barely played his last game (PR #66):** −0.4 until he plays a normal game (60% actual vs 69% shown under 40% of usual ice time).
- **ESPN status just cleared (PR #71):** 72% of his points a game if he hasn't played this season; a +0.4 boost if he has.

**What it changes:** the share of his team's remaining games he's expected to play, which multiplies points per game into the Season rating.

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

**How much it matters:** a top pickup scores 6.1 in his first week and about 4 in later weeks; most of that edge is schedule. On Oct 10 the stream card had offered goalies whose games had already started. The light-nights dots and **Open** column are shown next to the rating but don't change it: matching pickups to open nights didn't beat taking the best projected weeks.

**What it doesn't affect:** the Season rating, points per game.

## 6. Goalie expected starts

**What it uses:** a start model (logistic) for each remaining game: his share of the team's last 5, 10 and 20 starts and the season, last season's share, whether he started the previous game, back-to-backs, his run of straight starts, days since his last start, and team games and his starts in the last 7 days. Only goalies on the team's current NHL roster (plus a Daily Faceoff confirmed starter) share the starts (PR #61). Adjustments on top:
- **Early season (PR #14):** until his team's 10th game, shares are blended with last season's split. Last season's starts are divided by at least 70 (PR #92), so a no. 1 whose old partner left (Swayman, Sorokin, Vejmelka) isn't treated as a near-100% starter.
- **Last game's result (PRs #46-#48, #63):** after a bad start (5+ against, or pulled after 3+) his next-game chance drops and part of the drop carries to the game after; after a win it rises (a bit more after a win allowing 0-1), after a loss it falls.
- **Left early with no decision (PRs #63-#64):** about 12% for the next game, and the cut lasts the rest of the matchup (easing after the second game), until he starts or is confirmed.
- **Came on in relief last game (PR #69):** higher chance next game and a smaller boost later, more for a goalie with 0-3 of the last 20 starts.
- **Long team break (PR #74):** the last starter gets a boost for the first game after 4+ days off, bigger after 7+.
- **Second night of a back-to-back, night 1 played (PR #84):** the night-1 starter's chance for night 2 goes down if he is the team's no. 1 (most starts in the team's last 20) and up if he isn't; through the team's 10th game a no. 1 gets an extra nudge toward resting (PR #88).
- **Five to seven straight starts (PR #85):** the starter's next-game chance is trimmed (about 9 points) and the backup's raised to match; runs of 8+ are left alone.
- **Traded or claimed (PR #57):** +1.0 on the log-odds scale until his 4th start for the new team.
- **Games 2+ ahead (PR #48):** pulled a little toward an even split, since the chained week ran overconfident.
- **Injured goalie (PR #17):** his chance shrinks (part 3) before each game is split, so his partner gets his starts.
- **Daily Faceoff:** a goalie listed as **Confirmed** for today or tomorrow gets that game as a certain start and his partner zero, overriding every shift above. Names spelled differently (Matt / Matthew Murray) still match (PR #70). "Likely" and unconfirmed are shown on the row but don't change the number. A confirmed starter the site can't match to NHL data (a call-up) still sits the team's other goalies that game (PR #92).

**What it changes:** expected starts (`exp_starts`), which multiplies points per start into the Week rating. It also gives tonight's start chance used by the stream card.

**How much it matters:** goalies are almost entirely about starts.
- Weekly start error: 1.78 if you assume every team game, 0.58 with the start model, 0.56 with rest and workload. Ranking wire goalies: 4.9 points a week by expected starts vs 3.0 by points per game × team games (random 2.9).
- Early-season blend: error in a team's first 4 games 0.84 → 0.67 starts per goalie. Injured starter: backup 0.98 → 2.38 projected starts per 4 games (actual 2.24).
- After a bad start the goalie started 31% of next games vs 44% shown (about 0.4 points per goalie). No decision: 12% actual vs ~45% shown, about 20 times a season; over a four-game week about one start moves to the partner. Relief: 58-75% next game vs 45-65% shown. After a 7+ day break the last starter started 63% vs 47%.
- Later-game softening cut no. 1 overstatement from 0.21 to 0.08 starts per four games. Traded goalies: 32% next start actual vs 20% shown.
- Back-to-backs: a no. 1 who played night 1 started night 2 11% of the time against 18% shown, a backup 8.5% against 4% (error down 2%, 5 of 5 seasons); in a team's first 10 games 3% against 7% shown. Five to seven straight starts: 56% next start actual against 70% shown (error down 2.6%, 5 of 5 seasons).
- Early-season prior divided by at least 70: error halved in a 2022-26 backtest (0.186 to 0.092, 4 of 4 seasons).
- Confirmed starters: worth about a third more points per streamed goalie (+0.76 per day); Confirmed was right 54 of 56 times in the first live days.

**What it doesn't affect:** points per start, skaters.

## 7. Goalie points per start

**What it uses:** his own fantasy points per start this season, blended over 15 starts with his last-season rate (itself pulled toward the league average of 2.9 over 15 starts; 2.9 if he has none, PR #32). Then:
- **Tonight's game:** the DraftKings moneyline via the NHL's site, about 2.9 + 4.1 × (win chance − 50%), so roughly 2.3 for a 35% underdog and 3.5 for a 65% favorite (PR #10).
- **Later games this week:** his own rate + 3.4 × (win chance − 0.5), with the win chance from each team's goal differential (shrunk to half of last season's over 15 games) and home ice (PR #35). Tap a goalie to see "soft schedule" or "tough schedule".

**What it changes:** the points-per-start multiplier in the Week rating, and in the Season rating (own rate only, no line or schedule).

**How much it matters:** points per start is mostly unpredictable; season win %, goals against and shots against don't predict it (all under 0.1). The betting line does: starts with a 55-65% win chance averaged 4.2 points, under 35% 2.8. The schedule pricing lifted the weekly goalie ranking 0.273 → 0.297, better in all five seasons. The last-season starting point added +0.022 rank correlation for the Season rating, mostly in a team's first 20 games. His own shrunk rate adds +0.14 points a week on the best streaming picks.

**What it doesn't affect:** starts.

## 8. Goalie Season rating

**What it uses:** his share of his team's starts: three quarters from this season (blended with last season's split over 10 games) and a quarter from recent form, each game counting half as much every 5 team games back (PR #37). Then:
- × **0.88** for every goalie (PR #76, was 0.93 in PR #62): starts that go to goalies not on the roster yet, and shares spreading over a whole season.
- A **traded or claimed** goalie's share counts only the team's games since his first start for it (PR #58).
- A goalie who **left his last start with no decision** gets × 0.68 until he starts again or is confirmed (PR #67). The share he loses goes to his healthy partners (PR #92), and a goalie ESPN already lists as out isn't cut twice.
- A goalie **out for the season** (from ESPN's longer note, in sentences that name him) gets a Season of 0, and his share goes to the team's healthy goalies (PR #92: Shesterkin had topped the waiver goalies at 1.41).

Times his points per start (part 7, own rate only).

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
| **Move plan** (Upgrades header) | 7 moves a matchup (6 in the 6-day first one, 14 in a playoff round): 2 skater moves Monday, keep 5 for injury swaps and goalie streams; carry 2 goalies plus a stream slot | +5.6 pts a week over all 7 on Monday; 2 goalies plus a stream slot beats a third regular goalie by about 2 more |
| **Activate from IR** | A healthy player sitting in your IR slot, whose points don't count | Celebrini's 2.9 points were lost this way on Oct 10 |
| **Two-week playoff round** | One pool of moves for both weeks; about 2 skater moves each Monday | A week with no moves ran about 20 pts behind |
| **Waiver claims** (PR #94) | A player on waivers gets "put in a claim": a losing claim uses no move. Shows your place in ESPN's waiver order; a drop with Season 1.5+ is tagged as one others will claim | Every losing claim in the league's logs was cancelled without using a move |
| **Mid-week swaps** | Your skaters who are out, suspended, or sat the last game (unless back in Daily Faceoff's lines), next to the best free agent at their position; IR-eligible noted when an IR slot is open | +1.1 to +1.4 pts/roster/week from holding moves for this |
| **Stream a goalie tonight** | When fewer than two of your goalies start tonight: free-agent goalies confirmed to start, ranked by tonight's line, with moves left. Before first puck it says the time to add by; after it, that a goalie added now plays from tomorrow. Waiver goalies and started games are left out | Each of the first 2-3 streams adds 2-4 pts; which confirmed goalie matters little (~0.4) |
| **Tonight** | On a crowded night, start by per-game rating × next-game dress chance, bench the rest; skaters not in Daily Faceoff's lineup or who sat last game are flagged. Started players in the lineup fill their slots first (PR #87), but not those on ESPN's bench (PR #96); F and D counted separately against 9 F and 5 D plus the UTL | +0.9 pts a week, mostly from benching flagged players; before PR #96, 21 of 84 Saturday checks benched players still to play |
| **Best 2 moves** | The biggest keep-value gains over your weakest player at the same position, never more than your moves left. Skips anyone Mid-week swaps already drops and counts the swaps' adds against moves left (PR #96). On the matchup's last night before its first game, lists every move left and says unused moves expire tonight (PR #95) | +0.9 pts a week over the best free agent overall; Sunday's extra moves were worth 2.8-9.1 keep points per team |
| **Forwards / Defense / Goalies** | Free agents whose keep value beats your weakest at that position by 0.5+ | The Monday plan beats daily streaming by ~2 pts a week |
| **Drop list** | Your five lowest keep values, each with the best free agent at his position and the gain | Doubles each move's gain: +3.6 vs +1.7 dropping by season points |
| **IR stash** | With an open IR slot: up to 3 OUT or IR free agents at 1.5+ pts a game | 15-18 pts rest of season at 1.5-1.8, about 33 at 1.8+ |
| **Trade ideas** | Up to 5 offers where ESPN projects both players within 0.05 a game but Season differs by 0.3+, across positions if both your roster and the partner's stay at 11-13 F and 6-8 D (partner side, PR #87) | About 1 pt a week per trade, better player 70% of the time |
| **Goalies** | With 3 healthy goalies: the third spot is your stream slot, drop the lowest keep value for tonight's confirmed starter; with 4+, use the extra spot on a skater. After the day's first game, says a stream counts from tomorrow (PR #87) | Literal "spot on a skater" advice cost about a fifth of a matchup win every 8 weeks |
| **Trade check** (own panel) | Sums Season ratings for any trade; shows about 0.87 × gap × 3.5 pts a week and how often a gap that size picked the right side (53% to 87%). Never drops a goalie to make room unless goalies are in the trade (PR #87) | Calibrated on 2023-26 skaters; goalies rougher |

**Labels and phones (PRs #93, #96):** gains say what they measure ("+7.4 keep", or "+1.1 Season" with no moves left). On a phone the table fits the screen, each card's explanation folds behind "why" (except the Stream, Tonight, Forwards, Defense and Goalies cards, whose lines carry the deadline and who to bench or drop), and the waiver, injury and team tags wrap on their own line under the name.

**Ties in the table (PR #87):** players tied on the sorted column (every free agent's Week is 0.0 from Sunday's first game until Monday's update) are listed by keep value. Data from a matchup that already ended no longer says no moves are left.

**No moves left:** when your team has used all its moves, the add cards say "No moves left this matchup; these are next Monday's targets" and rank free agents by 7 × Season, waiver players who clear before Monday are dropped from the list, the stream card is hidden, and the Goalies card says to stream from next Monday.

## Built but switched off

A news hook (`data/news.json`) can adjust up to 25 players' games, starts or points per game (capped at ±20%). No news file is published, so it changes nothing today. Daily Faceoff covers the goalie case, which is where news was worth the most.

## Tested and left out

Each was checked over past seasons and moved the ranking by no more than a rounding error, or made decisions worse:
- **Skater points per game:** opponent strength and home ice, back-to-backs, 3-in-4s and travel, the opponent's starting goalie, team shooting pace, PDO and finishing luck corrections, age, two-seasons-ago stats, PP1 labels and PP-unit signals, separate forward and defense models, primary vs secondary assists, a boosted-tree blend (+0.002), leaving short games out of every window, a dip after returning from injury, teammate-based expected points.
- **Season rating experiments (Oct 11):** Marcel-style weighting, time-decayed game logs, role-based shrinkage, a direct boosted model, deployment trends, luck regression, ensembles and a rookie prior. None beat the ridge model; the same held for a boosted Season dress model and recalibrating the dress share.
- **Availability:** a roster-crunch input, coach-specific scratch habits, centre vs winger, prior injury history, injury caps rising by game number.
- **Goalies:** team goalie rotation habits, pts/start built from save % and team strength, Marcel goalie weighting, recent form, shutout and blowup tendencies, rust after rest, recalibrating start chances.
- **Moves and display:** keep value counting the real schedule for weeks 2-3 (shipped in PR #38, reverted in PR #39: about −0.9 pts a week in a season-long replay), choosier goalie streams, an extra 6:40 PM data run, a confidence badge, overtime rates, arena scorekeeper bias, next man up on the power play.
