# Fantasy hockey free agent finder

A one-page site that compares your ESPN fantasy hockey roster against free agents, using your league's scoring and each player's games in the current matchup.

Live site: https://jvansant122.github.io/Fantasy-hockey/. The research behind the Claude Rating, in plain language: [docs/research.md](docs/research.md).

## How it works

- `scripts/fetch.py` pulls your roster, free agents, waivers and the NHL schedule from ESPN and writes `data/players.json`.
- `.github/workflows/update.yml` runs it every morning (or on demand from the Actions tab) and commits the new data.
- `scripts/rating.py` adds the **Claude Rating**: projected points for the rest of the matchup, from NHL stats API game logs and MoneyPuck xG. Skaters: projected points per game × games left × chance to dress (recent games played plus ESPN injury status). Goalies: expected starts × his own points per start this season, shrunk toward the league average (2.9) over 15 starts. If the NHL or MoneyPuck calls fail, the update still publishes without the rating.
- `model/rating_model.json` holds the model weights; `model/train.py` refits them from the research data (2021-26 seasons).
- `scripts/leads.py` reads Daily Faceoff's starting goalies (confirmed starters count as certain starts) and, on every run, logs Daily Faceoff starters and lineups, ESPN injury notes, NHL/DraftKings odds and a rating snapshot to `data/log/` for testing later. The workflow runs at 9 AM, 12:30 PM and 5 PM ET.
- `data/news.json` (optional, not used yet) is where up to 25 news flags can go later. A flag shows in the row's details, and only changes the numbers (games, starts, or a capped points-per-game multiplier) when its `apply` is true.
- `index.html` reads that file. GitHub Pages serves it.

Fantasy points use ESPN's own calculation with your league's scoring settings. Weekly projection is projected points per game times games this matchup. Points per game blends this season's average with ESPN's season projection.

## Setup

1. **Secrets** (Settings → Secrets and variables → Actions):
   - `LEAGUE_ID`: the number after `leagueId=` in your league URL.
   - `ESPN_S2` and `SWID`: cookies from fantasy.espn.com while signed in (needed for a private league; SWID is also how the script finds your team).
   - `TEAM_ID` (optional): your team's number, if the script can't find your team.
2. **Pages** (Settings → Pages): deploy from branch `main`, folder `/ (root)`.
3. Run **Update data** once from the Actions tab.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest
```

The **Tests** workflow runs these on every pull request. They need no network or secrets: ESPN, NHL, MoneyPuck and Daily Faceoff are faked. They run the rating end to end on a small made-up league and check that `data/players.json` still has every field the page reads. Contributor notes (layout, conventions, how research hands changes to the site) are in [CLAUDE.md](CLAUDE.md).

Note: on a free GitHub plan the Pages site is public even though the repo is private. The cookies stay in encrypted secrets and never appear on the site; the site shows player names, stats and your roster.
