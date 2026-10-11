# How the Claude Rating works, and the research behind it

The Free Agent Finder ranks players by the **Claude Rating**: how many fantasy points a player is projected to score over the rest of the current matchup, using this league's scoring. This page explains what went into it and what the research found, in plain language.

Last updated Oct 9, 2026 (findings through betting lines for goalies, availability, assists, upside and age). The research is ongoing; new findings are added here as they land. Every idea tested so far, with its result and whether it changed the site, is listed in the [ideas ledger](ideas-ledger.md).

**League scoring:** goals 2, assists 1, power-play points 0.5, shorthanded points 0.5, shots 0.1, hits 0.1, blocks 0.5. Goalies: win 2, loss -1, overtime loss +1, goal against -1, save 0.2, shutout 3.

## The short version

1. **Ice time is the best single predictor of next week**, and it should come from the last 5 to 10 games, because role changes show up there first. Scoring stats should use the whole season plus last season. A hot last 5 games is the weakest signal.
2. **Whether a player dresses matters more than anything else.** Scratches, injuries and trips to the AHL mean zero points. Accounting for that is worth more than any modeling trick.
3. **Goalies are all about starts.** Ranking goalies by points per game × team games is no better than picking at random. Ranking by expected starts is about 60% better.
4. **Blocks matter for defensemen only. Hits predict nothing useful for forwards.**
5. **Use all 7 moves each week: 2 skater pickups on Monday, and keep the other 5 for injury swaps and streaming goalies with a confirmed start** on nights your goalies leave a slot open (sections 31-33, redone with the league's real settings in section 37). Carry 2 goalies, not 3. Re-pick for the schedule rather than holding a pickup.
6. **Drop the player with the lowest projected week**, not the lowest season total. That doubles what each move gains.
7. **Empty lineup slots on light nights are the biggest pool of points left**, and the simple Monday plan captures them: add the 6 best projected weeks and drop your 6 lowest. Chasing single nights one at a time loses points.

## How the rating is calculated

For a part-by-part breakdown (what each piece uses, what it changes, how much it was measured to matter and what it leaves alone), see [The Claude Rating, piece by piece](rating-components.md).

- **Skaters:** projected points per game × games left this matchup × the chance he dresses.
  - *Points per game* comes from a model that weighs ice time and power-play time over the last 5, 10 and 20 games, plus shots, blocks, hits, points and expected goals from this season and last season.
  - *The chance he dresses* comes from how many of his team's last 3 and 10 games he played, then ESPN's injury status on top (out, IR or suspended counts as 0; day-to-day is capped at 50%). These overrides are logged daily so the day-to-day number can be checked after a few weeks.
- **Goalies:** expected starts in the team's remaining games × his points per start. Expected starts come from his share of recent starts and back-to-backs. Points per start is his own average this season blended with the league average of about 2.9, so his own number gets half the weight after 15 starts. When Daily Faceoff lists a goalie as the **confirmed** starter for tonight or tomorrow, that game counts as a certain start for him and zero for his partner (section 23).
- The badge next to the rating is the player's percentile among forwards, defensemen or goalies across the league.

## How it was tested

- Data: NHL game logs and MoneyPuck expected goals for five regular seasons, 2021-22 to 2025-26 (236,091 skater games, 13,932 goalie games), all scored with this league's settings.
- Each Monday, the model only sees games before that Monday and predicts the Monday to Sunday week that follows. A player who doesn't dress scores 0.
- "Wire players" means skaters outside the top 190 (about what 12 teams roster) who have played in the last 14 days, and goalies outside the top 30.
- Each test season (2023-24, 2024-25, 2025-26) is predicted by a model trained only on earlier seasons, which gives 72 test weeks.
- A second, independent review rebuilt the data from scratch to check the claims. Where it disagreed, the research was rerun (see the end of this page).

## 1. Which stats are stable

![Stat stability](img/stability.png)

How much a stat in one set of 10 games tells you about another set of 10 games (1 = perfectly repeatable):

| Stat | Forwards | Defensemen |
| --- | --- | --- |
| Ice time | 0.97 | 0.96 |
| Power-play time | 0.93 | 0.95 |
| Hits | 0.84 | 0.80 |
| Shots on goal | 0.76 | 0.69 |
| Expected goals | 0.64 | 0.64 |
| Fantasy points | 0.62 | 0.64 |
| Blocks | 0.44 | 0.58 |
| Goals | 0.37 | 0.28 |

Ice time is almost perfectly repeatable. Goals are mostly noise over 10 games.

## 2. What predicts next week

![What predicts next week](img/predictiveness.png)

How well each stat ranks wire players by next week's points per game (higher is better):

- **Forwards:** ice time over the last 5 games 0.34, season points per game 0.32, on-ice expected goals 0.32, shots 0.27, power-play time 0.25, blocks 0.11, hits -0.01.
- **Defensemen:** ice time over the last 10 games 0.32, season points per game 0.30, blocks 0.23, shots 0.19, power-play time 0.16, hits 0.07.

![Short vs long windows](img/windows.png)

Fantasy points over the last 5 games score only 0.23, rising to 0.32 for the whole season. Hot streaks fade.

## 3. Choosing the model

![Model comparison](img/model_compare.png)

| Model | Forwards | Defensemen |
| --- | --- | --- |
| Last 10 games points per game | 0.29 | 0.27 |
| Season points per game | 0.33 | 0.30 |
| Season points per game, pulled toward last season | 0.34 | 0.32 |
| **Per-stat model with recent ice time (used on the site)** | **0.38** | **0.36** |
| Gradient boosting (more complex) | 0.38 | 0.36 |

The per-stat model wins in all three test seasons. The more complex model adds nothing, so the simpler one is used, which also lets the site explain each number.

![Calibration](img/calibration.png)

Its projections line up with what players actually scored, without bias up or down.

## 4. Will he dress?

Chance a wire player dresses for a team game, by how many of the team's last 3 games he played:

| Played in last 3 | Chance he dresses | Weeks with zero games |
| --- | --- | --- |
| 0 of 3 | 22% | 65% |
| 1 of 3 | 48% | 35% |
| 2 of 3 | 63% | 21% |
| 3 of 3 | 93% | 2% |

About 11% of wire player-weeks had zero games. The independent review found 24 to 26% when players who hadn't played recently were included, and showed that filtering for regulars raised the top 5 pickups from 5.0 to 6.5 points a week.

## 5. What it's worth when picking up players

![Pickup simulation](img/pickup_sim.png)

Points scored the following week by each method's top 5 wire picks:

| Method | Forwards | Defensemen | Picks who never played |
| --- | --- | --- | --- |
| Random wire player | 2.9 | 3.0 | |
| Points per game × team games (the old site) | 5.2 | 5.3 | 6% / 8% |
| **Claude Rating** | **6.2** | **6.1** | 1% / 2% |
| Perfect hindsight | 10.9 | 9.2 | |

That's about 1 extra point per forward pickup per week over the old ranking.

## 6. Goalies

![Goalies](img/goalies.png)

- **Starts:** the start model misses by 0.58 starts a week, versus 1.78 for assuming every team game. Starting the first night of a back-to-back nearly rules out the second night.
- **Points per start can't be predicted:** nothing tested ranks next week's points per start better than chance (all under 0.1). Team win % is the best at 0.09. So the site starts from the league average, about 2.9 points per start, and only slowly moves toward a goalie's own number (section 22).
- **Ranking wire goalies:** points per game × team games gives 3.0 points a week (random is 2.9). Expected starts gives 4.9.

### Rest and workload

![Goalie rest](img/goalie_rest.png)

- A long run of starts doesn't get a starter rested: he starts the next game 50 to 63% of the time whether he has started 1 or 8 in a row.
- The back-to-back is what matters. On the second night, after starting the first, he starts only 9 to 20% of the time.
- Days since his last start: 1 day 11%, 2 days 57%, 3 to 5 days 71 to 76%, 7+ days under 50% (usually hurt or benched).
- Adding these to the start model cut its weekly error by 5%.

## 7. Would checking the news help?

![News value](img/news_value.png)

This measures the most news could ever add, by giving the model perfect knowledge of what news would report:

| What the news would tell us | Extra points |
| --- | --- |
| Forwards: exactly who dresses | +0.28 per weekly pickup |
| Forwards: next week's ice time and power-play role | +0.58 per weekly pickup |
| Defensemen: exactly who dresses | +0.17 per weekly pickup |
| Defensemen: next week's ice time and power-play role | +0.35 per weekly pickup |
| **Goalies: confirmed starters, streaming each game day** | **+0.76 per day** |

For skaters, even perfect news adds only 3 to 10%. For goalies it adds about a third, because the start model's top wire goalie actually started only 71% of the time. Starters are usually confirmed after the morning skate, so a goalie news check needs to run around midday ET on game days. The site now gets confirmed starters for free from Daily Faceoff (section 23).

## 8. Opponents, home ice and back-to-backs

| | Per extra goal the opponent allows per game | Home | Second night of a back-to-back |
| --- | --- | --- | --- |
| Forwards (avg 1.36 pts/game) | +0.07 | +0.06 | -0.05 |
| Defensemen (avg 1.43) | +0.05 | +0.02 | -0.01 |
| Goalies, per start (avg 3.17) | -0.25 per extra goal the opponent scores | +0.21 | +0.05 |

Opponent strength changes skater pickups by less than 0.02 points a week, so it's left out. For goalies, home ice is worth about 0.2 points a start, and a weak opponent is a reasonable tie-breaker.

## 9. Using all 6 moves

![Pickup value](img/pickup_value.png)

Compared with dropping a typical bottom-of-roster skater (about 4.5 points a week):

| Pickup that week | Net gain |
| --- | --- |
| Best available | about +3.0 |
| 2nd and 3rd best | about +1.5 |
| 4th to 6th best | about +1.0 |
| 7th to 12th best | +0.3 to +1.7, noisy |

- **Use all 6 moves.** Even the 6th pickup beats the drop by about a point a week. (Sections 31-33 found it's better still to hold 4 of them for goalie streams and injuries.)
- **Re-pick each week.** A top pick scores 6.1 in his first week, then 4.4, 4.1 and 3.8. He's the same player per game; the first-week edge was his schedule, and some picks lose their spot over time.

## 10. Do ice-time jumps stick?

![Ice-time jumps](img/toi_jumps.png)

- **About half of an ice-time change lasts.** Jumps of 3+ minutes last best (66 to 68% still there the next week).
- **Power-play time is different.** A power-play time *increase* keeps only 17 to 28%, because short-term PP time mostly reflects how many penalties opponents took. A *decrease* keeps 58 to 66%. So "moved up to PP1" needs a confirmed unit change; "moved off PP1" can be trusted.
- The rating's model already accounts for this, so its error doesn't grow for players with big recent jumps.

## 11. Rookies and call-ups

![Rookies](img/rookies.png)

For players with fewer than 10 NHL games, their first few NHL games already predict better than their AHL scoring, draft slot or age. AHL points per game don't separate call-ups at all. What matters is the role he's given: his NHL ice time and power-play time. **No change to the rating**: a player with no NHL games gets the league average, and his minutes take over after one game.

## 12. Power-play unit and mid-season retraining

- **"On PP1"** looks important, but it adds nothing once the model knows his power-play minutes. It's a useful label, not an input.
- **Retraining during the season** doesn't help: a model trained on past seasons is as good in March as one retrained monthly. Accuracy rises through the season only because the season-to-date stats get longer. The model is trained once a year.

## 13. ESPN injury status

On Oct 8, six players ESPN listed as OUT were still rated 87 to 95% likely to dress, because they had played the games before getting hurt. ESPN's flag removes that lag. Among players who had dressed in all of their team's last 3 games, about 1% then scored a zero week, worth about 0.9 points per roster each week. The site now sets OUT, IR and suspended players to 0 and caps day-to-day at 50%.

## 14. Who to drop

![Drop rules](img/drop_rules.png)

Four ways of picking the 12 rostered skaters to drop each week, against the 12 best free agent pickups (6.1 points that week):

| Drop the player with the lowest… | His points that week | Net gain from the move | Drops that outscore the pickup |
| --- | --- | --- | --- |
| **Projected points this matchup (Claude Rating)** | **2.6** | **+3.6** | 11% |
| Season points total | 4.4 | +1.7 | 24% |
| ESPN-style rank | 4.5 | +1.6 | 26% |
| Last 10 games points per game | 4.6 | +1.6 | 27% |

Dropping by projected points doubles what each move gains, partly because that player has fewer games or is less likely to dress that week. It doesn't cost you a better player later: the dropped players score about the same over the next month under every rule. The site's **Drop list** shows your team's five lowest projected skaters.

## 15. Light nights and open slots

![Light nights](img/light_nights.png)

Simulating 12 rosters in this league's format:

| NHL games that night | Open lineup slots per roster | Points available from open slots |
| --- | --- | --- |
| 1-3 | 12.4 | 16.3 |
| 4-6 | 10.4 | 14.9 |
| 7-9 | 7.2 | 11.1 |
| 10-12 | 4.7 | 7.8 |
| 13+ | 1.9 | 3.2 |

- A typical roster has about 28 empty starting slots a week, worth about 72 points if every one were filled. About two thirds of that is on nights with 6 or fewer games. The 6-move limit means only part of it is reachable, but it's the largest pool of points in this study.
- A streamer scores about the same on any night, so what matters is simply whether he plays on a night your slot is open. The site's **Open** column counts exactly that for each free agent, for the team you pick.

## 16. The UTL slot

No skater in this league is eligible at both forward and defense, and the lineup uses general F slots, so forward eligibility never matters. UTL only comes into play on nights with 13+ games, and there the 6th defenseman outscored the 10th forward (1.40 vs 1.30 points). **The site counts UTL as a sixth D slot** when working out open slots.

## 17. Coming back from injury

Across 2,012 returns from absences of 2+ weeks, players scored at their old rate right away, even after 8+ weeks out. Ice time in the first 5 games back is only 0.1 to 0.35 minutes lower. **No change to the rating**: a returning player is rated on his pre-injury numbers.

## 18. Things that turned out not to matter

Each of these was added to the model and tested over three seasons. None changed its accuracy by more than a rounding error.

| Idea | Result |
| --- | --- |
| How much his team shoots and scores | Nothing. His own ice time, shots and expected goals already carry his team's pace. |
| Each team's goalie rotation habits | Teams differ, but the habit doesn't carry from one season to the next (it follows the goalies, not the coach). A per-team model was worse. |
| Hits for forwards | Worth exactly their 0.1 points each, no more and no less. Forwards who hit a lot score less only because they're fourth-liners. |
| Shot quality and finishing skill | Finishing is a real but small skill, and last season's goals and expected goals already capture it. |
| Stats from two seasons ago | Last season is enough. Two seasons back helps a tiny bit in October only. |

## 19. Moves: the Monday plan beats daily streaming

Simulating 12 rosters over 72 weeks, each with at most 6 moves a week:

| Strategy | Points per roster per week | vs. no moves |
| --- | --- | --- |
| No moves | 85.8 | |
| **Monday: add the 6 best projected weeks, drop the 6 lowest projected** | **91.2** | **+5.4** |
| Monday, matching pickups to your open-slot nights | 90.8 | +5.0 |
| Daily streaming: fill tonight's open slots until moves run out | 89.1 | +3.3 |

- **The simple Monday plan wins.** That's exactly the site's table (sorted by rating) plus the drop list.
- Matching pickups to open nights doesn't beat it, because the best projected week is usually a 4-game week anyway.
- Daily streaming spends moves on 1-point nights and loses about 2 points a week. Light nights are worth using, but through the weekly ranking, not one night at a time.

## 20. Streaming goalies by opponent

Over 101 weeks of picking the 3 best wire goalies:

| Ranked by | Points from top 3 picks per week |
| --- | --- |
| Expected starts | 5.19 |
| + home ice and opponent strength | 5.07 |
| **+ the goalie's own points per start, pulled toward the league average** | **5.33** |

Opponent strength is noise for goalies (it explains under 1% of points per start). A goalie's own points per start, blended with the league average so that one hot week doesn't swing it, adds a little. **The site now uses it** (section 22).

## 21. Should the rating show a confidence level?

No. Players with few games this season are not projected any worse: the model leans on last season for them, and they're mostly lower-usage players with less to swing. Weekly hockey scoring is noisy for everyone (the typical miss is about 0.55 points a game against an average of 1.3). A "low confidence" badge would tell you to distrust numbers that are as good as any other. Tap a row to see what drives a rating instead.

## 22. What changed on the site from this round

- **Goalies:** expected starts × his own points per start this season, blended with the league average of 2.9 (his own number gets half the weight after 15 starts). Worth about +0.14 points a week on the best streaming picks.
- Nothing else. Injury returns, team shooting, rotation habits, hits, shot quality, older seasons, opponent-based goalie streaming, night-by-night streaming and a confidence badge were all tested and left out.
- **Next to test:** comparing the rating with ESPN's projections over a few weeks of live data, using the log described in section 23.

## 23. Confirmed starting goalies, and what's being logged

The biggest gain left in the research was knowing tonight's starting goalie (section 7: about a third more points per streamed goalie). Daily Faceoff publishes it for every game, so the site now reads it directly, with no AI step needed.

- The site updates at **9 AM, 12:30 PM and 5 PM ET**. Starters are usually confirmed after the morning skate, so the midday run is the one that matters.
- A goalie Daily Faceoff marks **Confirmed** for tonight or tomorrow gets that game as a certain start, and his partner gets zero. A confirmed start on the first night of a back-to-back also lowers his chance of starting the second night. The row shows a "starts tonight" or "sits tonight" tag.
- "Likely" and unconfirmed goalies are shown when you tap the row but don't change the rating yet, until the log shows how often they're right.

Every update also saves a log (in `data/log`) of things that might help but need live data to test:

| Logged | Question it answers after three weeks |
| --- | --- |
| Daily Faceoff starters, every label, three times a day | How often "Confirmed" and "Likely" are right, and when they firm up |
| Daily Faceoff projected lineups, power-play units and scratches | Does "not in the projected lineup" predict a scratch better than the current dress estimate? |
| ESPN injury notes (expected return timing) | Do the notes predict games missed better than the flat 50% for day-to-day? |
| DraftKings moneylines and totals (from the NHL's site) | Do betting odds predict a goalie's points per start? |
| Each player's rating and ESPN's projection | Which one picks better free agents (section 25 of the research)? |

## 24. Betting lines for goalies

A goalie's points depend heavily on whether his team wins (a win and a loss are 3 points apart), and the betting line is the sharpest estimate of that. Across 3,308 starts with closing lines from 2021-22 and 2022-23:

| Chance to win, from the betting line | Starts | Points per start |
| --- | --- | --- |
| under 35% | 475 | 2.8 |
| 35-45% | 757 | 2.9 |
| 45-55% | 844 | 3.7 |
| 55-65% | 757 | 4.2 |
| over 65% | 475 | 4.0 |

- **The line is the first thing found that predicts points per start.** Season win %, goals against and shots against didn't (sections 6 and 20), because they're stale averages. The line already knows tonight's starters, rest and injuries.
- **The site now uses it for tonight's game:** about 2.9 + 4.1 × (win chance − 50%) points per start, so roughly 2.3 for a 35% underdog and 3.5 for a 65% favorite. The lines come from DraftKings through the NHL's site and only exist for today, so the rest of the week still uses the goalie's own points per start. Tap a goalie's row to see tonight's line.
- For skaters the line barely matters: players on teams expected to score 3.5+ goals score about 0.1 points more than usual. That's a tiebreak at most, so it isn't in the rating.

## 25. More ideas that didn't change anything

| Idea | Result |
| --- | --- |
| A fancier "will he dress" model (days since his last game, ice-time rank) | Slightly better, +0.02 points per pickup. The simple version stays. |
| Short-handed ice time | Shorthanded points are rare and unpredictable. No gain. |
| Separate models for forwards and defensemen | A hair better, within noise. |
| Primary vs. secondary assists | Primary assists are more repeatable, but the model already captures it. |
| An "upside" option for weeks you need a big score | High-ceiling players are just the high-projection players. When behind, pick the same players. |
| Two-week matchups | Same per-game projection × two weeks of games. |
| Age | Young players do improve and older ones decline, but recent ice time already shows it. |
| PDO and finishing luck | A player whose team shoots and saves unusually well with him on the ice (PDO), or who scores well above his expected goals, is mostly running lucky. The rating already expects him to cool off because it projects goals from shots and chances (see section 26). |

## 26. Expected vs. actual points

A second research thread built an expected-points number. It's what a player's season would be worth if his goals and assists matched the quality of chances he and his linemates created, keeping his shots, hits and blocks as they are.

- **Expected beats actual as a predictor.** Season expected points per game predicts the next month better than season points per game, in all three test seasons, especially for defensemen.
- **Most of a gap is luck.** About three quarters of the difference between a player's actual and expected points fades within 4 weeks. The luckiest tenth of wire players drop about 0.2 points a game, and the unluckiest rise about as much.
- **Not all of it, though.** Elite finishers like Kucherov, Draisaitl and Nylander beat their chances every year.
- **The rating already does most of this**, since it projects goals from shots and chances. So expected points don't change the rating. When you tap a skater's row, it shows his season points per game next to his expected points. Once he has 15+ games and the gap is 0.15 or more, it also says "running hot" or "running cold".

## 27. Goalie starts in the first two weeks

The start model was built on games from later in the season. On opening week it read one start as a 100/0 split, so after opening night it projected the backup for about 0.8 fewer starts over the next 4 games than backups actually got.

The site now blends in last season's split between a team's goalies until the team's 10th game, fading it out as games are played. Over four seasons this cuts the error in projected starts during a team's first 4 games from 0.84 to 0.67 per goalie. From game 10 on, nothing changes. Daily Faceoff confirmations still override on game days.

## 28. Two ratings: Week and Season

The site shows two numbers for every player:

- **Week** (the Claude Rating): projected points for the rest of this matchup. It counts games left, the chance he dresses, injuries and tonight's goalie news. Use it for this week's pickups and drops.
- **Season**: projected points per team game for the rest of the season. It's the same per-game projection without this week's schedule, and without ESPN's injury discount, so an injured star still rates as a star. For skaters it's projected points per game × the share of his team's remaining games he's expected to play (section 34). For goalies it's his share of his team's starts (this season, blended with last season's split over 10 games) × his points per start. Use it for keepers, trades and "is this a real player or a one-week stream".

Why one per-game projection works for the whole season: the research found a model trained for the next week and one trained for the next two weeks agree almost perfectly (0.999), and both predict longer stretches better than shorter ones. Each rating has its own percentile badge within forwards, defensemen or goalies.

## 29. Model upgrade: who plays, ice time, and goalie rest (Oct 9)

Three measured changes went into the rating together. All numbers are rank correlations tested on past seasons, each season predicted only from earlier ones.

| Change | What it does | Before | After |
| --- | --- | --- | --- |
| **Who plays next week** | The chance a skater dresses now comes from his lineup history this season: how many games in a row he has missed, how often he sits, his ice time in his last game, and whether past absences looked like scratches (1-2 games) or injuries (3+). A regular who sat the team's last game plays about 42% of next week's games, not the 61% the old table gave him. | Weekly total 0.637 (waiver pool 0.573) | 0.652 (0.594) |
| **Scoring rate per minute × recent ice time** | The model now also sees each player's points per minute (this season plus half of last, pulled toward the position average) times his ice time over the last 5 games, so a role change counts more for an efficient scorer. | Per game 0.520 (waiver 0.389) | 0.522 (0.393); first 6 weeks +0.005 |
| **Goalie rest and workload** | Start chances now use his run of straight starts, days since his last start, and team games and his starts in the last 7 days, not just back-to-backs. | Weekly starts off by 0.59 | 0.56 |

ESPN's injury status still sits on top: out, IR and suspended players are 0, and day-to-day players are capped at 50%. The cap is a ceiling, not a second discount, so a player already marked down for sitting isn't marked down twice.

Two ideas were left out: a luck correction for the luckiest tenth of players tested flat or negative, and a blend with a boosted-tree model added +0.002 at the cost of a new library in the daily job.

## 30. When a starting goalie is hurt

When ESPN marks a goalie out, the site used to set his starts to 0 but leave his partner's projection alone, so the backup looked like about 1 start in 4 games. Now the injured goalie's chance of starting each game goes to the healthy goalies on his team, so they share every game. When a regular starter (6+ of the team's last 10 starts) is out, his backup goes from 0.98 to 2.38 projected starts per 4 games, close to the 2.24 starts such backups actually got. A day-to-day goalie keeps half his chance, and the other half goes to his partner. Daily Faceoff confirmations still decide individual games.

## 31. Keep 1-2 moves for mid-week injuries

The Monday plan used to spend all 6 moves at once. A simulation of 12 rosters over 67 weeks tried holding some back: whenever a rostered skater misses his team's most recent game, swap him for the best free agent at his position for the rest of the week, and spend any held moves left on Thursday the usual way.

| Strategy | Skater points per roster per week | vs. all 6 on Monday |
| --- | --- | --- |
| All 6 on Monday | 93.9 | |
| **5 on Monday, 1 held for an injury** | **95.0** | **+1.1** |
| **4 on Monday, 2 held** | **95.3** | **+1.4** |
| 5 on Monday, 1 spent Thursday with no injury swap | 94.2 | +0.3 |

The 6th Monday move is worth about half a point to a point; replacing a player who has stopped dressing is worth 3-4, and a typical roster needs that about once every three weeks. Almost all of the gain is the injury swap, not the timing. ESPN's injury flags should do even better than "missed his last game" (a perfect flag gets +1.7 / +2.0).

**On the site:** the Upgrades card now has a **Mid-week swaps** list for your team: any skater ESPN lists as out, on IR or suspended, or who sat his team's last game while it still plays this week, next to the best free agent at his position by Week rating. Out and IR players are marked IR-eligible, so you can use the IR slot instead of dropping them. A day-to-day star who sat one game is usually better benched than dropped. Goalies weren't part of this test.

## 32. Spend spare moves streaming goalies

Section 31 split the 6 moves between Monday skaters and injury swaps. A goalie with a confirmed start off the wire is worth about 3 points, and each stream costs one move. A simulation of 12 rosters over 67 weeks (each roster holding two good goalies, all 12 streaming against each other) tried every split:

| Skater moves Monday + goalie streams | Points per roster per week | vs. all 6 on skaters |
| --- | --- | --- |
| 6 + 0 | 106.7 | |
| 5 + 1 | 107.7 | +1.0 |
| 4 + 2 | 109.9 | +3.2 |
| **3 + 3** | **111.0** | **+4.3** |
| **2 + 4** | **111.3** | **+4.6** |
| 0 + 6 | 107.9 | +1.2 |

Skater pickups 3 to 6 add less than a point together, while each of the first two or three goalie streams adds 2 to 4. A roster rarely has more than 3 nights a week with an open goalie slot, so a 4th stream often goes unused; those moves still cover injury swaps. The first stream costs a bench skater to make room.

**On the site:** the advice now says 2-3 skater moves on Monday, the rest for goalie streams and injuries. A **Stream a goalie tonight** card appears for your team when fewer than two of your goalies are starting tonight (Daily Faceoff confirmed, or the start model when not yet confirmed). It lists free-agent goalies confirmed to start tonight, ranked by tonight's points per start (priced from the betting line), and your moves left when ESPN reports them. If your league caps goalie starts per week, streams are worth less once you near the cap; the card mentions the cap when ESPN's settings include one.

## 33. Two on Monday, keep four

Sections 31 and 32 tested injury swaps and goalie streams separately. Run together from one budget (injury swaps first each day, then streams), the best split moves to **2 skater moves on Monday and 4 held**:

| Skater moves Monday | Gain vs all 6 on skaters | With a 7-start goalie cap |
| --- | --- | --- |
| 4 | +3.1 | +2.4 |
| 3 | +4.9 | +3.7 |
| **2** | **+5.8** | **+4.1** |

Four spare moves leave room for an injury swap plus the 2-3 goalie streams a roster can use in a week. A cap on goalie starts cuts the gain by about a third but doesn't change the best split. The site's advice now says "2 on Monday, keep 4".

Which free-agent goalie to stream matters much less than streaming one at all: across 627 nights, the best way of picking among confirmed wire starters (weak opponent, or his own points per start) beat a random one by about 0.4 points, against about 3 points for the stream itself. The site keeps ranking them by tonight's betting line, which already combines team and opponent strength.

## 34. A better Season rating: who plays the rest of the season

The Season rating had reused next week's chance of dressing. Tested on its own horizon (every Monday of 2023-24 to 2025-26, points from that Monday to season's end per team game left), it ranked skaters at 0.773 (waiver pool 0.671). Knowing each player's real share of games played would lift that to 0.91, while knowing his real points per game would only reach 0.89, so availability is where the error was.

The new model predicts the share of the team's remaining games he plays. It uses the same lineup history as the weekly model, plus last season's games played, ice time, games left and a simple "in the lineup or out k games" chain: how often he drops out after a game he plays, and how likely players are to come back after k games out (31% after one game, 17% after three, 7% after seven for forwards).

| | All skaters | Waiver pool |
| --- | --- | --- |
| Old Season rating | 0.773 | 0.671 |
| **New Season rating** | **0.797** | **0.701** |

With injured players included the gain is bigger (+0.030 / +0.038). An independent rebuild found +0.021 / +0.024. Players out for a while now keep a realistic Season rating: those idle two weeks or more dress for about 27% of the remaining games, not the 7% the old cap implied. Points per game didn't change; three alternatives tied or lost. The Week rating and ESPN's injury caps are unchanged.

## 35. Pick adds and drops by more than this week

Every move simulation before this one scored a single week, so dropping a good player who only had a light week looked free. With rosters carrying over across 8-week stretches, ranking adds and drops by this week alone churns away good players and costs later weeks. Adding about two more weeks of per-game value (7 games) to this week's projection works best:

| Ranking adds and drops by | 2 Monday moves | 6 Monday moves |
| --- | --- | --- |
| This week only (the old site) | baseline | baseline |
| Per-game value only | +0.7 | |
| **This week + 7 games of per-game value** | **+1.75** | **+2.3** |

**On the site:** the upgrade cards and the drop list now rank skaters by this "keep value" (shown next to the Week number). The per-game part uses the Season rating, so an injured star with a long future isn't put on the drop list. The Week column itself is unchanged, and goalies are still ranked on this week, since streams are decided night by night.

## 36. Injured players: how long they're out

ESPN's injury statuses aren't archived anywhere, so the research rebuilt them from 41,905 ESPN/Rotowire news notes. From a Monday, the typical number of further games missed:

| What is known | Median further games missed | Back within 4 games |
| --- | --- | --- |
| Day-to-day, hasn't missed a game yet | 0 | 78% |
| Day-to-day, missed 1-8 | 2-4 | 45-67% |
| Out | 3-5 | 35-51% |
| Injured reserve | 9-14 | 15-26% |
| Surgery or long-term IR | 16 | 12% |
| "Out for the season" | rest of season | 0% |

So the site's old rule (out and IR at 0) was too harsh: those players actually dress for about 25% and 11% of the week's games. **The Week rating now caps an injured skater's chance of dressing at 35% when out, 15% on IR and 50% day-to-day**, and goes to 0 only when ESPN's note says surgery, long-term IR, season-ending, week-to-week, month-to-month, indefinitely, or a return 2+ weeks away. Weekly rank correlation +0.007 overall and +0.009 on the waiver pool, and an independent rebuild found +0.011 to +0.013. It doesn't change which free agent tops the list, but it projects your own injured players more fairly (the drop list, mid-week swaps, who to bench). Tap an injured player to see how many more games players with his status usually miss, or what ESPN's note says.

The Season rating already handled injured players well (section 34), and note-based tweaks didn't improve it. Returning players come back at their old ice time, so nothing changes there either. Goalies keep the old rule.

## 37. The league's real move limit, and how many goalies to carry

The league's ESPN settings say "1 acquisition per scoring period", and the transaction log shows what that means: one move per day of the matchup, pooled and usable any time. That is **7 moves in a normal week** (6 in the 6-day first matchup), not 6, and several can go on the same day (one team made 3 adds in 7 minutes). Teams also carry more than the earlier simulations assumed: about 19 skaters and 3.3 goalies each.

The move plan was re-run with these settings over 67 weeks (2023-24 to 2025-26), with one active manager against 11 teams that never move:

| Plan | Points a week vs all 7 moves Monday |
| --- | --- |
| 6 moves, all Monday | −1.3 |
| 5 Monday + injury swaps, no goalie streams | +0.3 |
| 3 Monday + injury swaps + goalie streams | +4.7 |
| **2 Monday + injury swaps + goalie streams** | **+5.6** |
| 0 Monday + injury swaps + goalie streams | +2.6 |
| **2 goalies + an extra skater, 2 Monday + swaps + streams** | **+7.6** |

So the plan holds: **2 skater moves on Monday, the other 5 for injury swaps and confirmed goalie streams.** And **carrying 2 goalies and using the third roster spot for goalie streams beats carrying a third regular goalie by about 2 points a week**, because the third goalie's starts are easier to get off the wire one confirmed night at a time. That third spot holds whoever you streamed last; it isn't a spare spot for a skater (section 55). With only one manager streaming these gains are on the high side; the earlier runs with all 12 teams streaming ranked the plans the same way.

**On the site:** the Upgrades header states 7 moves and "keep the other 5", the "Stream a goalie tonight" card shows moves left out of 7, and a Goalies card appears when you carry more than 2, naming your weakest goalie this week.

## 38. Goalies: start from last season's level

A goalie's points per start blend his starts this season with 15 starts' worth of a starting point. That starting point used to be the league average (2.9). Scoring the goalie Season rating over 73 Mondays (2023-24 to 2025-26) found that starting from **his own last-season points per start** (itself pulled toward 2.9 over 15 starts) ranks starters better: rank correlation +0.022 (95% +0.013 to +0.031), almost all of it in a team's first 20 games. Pulling harder toward the average (the "Marcel" weighting some projection systems use, about 60 starts) was clearly worse (−0.03). **The site now does this for both the Week and Season goalie ratings.** A goalie with no starts last season still starts from 2.9.

## 39. When a regular comes back, the fill-in sits

When an injured regular returns, someone loses his spot. Over 351 returns in 2023-24 to 2025-26, the healthy skater with the **least ice time at that position (forward or defense) in the team's last game** dressed noticeably less the next week than the dress model predicted: about 8 points of dress share less for forwards and 12 for defensemen. These are almost all waiver-wire players, exactly the fill-ins you might pick up. Fresh call-ups, by contrast, were priced about right already.

**On the site:** when a regular (who dressed 80%+ of games before he got hurt) has missed 3+ straight games and ESPN now lists him healthy or day-to-day, the least-used skater at his position on that team gets his chance of dressing lowered by 8 points (forward) or 12 (defense). Tap the player to see "… is back from injury, so he may sit."

## 40. Tonight's lineup and the best two moves

Two small decision helpers, each worth about a point a week in the backtests (findings 65 and 68):

- **Tonight.** On a night when more of your skaters play than your 9 F / 5 D / 1 UTL slots hold, start by the Claude Rating's points per game and bench the rest. The bigger part of the gain (+0.9 a week) is benching a skater who isn't in Daily Faceoff's lineup for his team, or who sat his team's last game, since he'll likely score nothing. The Upgrades section now shows a "Tonight" card on crowded nights listing who to bench, with those players flagged.
- **Best 2 moves.** For the Monday pickups, taking the two swaps with the biggest keep-value gain over your weakest player *at the same position* beat taking the best free agent overall by +0.9 points a week. The Upgrades section now leads with those two moves, and each drop-list entry shows the best free agent at his position and how much he'd add.

## 41. Goalie starts later in the week: opponent and home ice

Only tonight's goalie starts have a betting line, so the rest of the week's starts used to be worth the goalie's flat points per start. A simple win model, built from each team's goal differential per game (this season, pulled toward half of last season's over 15 games) plus home ice, agrees with the betting market at 0.87 and explains about two thirds of what the line explains about goalie points. Pricing each later start at points per start + 3.4 × (win chance − 0.5) improved the weekly goalie ranking from 0.273 to 0.297 rank correlation, and it was better in all five seasons tested (2021-22 to 2025-26).

**On the site:** the Week rating for goalies now prices every remaining start without a line this way. Tonight's lined starts still use the betting line, and the Season rating is unchanged. Tap a goalie to see "soft schedule" or "tough schedule" and how many points per start it adds or takes away.

## 42. Players hurt before the season started, IR stashes, and an 84-game season

- **Hurt before opening night.** A skater with no games yet this season used to get the same 10% rest-of-season chance of playing as someone who has disappeared from the lineup. But 66 regulars injured before opening night in 2024-26 dressed for 41% of their team's remaining games (45% after a short injury note, 31% after a long one). **The Season rating now uses 45%, 30% for a long injury note, and 0 when ESPN says he's out for the season.** Nugent-Hopkins, back in about a week, was showing a Season rating of 0.17. Keep value and the drop list now treat these players fairly. The Week rating is unchanged.
- **IR stash.** A free agent who is OUT or on injured reserve can go straight into an open IR slot. Over the rest of the season, a 1.5-1.8 pts/game stash beats your weakest skater by about 15-18 points, and a 1.8+ one by about 33, for the cost of one move (about 2 points). Below 1.2 pts/game it never pays. **When you have an open IR slot, an "IR stash" card lists up to 3 such players at 1.5+ pts/game.**
- **84 games.** The 2026-27 regular season is 84 games per team, so the Season rating now counts games left out of 84.

## 43. Goalie Season rating: a little recent form

A goalie's share of his team's remaining starts used to come from his whole season (pulled toward last season early on). Blending in a quarter of a recent-form share, with each game counting half as much every 5 team games back, catches a new no. 1 sooner: rest-of-season errors are 11% smaller right after a starter change, and rank correlation is +0.013 overall. Using recent form alone overreacts, so it stays at a quarter. **The goalie Season rating now uses this blend**; the Week rating's start model is unchanged.

## 44. Keep value counts the real schedule

Keep value (section 35) added this week's projection to about 7 games' worth of the Season rating, the same for everyone. Some teams play 8 games over the next two weeks and others 5, a gap of about 4 points for a typical skater, which is the size of a swap decision. Multiplying the Season rating by each team's actual games in the two matchups after this one ranked players better on those weeks (+0.020 rank correlation, +0.023 among free agents). Keep value briefly used the real number of games, so the upgrade cards, Best 2 moves and the drop list all account for upcoming schedules.

**Reverted.** A season-long hold/drop replay scored actual decisions instead of rankings: the schedule-aware keep value lost about 0.9 points a week against the flat 7 games (95% range: -2.2 to +0.3). The ranking gain on weeks 2-3 doesn't turn into better moves, because next Monday's weekly projection already counts those games when the time comes. **Keep value is back to a flat 7 games.**

## 45. Trade ideas: players ESPN rates the same but Claude doesn't

Other managers judge players by what ESPN shows them. When ESPN projects two skaters at the same position about the same (within 0.05 points a game) but Claude's Season rating has one at least 0.3 points per team game higher, offering the lower-rated one for the higher-rated one won about 1 point a week for the rest of the season in 2023-26 replays. That held in all three seasons and at every stage of the season, and the better-rated player came out ahead about 70% of the time. Trades use none of the 7 weekly moves. The test only counted healthy players with 40+ NHL games the season before, because ESPN may know more than the rating about rookies and new arrivals. **The Upgrades panel now has a Trade ideas card** listing up to 5 such offers (2 per player at most), biggest rating gap first. The other manager still has to accept.

## 46. Two-week playoff rounds

ESPN's league settings put the fantasy playoffs in two rounds of two weeks each, likely Mar 8-21 and Mar 22-Apr 4, 2027. The move limit is 1 per day pooled over the matchup, so a round gets 14 moves. The site used to assume every matchup was one Monday-Sunday week. In the second week of a round it would have shown 0 moves left to a manager who had used 7, and replays put a week with no moves about 20 points behind the 7-move plan. **The site now reads each matchup's length from ESPN.** In a playoff round the move limit, the nights strip and the weekly rating all cover both weeks, and the Upgrades panel suggests about 2 skater moves each Monday, carrying the rest. In the final round keep value counts only the round itself, since nothing after it matters. The fantasy season ends about Apr 4, a week before the NHL's last games.

## 47. Matching ESPN names to NHL players

The rating looks up each ESPN player in the NHL's stats by name and team. Two kinds of name broke that lookup. Vancouver has two players named Elias Pettersson, the star center and a young defenseman, so the site gave up on both and rated the star at 0.10 a game. ESPN also uses short first names where the NHL uses full ones ("Zack" Bolduc for Zachary, "Sam" Montembeault for Samuel, "Joe" Veleno for Joseph), which left those players unrated and ranked with retired names. **The lookup now uses position to split same-name teammates, and accepts a short first name when the last name, team and position group match and the first two letters agree.** The players still unmatched are mostly prospects not on an NHL roster, retired players ESPN still lists, and goalies with no NHL team this season.

## 48. Goalies get keep value too

The Goalies card (shown when you carry 3) used to name the goalie with the fewest points this week as the one to drop. A starter with one game left that isn't his start would show 0 this week, even while starting most of his team's games. Goalies now use the same keep value as skaters: this week plus 7 games' worth of the Season rating, which for a goalie is points per team game from his share of starts. In 828 three-goalie decisions from 2023-26, keep value picked a different goalie 25% of the time. When it did, the goalie it kept was worth about 1 point a week more for the rest of the season. That held in every season. **The Goalies card and the goalie upgrade list now use keep value.** The goalie stream card stays night by night.

## 49. Players who haven't played yet: suspensions and healthy regulars

The Season rating counts how often a player dresses. A skater who hasn't played a game yet got the 10% idle floor, as if he were out of the league. That made suspended Charlie McAvoy, a 2.2-points-a-game defenseman, look like a 0.2 and put him at the top of the drop list. In past seasons, regulars who had sat out the opening games dressed far more often over the rest of the season. Suspended players matched the short-injury rate of 45%, and healthy regulars who hadn't played by the second Monday dressed for 38% of the rest. **Suspended players now take the 45% share, and a healthy skater with 60+ NHL games last season who hasn't played yet takes 38%.** Everyone else keeps the old rule.

## 50. Waiver players, and fresh numbers in the morning

A player someone drops sits on waivers until ESPN's first overnight run (about 3 AM ET) at least 24 hours after the drop. Until then nobody can add him. The site used to count his games from today, so a waiver player could top Best 2 moves partly on games he can't play. **Players on waivers now count only the games after they clear, and their tag says when ("waivers, clears Sun 11").** Waiver goalies are left out of the stream card, since they can't start tonight.

The data used to refresh first at 9 AM ET. Before that the page still showed the previous evening's numbers: last night's games still counted, and on Monday it was still on last week's matchup. Three of the league's first nine adds came before 9 AM, and replays put the cost at about 3.4 points a week for a manager who makes moves in the morning. **The data now also refreshes at 6 AM ET.** When the page shows an earlier day's data, a note says so and the two cards about tonight are hidden.

## 51. A goalie's next start after a bad game

After a starter gives up 5+ goals, or is pulled after giving up 3+, coaches often go to the backup next time. The site's start model gave that goalie a 44% chance of starting the team's next game, but across 2021-26 he started only 31% of them, and the gap showed up in all five seasons. **The site now lowers his chance for the team's next game only, and moves the difference to his partner, until Daily Faceoff confirms a starter.** It's worth about 0.4 projected points for each goalie, mostly on the stream card before starters are confirmed.

## 52. A goalie's next start after any result

The last game's result matters even when it wasn't a disaster. A goalie who won his last start goes back in a little more often than the start model expects, and one who lost goes back in a little less, in every season from 2021 to 2026. A bad start also keeps a smaller drop into the team's game after next. **The site now nudges the last starter's chance up after a win and down after a loss for the team's next game, and keeps part of the bad-start drop for the game after.** About 0.1 of a start moves between partners after an ordinary win or loss, roughly a quarter of a projected point. Measured on seasons the change wasn't fit to, start predictions got about twice as much better as the bad-start rule alone made them. A Daily Faceoff confirmation still overrides it.

## 53. Goalie starts further into the week

To project a week, the site predicts each goalie start in order and treats the likeliest goalie as having started before it predicts the next one. That chaining made it too sure about games later in the week. When it gave a goalie a 90%+ chance three or four games ahead, he actually started only 77-84% of them, so clear no. 1 goalies were overstated by about 0.2 starts every four games. **The site now softens the start chances for every game after the next one, moving them a little toward an even split, and leaves the next game alone.** That cut the overstatement from 0.21 to 0.08 starts and made the predictions better in all five seasons tested. Roughly 0.15 starts a week, about 0.4 projected points, move from clear no. 1s to their partners. A Daily Faceoff confirmation still overrides it.

## 54. Trade ideas across positions

The Trade ideas card used to pair forwards only with forwards and defensemen only with defensemen. In the same 2023-26 replays, forward-for-defenseman trades with the same ESPN projection and a 0.3+ Season rating gap won just as much, about 1 point a week per trade, in all three seasons. Getting a forward was worth a bit more (+1.24) than getting a defenseman (+0.71). **The card now offers trades across positions too**, as long as your roster afterwards still has 11-13 healthy forwards and 6-8 healthy defensemen, the shapes that scored best in the lineup simulations. For Jack's team that took the skaters with a trade idea from 5 to 9 of 15.

## 55. The third goalie spot is the stream slot

The plan the simulations score is 2 regular goalies plus one roster spot that holds the current stream. So a manager following it carries 3 goalies, the third being the last streamer. The Goalies card used to say "use the extra spot on a skater" whenever you had 3. Doing that literally burns a move every time and then forces you to drop a skater on the next stream, which cost about a fifth of a matchup win every 8 weeks in the replays. **With 3 healthy goalies the card now says the third spot is your stream slot and names the goalie to drop for tonight's confirmed starter. It suggests using a spot on a skater only when you carry 4 or more.**

## 56. Trade check

To check a trade you're weighing, the page now has a **Trade check** panel. Pick the players you'd give and the ones you'd get, and it adds up their Season ratings. Across 2023-26, the Season gap between two sides predicted how they really did for the rest of the season: each 1 point of gap per team game was worth about 0.87 points per game in results. So the panel shows **about 0.87 × gap × 3.5 points a week** for the rest of the season, plus how often a gap that size picked the side that really did better: 53% under 0.1, 60% from 0.1 to 0.2, 67% from 0.2 to 0.3, 73% from 0.3 to 0.5, and 87% above that. In an uneven trade, a spot you free up counts as the best healthy free agent at those positions, and a spot you'd need counts as dropping your lowest keep value. Jack's Oct 9 trade, Slavin (1.26) for A. Protas (1.06), reads about −0.6 points a week, with that gap picking the right side 2 times in 3. The test used skaters, so the numbers for goalies are rougher.

## 57. When the moves run out, and the utility slot

Jack used his 7th move by Friday morning in both weeks so far, but the page kept suggesting adds he couldn't make. **When your team has used all its moves for the matchup, the Upgrades cards now say "No moves left this matchup; these are next Monday's targets"** and rank the free agents by Season rating (7 × Season, as in the keep value) instead of this week's points. Separately, the open-slot count treated the utility slot as a sixth D slot even when it was already holding a tenth forward. It now counts the utility slot as an open D only when 9 or fewer forwards and 5 or fewer defensemen play that night. On Saturday Oct 10's 14-game night, that removed a phantom open D for 5 of the 12 teams.

## 58. The chance a skater dresses, game by game

The site used to give a skater one chance of dressing for every game left in the week. That number is right on average over the week, but not game by game. A player who missed his team's last game is less likely to play the very next game than one later in the week: he played only 12% of next games, against the site's 18%. A regular runs the other way, 94.5% next game against 91%. **The site now shifts the chance by game number: down for the next game and up later for a player who is out, and the reverse for a regular.** Weekly totals barely move. Tonight's lineup card now uses the next-game chance, which is the decision it's for. ESPN injury caps and the long-idle cap still apply on top. The pattern held in both test seasons.

## 59. Healthy scratches come back more often than injured players

Section 58 treats every player who missed his team's last game alike. But a healthy scratch is far more likely to be back than an injured player. Healthy scratches carry no ESPN injury status, and the site gave them about a 25% chance per game when they really played 34.5%, in both test seasons, at every game number and however long they had been out. **The site now reads the NHL game page's healthy-scratch list for each team's last game and raises those players' chance of dressing (+0.5 on the log-odds scale, about 25% to 34%).** If the list can't be read, a player who missed the game with no ESPN injury status counts as a healthy scratch. Injured players are unchanged: ESPN's status already handles them. It's worth about 0.3-0.4 points a week for fringe forwards and spare defensemen, mostly in mid-week swaps and keep-or-drop calls.

## 60. Skaters who just changed teams

After a trade or waiver claim, the site judged a player's chance of dressing partly from his old team, where he was often being scratched or only up for a short stint. The new team plays him more: in his first 1-3 games with the new team, a skater who dressed for its last game played 86% of the following games while the site said 78%, in both test seasons. **For those players the site now raises the chance of dressing (+0.45 on the log-odds scale, about 78% to 85%), until his 4th game with the new team.** It's worth about a quarter of a game in a mover's first week, and these are the players people look at on the wire right after a trade.

## 61. Goalies who just changed teams

The goalie start model counts only this season's starts for a goalie's current team. So in his first few starts after a trade or waiver claim he looks like a backup with a tiny sample, even though the team got him to play him. Across five seasons, goalies with 1-3 starts for a new team started the next game 32% of the time; the site said 20%, and it was low in every season. **The site now raises those goalies' start chance (+1.0 on the log-odds scale, before the team's starts are split among its goalies) until his 4th start for the new team.** Goalies called up from the minors with no other NHL team were already about right and are unchanged. It touches about four goalies a season, worth roughly a point a week each while it lasts, and they're the goalies on the wire right after a trade. The Season rating's start share is handled separately (section 62).

## 62. A traded goalie's Season rating counts from when he arrived

The Season rating's long-run start share had the same blind spot. It divided a traded or claimed goalie's starts for his new team by every game that team had played this season, including the games before he got there. So a goalie with 1-3 starts for his new team read as an 11% starter when he went on to make about a third of the team's next 20 starts, and one with 4-8 starts read as 19% against 33%. **His share now counts only the team's games since his first start for it**, same formula as everyone else. That brings him to 29-31% and cuts the error by about a third. Other goalies are unchanged. A share that's 15-20 points too low is worth about 0.4-0.5 points a game of Season rating, enough to make a traded-in starter look like a drop.

## What the independent review changed

| Review point | Outcome |
| --- | --- |
| Whether a player dresses is the biggest issue | Agreed. Scratches count as zero and the rating multiplies by the chance he dresses. |
| A simple "season points pulled toward last season" might match the fancy model | Rechecked over 3 seasons: the per-stat model beats it by +0.04 every season. The review reproduced this. |
| One test season can't separate models | Agreed. Now 3 seasons and 72 weeks. |
| Blocks look more stable when forwards and defensemen are pooled | Agreed. All numbers are within position. |
| Rank by projected points, not percentile | Agreed. The site sorts by points and shows percentile as a badge. |
| News should change games or ice time, not points directly | Agreed. That's how the news step is designed. |
