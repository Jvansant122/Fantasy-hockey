"""Pre-merge check for a batch of site changes (Jack, Nov 10: slow the pace slightly and catch bugs before they reach the page).

Run the "Update data" workflow on the PR branch next to one on main (same time, same feeds), then compare the two
players.json files:

    python scripts/premerge_diff.py branch_players.json main_players.json

It prints check_data's publish rules and warnings on the branch data, the biggest movers overall and on every team, and
REVIEW lines for the patterns behind past merge-wave bugs:
  - injured, long-term players lifted (Duchene, out after surgery, 0 -> 0.95 a game in #294),
  - a floor or cap the details claim that the number doesn't keep (McCarron's 0.65 floor shown 0.5 in #296),
  - a next-game chance jumping 0.3+ with no change in games left or status,
  - a rule newly firing on many players at once.
Exit code 1 when a publish rule fails or a REVIEW line prints, so a batch only merges after someone looked.
Standard library only."""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_data  # noqa: E402

LONG_STATUS = ("OUT", "INJURY_RESERVE", "SUSPENSION")
JUMP = 0.3          # next-game chance moved this much with nothing else changed
LIFT = 0.2          # an injured, long-term player's next-game chance rose this much
NEW_RULE_SHARE = 0.05  # a why key new on this share of rated players at once
TOP = 15


def _why(p):
    return p.get("cr_why") or {}


def long_term(p):
    """Out for weeks: an ESPN out / IR / suspension status with a long or season-ending note, or IR."""
    back = _why(p).get("back") or {}
    games = back.get("games")
    return p.get("injury") == "INJURY_RESERVE" or bool(back.get("season")) or (back.get("days") or 0) >= 14 \
        or (p.get("injury") in LONG_STATUS and games is not None and str(games).isdigit() and int(games) >= 10)


def claimed_bounds(p):
    """(floor, cap) on his next game that a why entry claims, from entries that carry them."""
    lo, hi = None, None
    for k, v in _why(p).items():
        if not isinstance(v, dict):
            continue
        if isinstance(v.get("floor"), (int, float)):
            lo = max(lo or 0.0, v["floor"])
        if isinstance(v.get("cap"), (int, float)):
            hi = min(hi if hi is not None else 1.0, v["cap"])
    return lo, hi


def review(new, base):
    """REVIEW lines for the branch data `new` against main's `base` (players.json dicts)."""
    out = []
    old = {p["id"]: p for p in base.get("players", [])}
    rated = [p for p in new.get("players", []) if p.get("cr") is not None]
    for p in rated:
        q, b = p.get("cr_dress_next"), old.get(p["id"])
        lo, hi = claimed_bounds(p)
        if q is not None and lo is not None and q < lo - 0.01 and (hi is None or hi >= lo):
            out.append(f"floor not kept: {p['name']} next game {q} under the {lo} his details claim")
        if q is not None and hi is not None and q > hi + 0.01:
            out.append(f"cap not kept: {p['name']} next game {q} over the {hi} his details claim")
        if not b or q is None or b.get("cr_dress_next") is None:
            continue
        d = q - b["cr_dress_next"]
        same = p.get("games_left") == b.get("games_left") and p.get("injury") == b.get("injury")
        if long_term(p) and d >= LIFT:
            out.append(f"long-term injured lifted: {p['name']} ({p.get('injury')}) next game {b['cr_dress_next']} -> {q}")
        elif same and abs(d) >= JUMP:
            out.append(f"jump: {p['name']} next game {b['cr_dress_next']} -> {q} with the same games left and status")
    before = Counter(k for p in base.get("players", []) for k in _why(p))
    after = Counter(k for p in rated for k in _why(p))
    for k, n in after.items():
        if n - before.get(k, 0) >= max(3, NEW_RULE_SHARE * len(rated)):
            names = [p["name"] for p in rated if k in _why(p) and k not in _why(old.get(p["id"]) or {})][:5]
            out.append(f"rule {k!r} now on {n} players (was {before.get(k, 0)}), new: {names}")
    return out


def movers(new, base):
    """[(|change in Week|, name, team, before, after)] sorted biggest first."""
    old = {p["id"]: p for p in base.get("players", [])}
    rows = []
    for p in new.get("players", []):
        b = old.get(p["id"])
        if b and p.get("cr") is not None and b.get("cr") is not None:
            rows.append((abs(p["cr"] - b["cr"]), p["name"], p.get("team"), b["cr"], p["cr"]))
    return sorted(rows, reverse=True)


def main(argv):
    new, base = (json.loads(Path(a).read_text()) for a in argv[1:3])
    bad = check_data.rules(new) + check_data.problems_vs_prev(new, base)
    print("publish rules:", bad or "clean")
    try:
        print("warnings:", check_data.warnings(new, base) or "none")
    except TypeError:
        print("warnings:", check_data.warnings(new) or "none")
    rows = movers(new, base)
    print(f"\nbiggest Week movers (of {len(rows)}):")
    for d, name, team, a, b in rows[:TOP]:
        print(f"  {name} ({team}) {a} -> {b}")
    print("\nbiggest mover on every team:")
    seen = set()
    for d, name, team, a, b in rows:
        if team not in seen and d >= 0.01:
            seen.add(team)
            print(f"  {team}: {name} {a} -> {b}")
    lines = review(new, base)
    print("\nREVIEW:" if lines else "\nREVIEW: nothing")
    for line in lines:
        print("  " + line)
    return 1 if bad or lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
