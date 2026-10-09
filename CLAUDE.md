# Fantasy hockey free agent finder

A one-page site (GitHub Pages) that ranks free agents in Jack's ESPN fantasy hockey league by the **Claude Rating**: projected fantasy points for the rest of the current Monday-Sunday matchup. Live at https://jvansant122.github.io/Fantasy-hockey/.

## Layout

- `index.html` - the whole site: one page, inline CSS and JS, reads `data/players.json`.
- `scripts/fetch.py` - ESPN league, rosters, free agents and schedule -> `data/players.json`. Calls `rating.add_ratings` and `leads`.
- `scripts/rating.py` - the Claude Rating (NHL stats API + MoneyPuck). Weights in `model/rating_model.json`.
- `scripts/leads.py` - Daily Faceoff starting goalies (used by the rating) and the day-of log in `data/log/`.
- `model/train.py` - refits `model/rating_model.json` from the research data in `/mnt/project-files/research/claude-rating` (needs pandas, scikit-learn, pyarrow).
- `data/` - written by the "Update data" workflow three times a day (9 AM, 12:30 PM, 5 PM ET). Don't hand-edit; merge `main` into your branch before pushing because the bot commits here often.
- `docs/research.md` - plain-language research write-up for league members (charts in `docs/img/`).
- `docs/ideas-ledger.md` - every research idea tried or queued, with its result and verdict. Mirror of `/mnt/project-files/research/claude-rating/ideas-ledger.md`.
- `tests/` - pytest suite, run by the "Tests" workflow on every PR. `tests/test_page.py` loads the page in Chromium (Playwright) with the real data, picks every team, filters, sorts, opens details, and also loads it with ratings stripped; any JS error fails the build.
- `scripts/check_data.py` - the update workflow runs it before committing data (a broken `players.json` is never published) and after (fails the run, so GitHub emails Jack, when the Claude Rating is missing). Add a field to its lists when the page starts depending on one.
- `.claude/hooks/session-start.sh` - cloud sessions install the test and research libraries on start.

## Run the tests

```
pip install -r requirements-dev.txt
python -m pytest
```

No network or secrets needed: every ESPN, NHL, MoneyPuck and Daily Faceoff call is faked.

- `tests/test_add_ratings.py` runs `add_ratings` end to end on a tiny made-up league. `fetch.py` catches rating errors so the site still publishes without ratings, so this test is the only thing that catches a broken rating before merge.
- `tests/test_site_contract.py` checks `data/players.json` has every field `index.html` reads, and that the page's script parses (`node --check`). If you add a `p.<field>` to the page, add it to the data too; if you rename a field in `fetch.py` or `rating.py`, update the page.
- Tests check invariants (fields present, probabilities in range, long-term injured players at 0, confirmed starters honoured), not exact rating values, so a deliberate rating change shouldn't need test edits unless it changes those rules.

## Conventions

- The daily scripts use only the standard library plus `requests` (the workflow installs nothing else). Heavy libraries belong in `model/train.py` or research code.
- League: 12-team H2H points, daily lineups, 6 acquisitions per matchup. Scoring: G 2, A 1, PPP 0.5, SHP 0.5, SOG 0.1, HIT 0.1, BLK 0.5; goalie W 2, L -1, OTL +1, GA -1, SV 0.2, SO 3. Lineup 9F/5D/1UTL/2G; UTL counts as a sixth D slot.
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

- **Before testing an idea, check the ledger.** If a row covers it, only re-run it with a stated reason (new data, a new season, a different population or horizon, a bug in the old test).
- **Every research run appends its row(s) before finishing**, including "no change" and abandoned runs. Re-tests get a new row naming the old one.
- When the build thread ships or declines a handoff item, it updates that row's Verdict, and it copies the working ledger into `docs/ideas-ledger.md` in its PRs.

## handoff.md protocol

`/mnt/project-files/research/claude-rating/handoff.md` is how research asks for site changes. It is one table: `# | Recommendation | Backing | Status`.

- **Research** adds a row at the bottom with the next number: the change in plain words with any formula, the `findings.md` section(s) backing it, and Status `New`. Research doesn't edit Status after that, except to withdraw its own row.
- **The build thread** (owner of the repo) sets Status as it works: `Building`, `Built (PR #n)`, or `Declined: <one-line reason>`. Rows that need nothing built say `Nothing to do`.
- Anything needing Jack (a new secret, a paid API, anything outside the site) says `(needs Jack)` in the Recommendation, and the build thread asks him before building it.
- Never delete rows; re-read the file just before writing and keep edits small, since several sessions share it.
