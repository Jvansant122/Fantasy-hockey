# Ideas ledger: every research idea tried or queued

One row per idea that has been tested, proposed or queued for the Claude Rating, so nothing gets re-tested by accident or forgotten. Started Oct 9, 2026 from `findings.md` (sections 1-58), `ideas.md`, `handoff.md`, `xg/findings.md`, `correlation/findings.md`, `season-rating/`, `injury/` and `literature-review.md`.

**Where it lives.** The working copy is `/mnt/project-files/research/claude-rating/ideas-ledger.md`; research sessions append there. The repo copy, `docs/ideas-ledger.md`, is a published mirror that the build thread refreshes from the working copy whenever it opens a PR.

## Rules

1. **Before testing an idea, search this ledger** (and `findings.md`) for it. If a row covers it, don't re-run it unless you can say what is different this time (new data, a new season, a different horizon or population, a bug in the old test). Put that reason in your new row.
2. **Every research run appends its row(s) before finishing**, even when the answer is "no change" or the run was abandoned. One row per idea. Re-tests get a new row that names the old one ("re-test of 31").
3. **IDs.** Ideas listed in `ideas.md` keep their number (1, 2, ... 53). Add new ideas to `ideas.md` with the next free number and use that number here. Standalone studies use a short prefix: `base-` (findings 1-9), `xg-`, `corr-`, `season-`, `inj-`, `lit-` (literature review), `live-` (needs live 2026-27 logs). Batch 3 ideas from `ideas-batch3.md` take numbers once they are copied into `ideas.md`.
4. **Verdict** is one of: `Shipped (PR #n)`, `Display only (PR #n)`, `No change`, `Optional, not built`, `Pending` (queued, not run), `Waiting until <date>` (needs data that doesn't exist yet), `Needs Jack`, `Not run` (with why).
5. **Result** is the measured number that decided it, with its baseline, in the units the source uses (rank corr, FP per pick, FP/roster/week). Keep it to one line; the source has the detail.
6. When the build thread ships or declines a handoff item, it updates the matching row's Verdict here.
7. Re-read the file just before writing and keep edits small: several sessions share it. Never delete rows.

**Run by:** Plan = thread "Plan the Claude Rating"; Hourly = the hourly research routine; xG = "Build an expected goals model"; Corr = "Raise the model's correlation"; Season = "Rest-of-season rating accuracy"; Injury = "Games missed to injury model"; Lit = "Hockey prediction literature review". Dates are 2026, UTC.

**Metrics:** "rank corr" is Spearman with next week's FP per game among skaters who dressed, rolling origin over 2023-24 to 2025-26, unless the row says weekly-total (scratches count as 0) or rest-of-season. "Waiver" or "wire" is the free-agent pool.

## Ledger

| ID | Date | Run by | Idea / what was tested | Result | Verdict | Source |
| --- | --- | --- | --- | --- | --- | --- |
| base-1 | Oct 8 | Plan | Which stats are stable (split-half reliability at 20 games) | TOI 0.97, PP TOI 0.93-0.95, hits 0.80-0.84, SOG 0.69-0.76, FP 0.62-0.64, blocks 0.44 F / 0.58 D, goals 0.28-0.37 | Shipped (PR #1, informs ridge inputs) | findings 1 |
| base-2 | Oct 8 | Plan | Which inputs and windows predict next week | TOI last 5-10 games best (0.32-0.34); scoring stats best season-to-date; last-5 FP weakest (0.23) | Shipped (PR #1) | findings 2 |
| base-3 | Oct 8 | Plan | Per-stat ridge vs shrunk FP/G vs gradient boosting | Ridge 0.38 F / 0.36 D, +0.045 over shrunk FP/G in all 3 seasons; boosting ties | Shipped (PR #1) | findings 3 |
| base-4 | Oct 8 | Plan | P(dresses) from games dressed in team's last 3 | Calibrated (0 of 3: 0.23 pred / 0.22 actual); 11% of wire weeks are zero-game | Shipped (PR #1), replaced by corr-9 (PR #16) | findings 4 |
| base-5 | Oct 8 | Plan | Pickup simulation: FP of each method's weekly top 5 | Ridge × expected games 6.2 F / 6.1 D vs site's season FP/G × games 5.2 / 5.3 | Shipped (PR #1) | findings 5 |
| base-6 | Oct 8 | Plan | Goalies: rank by projected starts instead of FP/G × team games; predict points per start | Starts 4.9 FP/week vs 3.0 (random 2.9); every pts/start predictor under 0.1 corr | Shipped (PR #1, PR #7) | findings 6 |
| base-7 | Oct 8 | Plan | Claude API news step: ceiling value of perfect news | Skaters +0.17 to +0.58 per pickup (ceiling); goalie confirmed starters +0.76 FP/day | Confirmed starters shipped via Daily Faceoff (PR #8); API step Needs Jack (ANTHROPIC_API_KEY), low value now | findings 7, 9 |
| base-8 | Oct 8 | Plan | Opponent strength, home ice, back-to-backs | Skater opponent adjustment moves picks under 0.02 FP; goalie home +0.21/start | No change for skaters; goalie home ice in production model | findings 8 |
| base-9 | Oct 8 | Plan | Production package without last-5/10/20 xG windows | 0.384 F / 0.358 D, same as full model | Shipped (PR #1) | findings 9, production/ |
| 1 | Oct 8-9 | Plan | Net pickup value and the 6-move budget | 6th pickup still +1 FP/week over the drop; first-week edge is schedule | Shipped (use all moves; later refined by 44-46, 50) | findings 10 |
| 2 | Oct 8-9 | Plan | Goalie rest and workload in the start model | Weekly start error 0.584 → 0.554; B2B second night 9-20% | Shipped (PR #16) | findings 11 |
| 3 | Oct 8-9 | Plan | Do ice-time jumps stick? | About half persists; PP TOI rises keep only 17-28%; ridge error flat across jumps | No change (ridge already prices it) | findings 12 |
| 4 | Oct 8-9 | Plan | Rookies and call-ups: AHL scoring, draft slot, age | No gain once a player has 1+ NHL game | No change | findings 13 |
| 5 | Oct 8-9 | Plan | PP1 membership instead of PP TOI | No gain over PP minutes | No change | findings 14 |
| 6 | Oct 8-9 | Plan | Calibrate P(dresses) with ESPN injury status | Cross-section only: OUT/IR → 0, DTD → 0.5 | Shipped (PR #6, handoff 5); caps revised by inj-1 | findings 15 |
| 7 | Oct 8-9 | Plan | Mid-season retraining | No drift; no gain | No change (train once a year) | findings 16 |
| 8 | Oct 8-9 | Plan | Weekly drop-candidate ranking | Drop by projected week +3.6 vs +1.7 | Shipped (PR #6, drop list) | findings 17 |
| 9 | Oct 8-9 | Plan | Light-night open-slot value | 28 open slots a week, 64% on light nights | Shipped (nights strip, PR #1) | findings 18 |
| 10 | Oct 8-9 | Plan | Multi-position eligibility and UTL | No F/D players; UTL acts as a 6th D | Shipped (PR #6, handoff 4) | findings 19 |
| 11 | Oct 8-9 | Plan | Scoring in first 5 games back from a 2+ week absence | At their old rate, no rust | No change | findings 20 |
| 12 | Oct 8-9 | Plan | Team shot environment as a skater input | Nothing beyond on-ice xG | No change | findings 21 |
| 13 | Oct 8-9 | Plan | Per-team goalie tandem rules | Per-team model worse than pooled; alternation adds nothing | No change | findings 22 |
| 14 | Oct 8-9 | Plan | Hits penalty for forwards | Zero information beyond the 0.1 per hit | No change | findings 23 |
| 15 | Oct 8-9 | Plan | Shooting % regression by shot danger | Nothing beyond last season's goals | No change | findings 24 |
| 16 | Oct 8-9 | Plan | Rating vs ESPN projection, live | Backtest proxy: ridge beats a season rate by +0.05; live log running | Shipped logging (PR #8); scoring Waiting until Oct 30 (live-1) | findings 25 |
| 17 | Oct 8-9 | Plan | Weekly 6-move optimizer | Monday rule +5.4 FP/week beats schedule-aware and daily streaming | Shipped (handoff 3); refined by 44-46, 50 | findings 26 |
| 18 | Oct 8-9 | Plan | Opponent-aware goalie streaming | Opponent adds nothing; goalie's own shrunk pts/start +0.14/week | Shipped (PR #7, handoff 2) | findings 27 |
| 19 | Oct 8-9 | Plan | Two seasons ago vs last season | Last season is enough | No change | findings 28 |
| 20 | Oct 8-9 | Plan | Uncertainty / confidence badge | Low-game players not projected worse | No change | findings 29 |
| 21 | Oct 9 | Plan | Betting lines for goalie points per start | Favourite's goalie +1.2-1.4 FP/start: pts = 2.9 + 4.1 × (p_win − 0.5) | Shipped (PR #10, handoff 1) | findings 30 |
| 22 | Oct 9 | Plan | Availability model v2 (logistic, days since last game, TOI rank) | +0.02 FP per pick | No change then; superseded by corr-9 | findings 31 |
| 23 | Oct 9 | Plan | Short-handed TOI as input | Nothing | No change | findings 32 |
| 24 | Oct 9 | Plan | Separate F and D models | +0.001 to +0.003 | Optional, not built | findings 33 |
| 25 | Oct 9 | Plan | Primary vs secondary assists | Nothing | No change | findings 34 |
| 26 | Oct 9 | Plan | Age curve on last season's numbers | Curve real (+0.2 at 19-22, −0.1 at 34+) but model already has it | No change | findings 37 |
| 27 | Oct 9 | Plan | Upside / boom weeks for H2H | Same ranking as the mean | No change | findings 35 |
| 28 | Oct 9 | Plan | Two-week matchup horizon | Same projection × games (7- and 14-day fits corr 0.999) | No change | findings 36 |
| 29 | Oct 9 | Plan | Linemate quality from shift charts | Not run: expected small (team context adds nothing, idea 12) | Not run | ideas.md |
| 30 | Oct 9 | Plan | Daily Faceoff starters/lineups and ESPN injury notes, live | Logging since PR #8 | Waiting until Oct 30 (live-1) | ideas.md |
| 31 | Oct 9 | Plan | PDO and possession stats (Corsi, Fenwick, xGF%, zone starts) | PDO is luck (0.08 split-half); as inputs slightly worse | No change; "running hot" note Display only (PR #12, handoff 8) | findings 38 |
| ens | Oct 9 | Plan | Ensembles: ridge by position, bigger GBM, ridge + GBMs averaged | Best +0.0018 ± 0.0005; wire picks identical | No change | findings 39 |
| 32 | Oct 9 02:39 | Hourly | Opening weeks for skaters: ridge with 1-4 games vs leaning on last season | Ridge holds; blending last season +0.008 (noise) | No change | findings 40 |
| 33 | Oct 9 02:39 | Hourly | Goalie start shares in a team's first 10 games | Backup understated 0.8 starts after game 1; shrinking to last season's split cuts error 20% | Shipped (PR #14, handoff 10) | findings 41 |
| 34 | Oct 9 03:39 | Hourly | P(dresses) in the opening weeks | Logistic holds up; shrinking to last season's GP worse | No change | findings 42 |
| 35 | Oct 9 03:39 | Hourly | Last season's pts/start as goalie prior instead of league mean | Corr 0.068 → 0.079 | Optional, not built | findings 43 |
| 36 | Oct 9 04:40 | Hourly | Goalie relief appearances | 0.07 FP per team game (~0.14/week for a backup) | No change | findings 44 |
| 37 | Oct 9 04:40 | Hourly | Team overtime rate (OTL +1) | Year-to-year −0.05, pure chance | No change | findings 45 |
| 38 | Oct 9 04:40 | Hourly | Quality vs quantity weighting (FP/G^a × games^b) | Plain product best; ridge slope 0.96 | No change | findings 46 |
| 39 | Oct 9 05:40 | Hourly | Arena scorekeeper bias in hits/blocks/shots | Real (hits sd 16%) but moves a week 0.02 FP/G | No change | findings 47 |
| 40 | queued | Hourly | Live check of 2026-27 opening weeks: goalie start shares (33) and P(dresses) vs actual lineups | Needs 3 weeks of games | Waiting until ~Oct 27 | ideas.md |
| 41 | Oct 9 06:40 | Hourly | Backup goalie when starter flagged out | Site gave backup 1.0 starts / 4 games vs 2.2 actual; renormalize | Shipped (PR #17, handoff 12) | findings 48 |
| 42 | Oct 9 07:40 | Hourly | Next man up when top PP player is out | Forwards nothing; 2nd PP defenseman +0.25 FP/G (± 0.18) | No change; recheck 2nd-PP D live | findings 49 |
| 43 | Oct 9 07:40 | Hourly | Ridge bias for players who just changed teams | Week 1 +0.10, weeks 2-5 −0.08 FP/G | No change | findings 50 |
| 44 | Oct 9 08:40 | Hourly | Hold some of the 6 moves for mid-week injuries | Hold 1-2: +1.1 to +1.4 FP/roster/week | Shipped (PR #18, handoff 13) | findings 51 |
| 45 | Oct 9 09:40 | Hourly | Split moves between skater pickups and goalie streams | 2-3 skater + 3-4 streams: +4.3 to +4.6 FP/week vs all skaters | Shipped (PR #19, handoff 14) | findings 52 Audit Oct 9: all 12 managers followed the plan and shared one pool; with one active manager extra Monday skater moves help slightly (+0.4), plan still wins. Re-run under real settings (7 pooled, one manager, real rosters): shape confirmed, numbers superseded by findings 59. |
| 46 | Oct 9 10:40 | Hourly | Goalie streams and injury swaps from one budget; 7-start cap | 2 skater moves Monday + 4 spare: +5.8 FP/week (+4.1 with cap) | Shipped (PR #20, handoff 15) | findings 53 Audit Oct 9: same 12-identical-managers caveat; plan still wins with one active manager. Re-run under real settings (7 pooled, one manager, real rosters): shape confirmed, numbers superseded by findings 59. |
| 47 | Oct 9 10:40 | Hourly | Which wire goalie to stream (own pts/start, team, opponent, home) | All within noise; best +0.4 FP/stream | No change | findings 54 |
| 48 | Oct 9 11:40 | Hourly | Spend leftover moves Fri/Sat on skaters | Best +0.1, noise | No change | findings 55 Audit Oct 9: 12 identical managers; re-check with one active manager. Re-run under real settings (7 pooled, one manager, real rosters): shape confirmed, numbers superseded by findings 59. |
| 49 | Oct 9 11:40 | Hourly | Roster a third goalie | −2.7 FP/week vs 2 goalies + streaming | No change | findings 55 Audit Oct 9: UNPROVEN, assumed 2 goalies/16 skaters per team; league averages 3.3 G / ~19 skaters. Re-test (idea 98). Re-run under real settings (7 pooled, one manager, real rosters): shape confirmed, numbers superseded by findings 59. |
| 50 | Oct 9 12:40 | Hourly | Rank adds/drops by this week + future weeks (8-week carry-over sim) | Horizon 2: +1.75 FP/roster/week (2 moves), +2.3 (6 moves) | Shipped (PR #22, handoff 17) | findings 57 |
| 51 | queued | Lit | Blend Claude Rating with ESPN's projection | Needs weeks of logged ESPN projections | Waiting until late Oct (live-1) | ideas.md, literature-review.md §21 |
| 52 | queued | Lit | Linemate pairing for managers behind in a matchup (same-line points correlate) | Not run | Pending | ideas.md, literature-review.md |
| 53 | queued | Lit | Season FP/G: 3 past seasons weighted, age adjustment, team PP time regressed | Not run; season-1..3 found the FP/G side flat, check first | Pending (low priority) | ideas.md, literature-review.md |
| xg-1 | Oct 9 | xG | Own xG model from play-by-play (567k shots) | AUC 0.75-0.79; r = 0.986 with MoneyPuck; predicts next-season goals 0.837 vs MP 0.817 | Built (research only) | xg/findings 1-2 |
| xg-2 | Oct 9 | xG | Expected FP/G as a ranker, and added to the rating | Beats actual FP/G by +0.017-0.025, but rating beats xFP/G by 0.04; adding it +0.002-0.003 | No change to rating; xFP/G Display only (PR #13, handoff 9) | xg/findings 3-4 |
| xg-3 | Oct 9 | xG | Does luck regress (sell-high / buy-low) | 76% of FP − xFP gap disappears in 4 weeks; rating over-projects luckiest tenth by 0.08 | Display only: "running hot/cold" (PR #13) | xg/findings 5 |
| xg-4 | Oct 9 | xG | NHL EDGE tracking (speed, zone time, hardest shot) | −0.001 (1 week), −0.002 (4 weeks) | No change | xg/findings 6 |
| xg-5 | Oct 9 | xG | Goalie GSAx vs save % for points per start | GSAx worse than shrunk save % (0.086 vs 0.137) | No change | xg/findings 7 |
| corr-1 | Oct 9 | Corr | Leakage and target check of the backtest | Clean | No change | correlation/findings 1 |
| corr-2 | Oct 9 | Corr | Proper noise ceiling for one-week corr | Ceiling 0.57 all / 0.45 waiver; rating at 92% / 87%; matches hindsight season average | No change | correlation/findings 2-3 |
| corr-3 | Oct 9 | Corr | Usage × efficiency: shrunk per-60 rates × last-5 TOI | +0.0039 all / +0.0051 waiver; site-friendly version +0.0022 | Optional, not built | correlation/findings 4 |
| corr-4 | Oct 9 | Corr | Player random effect (persistent "beats the model" term) | +0.0001 on top of corr-3 | No change | correlation/findings 4 |
| corr-5 | Oct 9 | Corr | Week context: opponent GA, home share, back-to-backs | Flat to negative on top of corr-3 | No change | correlation/findings 4 |
| corr-6 | Oct 9 | Corr | Train on 4-week targets | −0.0017; 1+4 week average +0.0022 | No change | correlation/findings 4 |
| corr-7 | Oct 9 | Corr | Stacked LightGBM + ridge average | +0.0044 all / +0.0068 waiver; +0.10 FP per pick (ns) | Optional, not built | correlation/findings 4 |
| corr-8 | Oct 9 | Corr | Headroom from knowing who sits all week | Waiver weekly-total corr 0.575 → 0.634 with perfect zero-game knowledge | Motivates live-1 | correlation/findings 5 |
| corr-9 | Oct 9 | Corr | Logistic P(dresses) with out streak and 18 features | Weekly-total +0.015 all / +0.021 waiver | Shipped (PR #16, handoff 11) | correlation/findings 6 |
| season-1 | Oct 9 | Season | FP/G ridge refit on rest-of-season targets | −0.003 all / −0.008 waiver | No change | findings 56 |
| season-2 | Oct 9 | Season | Comparables (200 nearest past player-weeks) | −0.012 alone; 0.000 half-blended | No change | findings 56 |
| season-3 | Oct 9 | Season | Boosted residual with team-share inputs (point share, PP share) | −0.008 / −0.018 | No change | findings 56 |
| season-4 | Oct 9 | Season | Rest-of-season P(dresses) logistic + lineup chain | ROS corr 0.773 → 0.797 (waiver 0.671 → 0.701); independent rebuild +0.021 / +0.024 | Shipped (PR #21, handoff 16) | findings 56 |
| inj-1 | Oct 9 | Injury | Softer weekly caps OUT 0.35 / IR 0.15 / DTD 0.5, 0 for long-absence notes | +0.0066 all / +0.0090 waiver; independent rebuild +0.011-0.013 | Shipped (PR #24, handoff 18) | findings 58 |
| inj-2 | Oct 9 | Injury | Logistic per-game model on all note features (body part, timelines) | +0.002, lost to simple caps out of sample | No change | findings 58 |
| inj-3 | Oct 9 | Injury | Note-based Season rating tweaks (season-ending 0, surgery × 0.8, return date) | +0.0014 ± 0.0012, not significant | No change | findings 58 |
| inj-4 | Oct 9 | Injury | Post-return TOI dip and cut-short last game | TOI −0.15 min for ~5 games; adjustment +0.0002 | No change | findings 58 |
| inj-5 | Oct 9 | Injury | Games still to miss by status and body part (medians) | DTD 0, OUT 3-5, IR 9-14, surgery/LTIR 16 | Display only: "usually back in about N games" (PR #24) | findings 58, injury/games_missed_table.txt |
| live-1 | Oct 30 | Coordinator (scheduled) | 3-week log review: Daily Faceoff "likely" starter reliability, injury caps vs real ESPN flags, who sits all week from lineups, rating vs ESPN projection (16, 30, 51) | Not run | Waiting until Oct 30 | data/log/, trigger trig_014aqpuBGneWvQ3cesyakTyE |
| lit-1 | Oct 9 | Lit | Literature review gaps where site logs are new evidence: DFO starter accuracy, DTD outcomes, ESPN projection accuracy | Covered by live-1 | Waiting until Oct 30 | literature-review.md |
| 54 | Oct 9 | Ideas | Bank leftover moves on Sunday (simple) | Not run | Pending | ideas-batch3.md |
| 55 | Oct 9 | Ideas | Start/sit on crowded nights (simple) | Not run | Pending | ideas-batch3.md |
| 56 | Oct 9 | Ideas | When to make Monday moves: 9 AM, 12:30 or 5 PM (simple) | Not run | Pending | ideas-batch3.md |
| 57 | Oct 9 | Ideas | Underdog defensemen block more shots (simple) | Not run | Pending | ideas-batch3.md |
| 58 | Oct 9 | Ideas | Healthy scratches on the second night of a back-to-back (simple) | Not run | Pending | ideas-batch3.md |
| 59 | Oct 9 | Ideas | Do coaches save the starter for strong opponents? (simple) | Not run | Pending | ideas-batch3.md |
| 60 | Oct 9 | Ideas | Call-ups: do they stay in the lineup? (simple) | Not run | Pending | ideas-batch3.md |
| 61 | Oct 9 | Ideas | Fantasy playoff schedule (simple) | Not run | Pending | ideas-batch3.md |
| 62 | Oct 9 | Ideas | Value over replacement by position (simple) | Not run | Pending | ideas-batch3.md |
| 63 | Oct 9 | Ideas | Line-blending coaches and the last-5 TOI window (simple) | Not run | Pending | ideas-batch3.md |
| 64 | Oct 9 | Ideas | Goalie Season rating (job security) (moderate) | Not run | Pending | ideas-batch3.md |
| 65 | Oct 9 | Ideas | Goalie odds for the rest of the week (moderate) | Not run | Pending | ideas-batch3.md |
| 66 | Oct 9 | Ideas | Who sits when a regular comes back (moderate) | Not run | Pending | ideas-batch3.md |
| 67 | Oct 9 | Ideas | IR stash pickups (moderate) | Not run | Pending | ideas-batch3.md |
| 68 | Oct 9 | Ideas | Healthy-scratch history as a P(dresses) input (moderate) | Not run | Pending | ideas-batch3.md |
| 69 | Oct 9 | Ideas | Rolling-horizon move planner (extreme) | Not run | Pending | ideas-batch3.md |
| 70 | Oct 9 | Ideas | Team lineup solver (extreme) | Not run | Pending | ideas-batch3.md |
| 71 | Oct 9 | Ideas | Twelve-manager league simulator (wins, not points) (extreme) | Not run | Pending | ideas-batch3.md |
| 72 | Oct 9 | Ideas | Late-season rest and tanking (extreme) | Not run | Pending | ideas-batch3.md |
| 73 | Oct 9 | Ideas | Joint weekly points simulator (extreme) | Not run | Pending | ideas-batch3.md |
| 74 | Oct 9 | Ideas | At what horizon does the Season rating beat the weekly one? (simple) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 75 | Oct 9 | Ideas | Error map: where the 0.797 loses (simple) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 76 | Oct 9 | Ideas | Level calibration by tier (simple) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 77 | Oct 9 | Ideas | Day-to-day jumpiness (simple) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 78 | Oct 9 | Ideas | Remaining schedule: per team game vs total (simple) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 79 | Oct 9 | Ideas | Do shot-blockers and hitters get hurt more? (simple) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 80 | Oct 9 | Ideas | Waiver-exempt players get sent down (simple) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 81 | Oct 9 | Ideas | Do heavy-workload goalies fade late? (simple) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 82 | Oct 9 | Ideas | This season's weight by age at the season horizon (simple) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 83 | Oct 9 | Ideas | Idle players: sent down, scratched or injured (simple) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 84 | Oct 9 | Ideas | Filling in for an injured teammate (moderate) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 85 | Oct 9 | Ideas | Patience rule for slumping stars (moderate) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 86 | Oct 9 | Ideas | Option value of uncertain players (moderate) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 87 | Oct 9 | Ideas | Score the Season rating by decisions, not Spearman (moderate) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 88 | Oct 9 | Ideas | Playoff-weighted Season rating (moderate) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 89 | Oct 9 | Ideas | Rest-of-season games-played simulator (extreme) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 90 | Oct 9 | Ideas | League-wide goalie depth-chart simulator (extreme) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 91 | Oct 9 | Ideas | AHL call-up pipeline (extreme) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 92 | Oct 9 | Ideas | Trade deadline model (extreme) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 93 | Oct 9 | Ideas | Joint talent-and-availability model with intervals (extreme) | Not run | Pending | ideas-batch4.md (Season rating only) |
| 94 | queued | Lit 23.6 | Empty-net points: top-line forwards on betting favourites vs the rating | Not run | Pending | ideas.md |
| 95 | queued | Lit 23.6 | Marcel-style goalie projection vs rest-of-season goalie rating | Not run | Pending | ideas.md |
| 96 | queued | Lit 23.6 | Ice time only from games under a new coach after a coaching change | Not run | Pending | ideas.md |
| 97 | queued | Lit 23.6 | NHL final-week resting vs ESPN fantasy playoff weeks | Not run | Pending | ideas.md |
| 98 | Oct 9 13:40 | routine | Third goalie with real roster sizes and one active manager (in idea 101 sim) | 2G + streaming beats 3G by +2.0 FP/week | No change (finding 55 confirmed) | findings 59 |
| 99 | queued | Lit 24 | Backtest goalie Season rating vs rest-of-season results; last-season weight, start split, team strength | Not run | Pending | ideas.md |
| 100 | queued | Lit 24 | Injury rates C vs W; multi-season injury history; age in skater games-played model | Not run | Pending | ideas.md |
| 101 | Oct 9 13:40 | routine | Move sim: pooled 7 moves/matchup, one active manager, 12F/7D/3G rosters | 2 Mon + injuries + goalie streams +5.6 FP/week vs all Monday; 7th move +1.3-2.9; 2G + extra skater +2.0 over 3G | Pending (handoff 21) | findings 59 |
| lit-2A | queued | Lit (2nd pass) | Empty-net points for top-6 F / PP1 D by team implied win probability; favourite × top-of-lineup term, or confirm implied-total tiebreak covers it | Not run; expected 0.1-0.3 FP/G for a few dozen players | Pending | literature-review.md §23.6 A |
| lit-2B | queued | Lit (2nd pass) | Hockey Marcels goalie Season rating (weights 1/.6/.5/.3, +1,525 shots at league avg, decline from 30) vs shrink to 2.9 over 15 starts | Not run; goalie Season rating never tested | Pending | literature-review.md §23.6 B |
| lit-2C | queued | Lit (2nd pass) | After mid-season coaching changes, TOI windows from post-change games only vs last-5/10 | Not run; few team-weeks a season | Pending | literature-review.md §23.6 C |
| lit-2D | queued | Lit (2nd pass) | Late-season resting: dress rates and goalie splits for clinched/eliminated teams if NHL's final week overlaps fantasy playoffs | Not run | Pending (matters only in March-April) | literature-review.md §23.6 D |
| lit-2E | Oct 9 | Lit (2nd pass) | Goalie back-to-back, blowout carry-over, contract year, blocks rink bias | Literature agrees with existing calls (goalie B2B penalty ~0.004 on 40k games; contract year no average effect) | No change | literature-review.md §23.6 E |
| audit | Oct 9 | "Audit the repo's assumptions" | Skeptic re-test of settled conclusions and constants | Running | Pending: add one row per re-test (name the original row) | thread, handoff.md if any |
| lit-3S1 | queued | Lit (Season pass) | Backtest goalie Season rating on ROS goalie FP per team game; tune pts/start prior (15 → 30-60 starts, prior = last season or Marcel blend), start-share prior (10 → 20-40 games of last season's split), add team-strength term | Not run; goalie Season never tested; literature says goalie numbers need far more prior than skaters | Pending (highest Season priority; supersedes lit-2B) | literature-review.md §24.7 S1 |
| lit-3S2 | queued | Lit (Season pass) | Lineup chain: separate center and winger drop rates | Not run; centers' injury risk 1.4-1.7× (Martin et al. 2024) | Pending (small) | literature-review.md §24.7 S2 |
| lit-3S3 | queued | Lit (Season pass) | Season dress model: games missed to injury over 2-3 seasons (injury notes) instead of last season GP/82, kept separate from scratches | Not run; prior injury is the strongest general predictor | Pending | literature-review.md §24.7 S3 |
| lit-3S4 | queued | Lit (Season pass) | Age as an input to the season dress logistic | Not run | Pending (small) | literature-review.md §24.7 S4 |
| lit-3S5 | queued | Lit (Season pass) | Season P(dresses) inside keep value (findings 57) instead of weekly P(dresses) | Not run; findings 57 flags it | Pending (small) | literature-review.md §24.7 S5 |
| lit-3x | Oct 9 | Lit (Season pass) | Literature check of row 53 (Season FP/G with 3 seasons, age, team PP regression) | Szymborski 2026: prior-heavy projections barely move with in-season data (62/38 after 5 months); with season-1..3 flat, nothing expected | Recommend closing row 53 | literature-review.md §24.2 |
