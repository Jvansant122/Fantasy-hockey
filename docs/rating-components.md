# The Claude Rating, piece by piece

What each part of the rating does, what it uses, how much it was measured to matter, and what it leaves alone. Written for a fantasy manager; the research behind every number is in [the research write-up](research.md).

Current as of Oct 9, 2026 (site code through PR #21, plus the softer injury caps now being built).

## The two numbers

The site shows two ratings for every player. Everything below feeds one or both.

- **Week** (the Claude Rating): projected fantasy points for the rest of this Monday to Sunday matchup.
  - Skater: points per game × team games left this week × chance he dresses (with ESPN's injury status as a cap).
  - Goalie: expected starts left this week × points per start.
- **Season**: projected points per team game for the rest of the season, ignoring this week's schedule and ESPN's injury flags.
  - Skater: points per game × share of his team's remaining games he's expected to play.
  - Goalie: his share of his team's starts × points per start.

```
SKATER                                         GOALIE
points per game ──┬── × games left × P(dress) ─► Week     expected starts ── × points per start ─► Week
   (part 1)       │        (part 5)  (part 2)               (part 6)            (part 7, tonight: betting line)
                  │                     ▲                       ▲
                  │        ESPN injury cap (part 3)    ESPN injury + Daily Faceoff (parts 3, 6)
                  │
                  └── × rest-of-season share played ─► Season   season start share × points per start ─► Season
                            (part 4)                                       (part 8)
```

## At a glance

| # | Part | Changes | Measured effect | Doesn't touch |
| --- | --- | --- | --- | --- |
| 1 | Skater points per game | Week and Season (skaters) | Ranks next week 0.38 (F) / 0.36 (D) vs 0.33 / 0.30 for season points per game | Games, availability, injuries, opponents |
| 2 | Chance he dresses this week | Week (skaters) | The biggest single gain: ~+0.5 pts per pickup per week; the lineup-history upgrade took weekly ranking 0.637 → 0.652 | Season, points per game, goalies |
| 3 | ESPN injury status | Week (skaters and goalies) | Removes the lag on newly injured players; softer OUT/IR caps (queued) add +0.007 weekly ranking, +0.009 free agents | Season, points per game |
| 4 | Rest-of-season availability | Season (skaters) | Rest-of-season ranking 0.773 → 0.797 (waiver pool 0.671 → 0.701) | Week, goalies |
| 5 | Games left this week | Week (everyone) | A top pickup's first week is worth 6.1 vs ~4 in later weeks, mostly schedule | Season, points per game |
| 6 | Goalie expected starts | Week (goalies) | Weekly start error 1.78 → 0.56; wire goalie picks 3.0 → 4.9 pts a week | Points per start |
| 7 | Goalie points per start | Week and Season (goalies) | Own rate +0.14 pts/week on stream picks; betting line splits favorites (~4.1) from underdogs (~2.8) | Starts |
| 8 | Goalie Season share | Season (goalies) | Not yet tested on the rest-of-season horizon | Week |
| 9 | Percentile badge | Display only | None by design | The rating and the sort |
| 10 | Advice cards | Display only | Move split +5.8 pts/roster/week; drop list doubles each move's gain | The rating |

How good is it overall? Ranking skaters on next week's points per game, the rating sits at about 92% of the best any model could do (the rest is game-to-game noise). On what actually gets scored, a whole week with scratches as zeros, its ranking correlation is 0.64-0.65. The remaining headroom is knowing who sits the whole week, not who scores more per game.

---

## 1. Skater points per game

**What it uses:** a per-stat model (ridge regression) with 79 inputs, all from the NHL stats API and MoneyPuck:
- ice time and power-play time over the last 5, 10 and 20 games;
- shots, shot attempts, hits, blocks, goals, assists, power-play points and fantasy points per game over the same windows, the season to date and last season;
- expected goals (his own and on-ice) this season and last;
- scoring per minute (this season plus half of last, pulled toward the position average) × last-5-game ice time, so a role change counts more for an efficient scorer;
- games played, whether he's a defenseman, and whether he has a last season.

**What it changes:** `cr_fpg`, the points-per-game number that multiplies into both Week and Season. A player not matched to NHL data gets a replacement level of 1.04 (F) / 1.17 (D).

**How much it matters:**
- Ranking wire players on next week's points per game: 0.38 (F) / 0.36 (D), against 0.33 / 0.30 for season points per game and 0.29 / 0.27 for last 10 games. It wins in all three test seasons.
- Recent ice time is the strongest single input (0.34 for forwards on its own). A hot last 5 games is the weakest (0.23).
- Expected goals: worth including, but a custom xG model on top added only +0.003. The per-minute × ice-time inputs added +0.002 (more in the first six weeks of a season).
- Projections are calibrated: no bias up or down.

**What it doesn't affect:** whether he plays, how many games his team has, injuries. It also ignores opponent strength, home ice, back-to-backs, PDO, team pace, age and PP1 labels: all tested, all under 0.02 points a week.

**Shown on the site:** tap a row for ice time, PP time, shots, blocks, last season's rate, and expected vs actual points per game ("running hot/cold" is a note only; it doesn't change the number).

## 2. Chance he dresses this week (skaters)

**What it uses:** his lineup history on his current team this season: how many games in a row he has missed, how often he sits, whether past absences looked like scratches (1-2 games) or injuries (3+), how many of the team's last 3 and 10 games he played, his ice time in his last game and how it compares with his average, days since his last game, and whether he's new to the team.

Fallbacks: before his team has played, 90% if he played last season (50% if not). A player not on any NHL roster or stat sheet gets 10%. A player idle 14+ days while his team kept playing is capped at 10%.

**What it changes:** expected games this week = games left × P(dresses), so it scales the Week rating directly. Shown as `cr_dress`.

**How much it matters:** more than any modeling choice. Discounting for likely scratches added about +0.5 points per pickup per week, and cut picks who never played from 6-8% to 1-2%. The lineup-history version (Oct 9) lifted the weekly ranking from 0.637 to 0.652 (waiver pool 0.573 to 0.594). Example: a regular who sat the team's last game now projects to play about 42% of next week's games, not 61%.

**What it doesn't affect:** points per game, the Season rating (part 4 has its own model), goalies.

## 3. ESPN injury status

**What it uses:** ESPN's status on each player at every update and, for the change queued below, the latest ESPN injury note.

**What it changes today:**
- Skaters: OUT, injured reserve and suspended set P(dresses) to 0. Day-to-day caps it at 50%. It's a ceiling, not a second discount: a player already rated at 30% stays at 30%.
- Goalies: the same factor scales his chance of starting each game, and the starts he loses go to his healthy partner (part 6).
- Every override is logged daily to `data/injury_log.jsonl` so the caps can be rechecked against live data.

**Change being built into the site:** the injury research found the zeros too harsh. Injured skaters actually dressed for about 25% (OUT) and 11% (IR) of the week's games. The new caps for skaters are:

| ESPN status | Cap today | Cap after the change |
| --- | --- | --- |
| Day-to-day | 50% | 50% |
| OUT | 0 | 35% |
| Injured reserve | 0 | 15% |
| Suspended | 0 | 0 |
| Note says surgery, LTIR, out for the season, week-to-week, or a return 2+ weeks away | 0 | 0 |

Tapping an injured player will also show "usually back in about N games". Goalies keep today's rule.

**How long players are out** (41,905 ESPN injury notes, 2,868 injured player-weeks, 2024-25 and 2025-26; median further team games missed from a Monday):

| What's known | Usually misses |
| --- | --- |
| Day-to-day, hasn't missed a game yet | 0 more games (78% back within 4) |
| OUT | 3-5 |
| Injured reserve | 9-14 |
| Surgery or LTIR | about 16 |
| "Out 4-8 weeks" | about 15 (they rarely come back early) |
| By injury | illness or back ~0, upper body ~3.5, concussion 5 (some much longer), knee 12, shoulder 19 |

**How much it matters:** ESPN's flag removes the lag in P(dresses), which is built from games already played (on Oct 8, six OUT players were still rated 87-95% likely to dress). The softer caps lift the weekly ranking by +0.007 overall and +0.009 in the free-agent pool (an independent rebuild found +0.011 to +0.013), in both test seasons. The gain is in how your own injured players are projected, which feeds the drop list, Mid-week swaps and who to bench. It changes none of the top free-agent picks: a capped injured player never reaches the top of the wire.

**What it doesn't affect:** the Season rating, on purpose: an injured star still rates as a star there, and its lineup chain (part 4) already prices injured players (adding the injury notes there was flat). Points per game is unchanged: players come back at their old rate, and ice time in the first 5 games back is only about 0.15 minutes lower, too small to adjust for. A detailed model using body part and stated timelines did worse than the simple caps on unseen seasons.

## 4. Rest-of-season availability (Season rating, skaters)

**What it uses:** everything part 2 uses, plus last season's games played, ice time, games his team has left, and a lineup chain: how often he drops out after a game he plays, and how likely players are to come back after k games out (forwards: 31% after one game out, 17% after three, 7% after seven).

**What it changes:** the share of his team's remaining games he's expected to play, which multiplies points per game into the Season rating.

**How much it matters:** rest-of-season ranking 0.773 → 0.797 (waiver pool 0.671 → 0.701), and a bit more with injured players included. Knowing each player's true share of games played would reach 0.91, against 0.89 for knowing his true points per game, so availability is where the Season rating's error is. Players out two weeks or more now keep a realistic Season rating (they play about 27% of remaining games, not the 7% the old cap implied).

**What it doesn't affect:** the Week rating, goalies, ESPN's injury caps.

## 5. Games left this week

**What it uses:** the NHL schedule for the Monday to Sunday matchup, counting only games not yet played.

**What it changes:** the Week rating for everyone (skaters via games × P(dresses), goalies via the games their starts are spread over). It's why a 4-game week outranks a better player with 2 games.

**How much it matters:** a top pickup scores 6.1 in his first week and about 4 in later weeks; most of that first-week edge is schedule. The light-nights dots and the **Open** column (games on nights your lineup has an empty slot) are shown next to the rating but don't change it: matching pickups to open nights didn't beat simply taking the best projected weeks.

**What it doesn't affect:** the Season rating, points per game.

## 6. Goalie expected starts

**What it uses:** a start model (logistic) for each remaining game this week: his share of the team's last 5, 10 and 20 starts and the season, last season's share, whether he started the previous game, back-to-backs, his run of straight starts, days since his last start, and team games and his starts in the last 7 days. Three adjustments on top:
- **Early season:** until his team's 10th game, shares are blended with last season's split, so one opening-night start isn't read as a 100/0 split.
- **Injured goalie:** ESPN's injury factor scales his chance before each game is split among the team's goalies, so his partner gets his starts.
- **Daily Faceoff:** a goalie listed as **Confirmed** for today or tomorrow gets that game as a certain start, his partner zero. "Likely" and unconfirmed are shown on the row but don't change the number yet.

**What it changes:** expected starts (`exp_starts`, shown as games), which multiplies points per start into the Week rating. It also gives tonight's start chance used by the goalie stream card.

**How much it matters:** goalies are almost entirely about starts.
- Weekly start error: 1.78 if you assume every team game, 0.58 with the start model, 0.56 with rest and workload.
- Ranking wire goalies: 4.9 points a week by expected starts vs 3.0 by points per game × team games (random is 2.9).
- Early-season blend: error in a team's first 4 games 0.84 → 0.67 starts per goalie.
- Injured starter: his backup goes from 0.98 to 2.38 projected starts per 4 games (actual 2.24).
- Confirmed starters: worth about a third more points per streamed goalie (+0.76 per day).

**What it doesn't affect:** points per start, skaters.

## 7. Goalie points per start

**What it uses:** his own fantasy points per start this season, blended with the league average of 2.89 (his own number gets half the weight after 15 starts). For tonight's game only, the DraftKings moneyline via the NHL's site: about 2.9 + 4.1 × (win chance − 50%), so roughly 2.3 for a 35% underdog and 3.5 for a 65% favorite.

**What it changes:** the points-per-start multiplier in the Week rating (tonight's start uses the line, later games use his own rate) and the Season rating (own rate only).

**How much it matters:** points per start is mostly unpredictable. Season stats like win % and goals against don't predict it (all under 0.1). His own shrunk rate adds +0.14 points a week on the best streaming picks. The betting line is the one thing that does separate goalies: starts with a 55-65% win chance averaged 4.2 points, under 35% averaged 2.8.

**What it doesn't affect:** starts. Opponent strength and home ice beyond what the line already prices are left out (noise for goalies).

## 8. Goalie Season rating

**What it uses:** his share of his team's starts this season, blended with last season's split over 10 games, × his own points per start (part 7, without the betting line).

**What it changes:** the goalie Season rating only.

**How much it matters:** not yet tested on a rest-of-season horizon. It's the least-researched piece of the rating.

**What it doesn't affect:** the Week rating, injuries (no injury discount in Season).

## 9. Percentile badge

**What it uses:** where the player's rating falls among all forwards, defensemen or goalies on the site (rostered and free agents), separately for Week and Season. 90+ is shaded green, 70+ light green.

**What it changes:** nothing. The site sorts by projected points, not percentile. The badge is a quick read of how good a number is for that position.

## 10. The advice layer

These cards use the rating; none of them change it. All are for the team picked at the top of the page.

| Card or advice | What it does | Measured effect |
| --- | --- | --- |
| **Move split:** 2 skater moves Monday, keep 4 | Advice text in the Upgrades card | +5.8 pts/roster/week over spending all 6 on skaters Monday (+4.1 with a 7-start goalie cap) |
| **Upgrades** | Free agents projected above your weakest healthy player, across all forwards | The simple Monday plan beats daily streaming by ~2 pts a week |
| **Drop list** | Your five lowest skaters by Week rating | Doubles each move's gain: +3.6 vs +1.7 dropping by season points |
| **Mid-week swaps** | Your skaters who are out or sat the team's last game, next to the best free agent at their position | +1.1 to +1.4 pts/roster/week from holding moves for this |
| **Stream a goalie tonight** | When fewer than two of your goalies start tonight, wire goalies confirmed to start, ranked by tonight's line | Each of the first 2-3 streams adds 2-4 pts; which confirmed goalie you pick matters little (~0.4 pts) |

**Pending:** ranking adds and drops by "keep value" (this week plus about two more weeks of per-game value), so the drop list stops cutting good players who have a light week. Measured at +1.75 pts/roster/week over 8-week stretches. Queued for the site, not built yet.

## Built but switched off

A news hook (`data/news.json`) can adjust up to 25 players' games, starts or points per game (capped at ±20%). No news file is published, so it changes nothing today. Daily Faceoff covers the goalie case, which is where news was worth the most.

## Tested and left out

Each was added to the model and checked over three seasons; none moved the ranking by more than a rounding error: opponent strength and home ice for skaters, back-to-backs for skaters, team shooting pace, PDO and finishing luck corrections, a confidence badge, age, two-seasons-ago stats, PP1 labels, separate forward and defense models, primary vs secondary assists, boosted-tree ensembles, team goalie rotation habits, relief appearances, overtime rates, arena scorekeeper bias, and next man up on the power play.
