# Fantasy hockey free agent finder

A one-page site that compares your ESPN fantasy hockey roster against free agents, using your league's scoring and each player's games in the current matchup.

Live site: https://jvansant122.github.io/Fantasy-hockey/. The research behind the Claude Rating, in plain language: [docs/research.md](docs/research.md). What each part of the rating does and how much it matters: [docs/rating-components.md](docs/rating-components.md). Every idea tested so far, and what came of it: [docs/ideas-ledger.md](docs/ideas-ledger.md).

## How it works

- `scripts/fetch.py` pulls your roster, free agents, waivers and the NHL schedule from ESPN and writes `data/players.json`.
- `.github/workflows/update.yml` runs it four times a day (or on demand from the Actions tab) and commits the new data.
- `scripts/rating.py` adds the **Claude Rating**: projected points for the rest of the matchup, from NHL stats API game logs and MoneyPuck xG. Skaters: a ridge model's points per game × games left × chance to dress in each game (lineup history, a lineup model trained on 16 seasons, team form, Daily Faceoff injury and roster news, and ESPN injury status). Goalies: expected starts from a start model (with Daily Faceoff confirmations and tandem-pattern rules) × points per start (his own rate pulled toward the 2.9 league average, with tonight's betting line). A Season rating projects the rest of the year for keep-or-drop calls (with Daily Faceoff news, ESPN's preseason projection and two more boosted models trained on 16 seasons). If the NHL or MoneyPuck calls fail, the update still publishes without the rating. Piece by piece: [docs/rating-components.md](docs/rating-components.md).
- `model/rating_model.json` holds the model weights; `model/train.py` refits them from the research data (2021-26 seasons).
- `scripts/season_boost.py` runs the three boosted models (Season dress share, Season points per game, and the Week's chance to dress) from `model/season_boost_*.json` in plain Python; `model/train_boost.py` refits them offline from 16 seasons of research data.
- `scripts/winners.py` builds the **Predicted winners** tab: each NHL game's win chance this week from the expected lineups' points per game, the likely starter's save%, goal differential, home ice and back-to-backs (picks are locked at puck drop), and each fantasy matchup's win chance from ESPN's points so far plus the Week ratings.
- `scripts/leads.py` reads Daily Faceoff's starting goalies (confirmed starters count as certain starts) and Daily Faceoff's injury and call-up/send-down news (saved in `data/dfo_news.json`), which moves the Week and Season ratings. On every run it also logs Daily Faceoff starters and lineups, ESPN injury notes, NHL/DraftKings odds and a rating snapshot to `data/log/` for testing later. The workflow runs at about 6 AM, 9 AM, 12:40 PM and 5 PM ET.
- `data/news.json` (optional, not used yet) is where up to 25 news flags can go later. A flag shows in the row's details, and only changes the numbers (games, starts, or a capped points-per-game multiplier) when its `apply` is true.
- `index.html` reads that file. GitHub Pages serves it.

Fantasy points use ESPN's own calculation with your league's scoring settings. ESPN's own season projection is shown next to the Claude Rating for comparison.

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
python -m pytest -n auto
```

`-n auto` runs the tests on every CPU, as the Tests workflow does (about 1 minute instead of 3); plain `python -m pytest` works too.

The **Tests** workflow runs these on every pull request. They need no network or secrets: ESPN, NHL, MoneyPuck and Daily Faceoff are faked. They run the rating end to end on a small made-up league, check that `data/players.json` still has every field the page reads, load the page in a browser with the real data, and test the Predicted winners model, Daily Faceoff parsing and the model files. Contributor notes (layout, conventions, how research hands changes to the site) are in [CLAUDE.md](CLAUDE.md).

Note: the repo and the Pages site are public: code, docs, `data/players.json` and the run logs in `data/log` (league transactions) are visible to anyone. The cookies stay in encrypted secrets and never appear in either.
