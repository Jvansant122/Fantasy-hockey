"""Daily Faceoff's next-game floors survive the Week lineup step (week_slack), handoff 720."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import rating  # noqa: E402


def _row(pid, q, floor0=None, regular=False, j0=0):
    return {"p": {"name": str(pid), "cr_fpg": 1.0, "cr_why": {}}, "pid": pid, "team": "ANA", "isD": False,
            "dates": ["2026-10-16"], "per_game": [q], "before": [q], "caps": [1.0], "missed": 0 if regular else 1,
            "after_trip": [False], "j0": j0, "others": [], "regular": regular, "floor0": floor0}


def _run(extra):
    rows = [_row(i, 0.97, regular=True) for i in range(12)] + extra
    rating.week_slack(rows, {}, None, None)
    return [r["p"]["cr_dress_next"] for r in rows]


def test_in_lines_scratch_keeps_the_floor_after_the_lineup_step():
    out = _run([_row(99, rating.DFO_LINEUP_NEXT, floor0=rating.DFO_LINEUP_NEXT)])
    assert out[12] >= rating.DFO_LINEUP_NEXT
    # without the floor the same row is published under it (the bug)
    assert _run([_row(99, rating.DFO_LINEUP_NEXT)])[12] < rating.DFO_LINEUP_NEXT


def test_floor_respects_the_cap_and_short_groups():
    r = _row(99, 0.5, floor0=rating.DFO_LINEUP_NEXT)
    r["caps"] = [0.6]
    assert rating.lineup_floor(r, [0.5])[0] == 0.6
    assert rating.lineup_floor(_row(99, 0.5, floor0=0.9, j0=1), [0.5]) == [0.5]  # not the team's next game
    assert rating.lineup_floor(_row(99, 0.0, floor0=0.9), [0.0]) == [0.0]  # no chance stays no chance
    rows = [_row(i, 0.97, regular=True) for i in range(5)] + [_row(99, 0.7, floor0=0.9)]  # fewer than 12: no slack
    rating.week_slack(rows, {}, None, None)
    assert rows[-1]["p"]["cr_dress_next"] >= 0.9
