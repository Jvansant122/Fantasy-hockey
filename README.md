# Fantasy hockey free agent finder

A one-page site that compares your ESPN fantasy hockey roster against free agents, using your league's scoring and each player's games in the current matchup.

## How it works

- `scripts/fetch.py` pulls your roster, free agents, waivers and the NHL schedule from ESPN and writes `data/players.json`.
- `.github/workflows/update.yml` runs it every morning (or on demand from the Actions tab) and commits the new data.
- `index.html` reads that file. GitHub Pages serves it.

Fantasy points use ESPN's own calculation with your league's scoring settings. Weekly projection is projected points per game times games this matchup. Points per game blends this season's average with ESPN's season projection.

## Setup

1. **Secrets** (Settings → Secrets and variables → Actions):
   - `LEAGUE_ID`: the number after `leagueId=` in your league URL.
   - `ESPN_S2` and `SWID`: cookies from fantasy.espn.com while signed in (needed for a private league; SWID is also how the script finds your team).
   - `TEAM_ID` (optional): your team's number, if the script can't find your team.
2. **Pages** (Settings → Pages): deploy from branch `main`, folder `/ (root)`.
3. Run **Update data** once from the Actions tab.

Note: on a free GitHub plan the Pages site is public even though the repo is private. The cookies stay in encrypted secrets and never appear on the site; the site shows player names, stats and your roster.
