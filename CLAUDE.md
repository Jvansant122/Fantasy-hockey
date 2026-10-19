# Fantasy hockey free agent finder

A one-page site (GitHub Pages) that ranks free agents in Jack's ESPN fantasy hockey league by the **Claude Rating**: projected fantasy points for the rest of the current Monday-Sunday matchup. Live at https://jvansant122.github.io/Fantasy-hockey/.

## Layout

- `index.html` - the whole site: one page, inline CSS and JS, reads `data/players.json`.
- `scripts/fetch.py` - ESPN league, rosters, free agents and schedule -> `data/players.json`. Calls `rating.add_ratings` and `leads`.
- `scripts/rating.py` - the Claude Rating (NHL stats API + MoneyPuck). Weights in `model/rating_model.json`.
- `scripts/leads.py` - Daily Faceoff starting goalies (used by the rating), Daily Faceoff injury and call-up news for the Week and Season ratings (`data/dfo_news.json`), and the day-of log in `data/log/`.
- `scripts/winners.py` - the Predicted winners tab: each NHL game's win chance rolled up from the player projections (lineup points x chance to dress, starter save%, goal differential, back-to-backs) with picks frozen at puck drop, and each fantasy matchup's win chance from ESPN's points so far plus the Week ratings. Written into `players.json` as `winners`.
- `scripts/season_boost.py` - the three boosted models (trained on 16 seasons): Season dress share, Season FP/G, and the Week's next-week dress share (`season_boost_week.json`). Rebuilds their inputs from the run's NHL data and walks the trees in `model/season_boost_*.json` in plain Python.
- `model/train_boost.py` - refits `model/season_boost_dress.json`, `season_boost_fpg.json` and `season_boost_week.json` offline (`--models=dress,fpg,week`) from the research's 16-season panel in `/mnt/project-files/research/claude-rating/hunt11b/data` (the week model also needs `hunt13/data/wk_targets.parquet` and `hunt13/feats.py`; pandas, scikit-learn). The JSON is committed; the daily run only reads it.
- `model/train_toi_boost.py` - refits `model/season_boost_toi.json` and `season_boost_pptoi.json` offline (handoff 668): boosted next-week TOI/G and PP TOI/G on the Week ridge's inputs (`train.prep`, no hinges or products) plus team games. `rating.forecast_features` puts them in place of the linear forecasts for the Season FP/G only; rerun it after `model/train.py` changes the forecast ridges or the ridge inputs.
- `model/season_dress.json` - the Season rating's rest-of-season dress logistic; `model/season_range.json` - the Season rating's 80% range table. Both fit in the research and read by `rating.Model`.
- `model/train.py` - refits `model/rating_model.json` from the research data in `/mnt/project-files/research/claude-rating` (needs pandas, scikit-learn, pyarrow). The forecast ridges' teammate and PP-share inputs (handoff 678, `add_teammates`; computed live by `rating.teammate_inputs`) come from that folder's `hunt14/toi/*.parquet` tables (built by the `build_*.py` scripts there); rerun `model/train_toi_boost.py` after it.
- `data/` - written by the "Update data" workflow four times a day (about 6 AM, 9 AM, 12:40 PM, 5 PM ET). GitHub's own cron skips most runs, so two Claude routines in the build thread ("Start the data update") dispatch it on `main` at those times, and two more ("Recheck the data update") retry 30 minutes later if no run on `main` succeeded. Only runs on `main` count, so test runs on a branch never stand in for a slot. The crons stay as a backup, and a scheduled run skips itself once main's data is newer than the latest ET slot (`scripts/slot_gate.py`). Each branch has its own concurrency queue, so a branch test never cancels a waiting main run. Don't hand-edit; merge `main` into your branch before pushing because the bot commits here often. A test run on a branch commits its data to that branch: run `git checkout origin/main -- data` and commit before opening or merging the PR, since the Tests check fails any PR that changes `data/` (handoff 265).
- `docs/research.md` - plain-language research write-up for league members (charts in `docs/img/`).
- `docs/ideas-ledger.md` - every research idea tried or queued, with its result and verdict. Mirror of `/mnt/project-files/research/claude-rating/ideas-ledger.md`.
- `tests/` - pytest suite, run by the "Tests" workflow on every PR: `test_add_ratings.py`, `test_site_contract.py`, `test_page.py`, `test_winners.py` (Predicted winners), `test_leads.py` (Daily Faceoff parsing), `test_model.py` (model files load and give sane outputs), `test_helpers.py` (small helpers in `rating.py`, `fetch.py` and `slot_gate.py`), `test_team_change.py` (players the NHL moves before ESPN does), `test_fetch_main.py` (`fetch.py main()` end to end on a fake ESPN league, `fake_espn.py`, across clocks, matchup shapes, feed failures and postponed games; every output passes `check_data.rules`), `test_audit_invariants.py` (rules every version keeps across all teams and clocks: cards never offer more adds than moves left, Activate from IR's drop is never offered a replacement, never drop tonight's starting goalie, swap subs beat the player they replace, page numbers keep the publish rules after the first puck, one definition per module-level name). `test_mutate_*.py` (handoffs 700-709: pin the direction and wiring of rating constants, publish checks, move cards and details text so a zeroed or flipped rule fails a test). Prefer adding a rule there over a one-case test when fixing a bug. `tests/test_page.py` loads the page in Chromium (Playwright) with the real data, picks every team, filters, sorts, opens details, and also loads it with ratings stripped; any JS error fails the build.
- `scripts/check_data.py` - the update workflow runs it before committing data (a broken `players.json` is never published) and after (fails the run, so GitHub emails Jack, when the Claude Rating is missing). Add a field to its lists when the page starts depending on one. Before publishing it also checks rules every version keeps (`rules`, `problems_vs_prev`: games left match the nights, expected games within games left, Week = pts/game x games, goalie starts within team games, nights on their dates) and prints warnings (`warnings`) when a change moves ratings for players who haven't played. A deliberate model change that trips a warning is fine; a rule failure blocks publishing.
- `.claude/hooks/session-start.sh` - cloud sessions install the test and research libraries on start.

## Run the tests

```
pip install -r requirements-dev.txt
python -m pytest -n auto
```

`-n auto` (pytest-xdist) runs the tests on every CPU, as the Tests workflow does: about 1 minute instead of 3. Plain `python -m pytest` still works.

No network or secrets needed: every ESPN, NHL, MoneyPuck and Daily Faceoff call is faked.

- `tests/test_add_ratings.py` runs `add_ratings` end to end on a tiny made-up league. `fetch.py` catches rating errors so the site still publishes without ratings, so this test is the only thing that catches a broken rating before merge.
- `tests/test_site_contract.py` checks `data/players.json` has every field `index.html` reads, and that the page's script parses (`node --check`). If you add a `p.<field>` to the page, add it to the data too; if you rename a field in `fetch.py` or `rating.py`, update the page.
- Tests check invariants (fields present, probabilities in range, long-term injured players at 0, confirmed starters honoured), not exact rating values, so a deliberate rating change shouldn't need test edits unless it changes those rules.

## Conventions

- The daily scripts use only the standard library plus `requests` (the workflow installs nothing else). Heavy libraries belong in `model/train.py` or research code.
- League: 12-team H2H points, daily lineups, one acquisition per day of the matchup, pooled (ESPN `matchupAcquisitionLimit` 1 with `matchupLimitPerScoringPeriod`: 7 a normal week, 6 in the 6-day first matchup, 14 in each two-week playoff round (Mar 8-21 and Mar 22-Apr 4, 2027; `fetch.matchup_days` reads the length from ESPN); several can be used the same day). Scoring: G 2, A 1, PPP 0.5, SHP 0.5, SOG 0.1, HIT 0.1, BLK 0.5; goalie W 2, L -1, OTL +1, GA -1, SV 0.2, SO 3. Lineup 9F/5D/1UTL/2G; the UTL takes a forward or a defenseman (the page counts it as a sixth D slot unless a tenth forward fills it, and an empty UTL is room for either).
- One ESPN scoring period is one day. ESPN's 2027 season is the NHL's 20262027.
- `api.nhle.com` returns 403 without a browser-like User-Agent.
- ESPN and Daily Faceoff team abbreviations differ from the NHL's: see `ESPN_TO_NHL` in `rating.py` and `DF_TO_NHL` in `leads.py`.
- Secrets (`LEAGUE_ID`, `ESPN_S2`, `SWID`, optional `TEAM_ID`, any future API key) live only in GitHub Actions secrets. Never put them in code, commits, logs or chat.
- Commit messages and PR titles describe what a league member would see, in plain words.

## Working with Claude in the project

- Work on a branch, open a PR, and merge once the Tests check is green. Jack's standing policy (Oct 9, 2026): auto-merge site changes when tests pass and tell him after.
- The thread "Build the Claude Rating into the site" owns site code (`index.html`, `scripts/`, `model/`) and keeps `docs/research.md` in step with `findings.md`. Other threads change those files only through that thread or with its agreement.
- Research lives outside the repo in `/mnt/project-files/research/claude-rating/` (`findings.md`, `ideas.md`, scripts, `production/`). Shared research helpers with on-disk caching are in `/mnt/project-files/research/hockeylib/`. Research runs start and end with `python3 -m hockeylib.check` (from `/mnt/project-files/research`), which catches out-of-order handoff rows, citations of missing findings sections, duplicate section numbers and stale RUNNING claims.

## Ideas ledger

`/mnt/project-files/research/claude-rating/ideas-ledger.md` lists every research idea tried or queued: ID, date, who ran it, what was tested, the measured result, the verdict (shipped with PR number, no change, pending, waiting until a date) and the source section.

- It is possible to find ways to boost performance rating predictions. A no-gain row records what one test measured, not a limit on the rating; never describe the rating as at a ceiling or impossible to improve.
- **Before testing an idea, check the ledger.** If a row covers it, only re-run it with a stated reason (new data, a new season, a different population or horizon, a bug in the old test).
- **Every research run appends its row(s) before finishing**, including "no change" and abandoned runs. Re-tests get a new row naming the old one.
- When the build thread ships or declines a handoff item, it updates that row's Verdict, and it copies the working ledger into `docs/ideas-ledger.md` in its PRs.

## handoff.md protocol

`/mnt/project-files/research/claude-rating/handoff.md` is how research asks for site changes. It is one table: `# | Recommendation | Backing | Status`.

- **Research** adds a row at the bottom with the next number: the change in plain words with any formula, the `findings.md` section(s) backing it, and Status `New`. Research doesn't edit Status after that, except to withdraw its own row.
- **The build thread** (owner of the repo) sets Status as it works: `Building`, `Built (PR #n)`, or `Declined: <one-line reason>`. Rows that need nothing built say `Nothing to do`.
- Anything needing Jack (a new secret, a paid API, anything outside the site) says `(needs Jack)` in the Recommendation, and the build thread asks him before building it.
- Never delete rows; re-read the file just before writing and keep edits small, since several sessions share it.
- After each batch of merges, the build thread runs a short sanity pass on that day's real `data/players.json` (and the page with it) and fixes what it finds before moving on: rows with no games left but a nonzero Week rating, games that already started still counted anywhere (columns, dots, badges, cards), cards offering more moves than remain, overnight-run dates and past nights, and Daily Faceoff lineups older than the team's last game. 8 of the fifth hunt's 22 items were bugs in code shipped in the previous two days.
