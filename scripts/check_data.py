"""Check data/players.json before the update workflow publishes it.

    python scripts/check_data.py                  # exit 1 if the page couldn't render this data
    python scripts/check_data.py --require-rated  # also exit 1 if the Claude Rating is missing

Standard library only, like the other daily scripts. tests/test_site_contract.py runs the same checks."""
import json
from datetime import date
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP_FIELDS = {"updated", "nights", "rated", "teams", "players", "goalie_cap", "move_limit", "ir_slots", "matchup_weeks", "final_matchup", "updated_date", "updated_utc", "first_start"}
NIGHT_FIELDS = {"sp", "date", "games", "light", "past"}
PLAYER_FIELDS = {"id", "name", "team", "pos", "slots", "owner", "team_id", "ir", "injury", "owned", "ppg", "cur_ppg", "last_ppg",
                 "games", "games_left", "light", "light_left", "nights", "dfo_out", "games_next2", "last_gp", "clears", "today_start"}
RATED_FIELDS = {"cr", "cr_fpg", "cr_games", "cr_dress", "cr_dress_next", "cr_matched", "cr_why", "cr_pct", "cr_season", "cr_season_pct", "sat_last"}
MIN_PLAYERS = 100  # rosters alone are ~200 players; far fewer means ESPN sent a partial answer


def problems(data):
    """Reasons the page would break or mislead with this data; empty when it's fine."""
    out = []
    missing = TOP_FIELDS - set(data)
    if missing:
        return [f"missing top-level fields: {sorted(missing)}"]
    if len(data["nights"]) != 7:
        out.append(f"{len(data['nights'])} nights, expected 7 (Monday-Sunday)")
    for n in data["nights"]:
        if NIGHT_FIELDS - set(n):
            out.append(f"night {n.get('date')} missing {sorted(NIGHT_FIELDS - set(n))}")
    # nights run Monday to Sunday on consecutive dates, and today isn't already marked past (findings 180)
    labels = [n.get("date", "") for n in data["nights"]]
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if all(l[:3] in days for l in labels) and [l[:3] for l in labels] != (days * 2)[:len(labels)]:
        out.append(f"nights don't run Monday to Sunday: {labels}")
    nums = [int(l.split()[-1]) for l in labels if l.split()[-1].isdigit()]
    if len(nums) == len(labels) and any(b != a + 1 and b != 1 for a, b in zip(nums, nums[1:])):
        out.append(f"night dates aren't consecutive: {labels}")
    today = data.get("updated_date")
    if today and len(labels) == 7 and all(l[:3] in days for l in labels):
        k = date.fromisoformat(today).weekday()  # today's night: not past, and every earlier night past
        if [bool(n.get("past")) for n in data["nights"]] != [i < k for i in range(7)]:
            out.append(f"past nights don't match today ({today}): {[(n.get('date'), n.get('past')) for n in data['nights']]}")
    players = data["players"]
    if len(players) < MIN_PLAYERS:
        out.append(f"only {len(players)} players")
    need = PLAYER_FIELDS | (RATED_FIELDS if data["rated"] else set())
    bad = [(p.get("name"), sorted(need - set(p))) for p in players if need - set(p)]
    if bad:
        out.append(f"{len(bad)} players missing fields, e.g. {bad[0]}")
    if data["rated"] and players and all(not p.get("cr") for p in players):
        out.append("rated is true but every Claude Rating is 0")
    return out


def main(argv):
    path = ROOT / "data" / "players.json"
    data = json.loads(path.read_text())
    errs = problems(data)
    if "--require-rated" in argv and not data.get("rated"):
        errs.append("published without the Claude Rating: the rating step failed (see the 'python scripts/fetch.py' step's log)")
    for e in errs:
        print(f"::error::{e}")
    if not errs:
        print(f"{path.name} OK: {len(data['players'])} players, rated={data['rated']}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
