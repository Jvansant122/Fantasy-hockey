"""Boosted Season models trained on 16 seasons of game logs (findings 360 (3)-(4), handoff 332).

model/train_boost.py fits two gradient-boosted models offline and exports their trees to model/season_boost_*.json:
  dress: share of his team's remaining games a skater plays; its logit corrects the Season dress logit.
  fpg:   his rest-of-season fantasy points per game played; it corrects the Season FP/G.
This module rebuilds their inputs from the daily run's NHL data exactly as the research panel did
(hunt11b/compact.py, mp_prev.py) and walks the trees in plain Python, so the daily scripts stay standard library.
"""
import json
import math
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parent.parent / "model"
STATS = ("goals", "assists", "shots", "hits", "blockedShots", "ppPoints")
MIN_LEFT = 5  # the panel kept only rows with 5+ team games left


def load(name):
    """An exported model, or None when the file is missing or unreadable (the Season rating then goes without it)."""
    try:
        return json.loads((MODEL_DIR / f"season_boost_{name}.json").read_text())
    except (OSError, ValueError):
        return None


def predict(model, features):
    """sklearn HistGradientBoostingRegressor.predict for one row: go left when x <= threshold (null = +inf); a missing,
    None or NaN input goes to the node's missing side; baseline + the leaf values reached."""
    x = []
    for name in model["features"]:
        v = features.get(name)
        x.append(math.nan if v is None else float(v))
    total = model["baseline"]
    for f, t, left, right, miss in model["trees"]:
        i = 0
        while f[i] >= 0:
            v = x[f[i]]
            if v != v:
                i = left[i] if miss[i] else right[i]
            else:
                i = left[i] if t[i] is None or v <= t[i] else right[i]
        total += t[i]
    return total


def mean(vals):
    vals = list(vals)
    return sum(vals) / len(vals) if vals else None


def mp_rates(r):
    """MoneyPuck last-season rates (mp_prev.py) from rating.load_xg's row; none under 5 games."""
    if not r or (r.get("gp") or 0) < 5:
        return {}
    gp, t60 = r["gp"], (r.get("icetime") or 0) / 3600
    per60 = lambda v: v / t60 if t60 > 0 else None
    zs = (r.get("oz") or 0) + (r.get("dz") or 0)
    pp60 = (r.get("pp_icetime") or 0) / 3600
    return {"mp_ixg60": per60(r["ixg"] * gp), "mp_hdxg60": per60(r.get("hdxg") or 0), "mp_oxg": r.get("oxg_pct"),
            "mp_gs": (r.get("gamescore") or 0) / gp, "mp_a160": per60(r.get("a1") or 0), "mp_hits60": per60(r.get("hits") or 0),
            "mp_take60": per60(r.get("takeaways") or 0), "mp_oz": (r.get("oz") or 0) / zs if zs else None,
            "mp_toi_mp": (r.get("icetime") or 0) / gp / 60,
            "mp_ppxg60": (r.get("pp_ixg") or 0) / pp60 if pp60 > 0 else None,
            "mp_pptoi": (r.get("pp_icetime") or 0) / gp / 60 if "pp_icetime" in r else None}


def features(by_player, team_games, prev, prev2, bios, xg_prev, season, today, season_games):
    """{NHL id: inputs} for every skater with a game this season whose team has MIN_LEFT+ games left, as compact.py built
    them for a Monday: this season's games so far, his team = the team of his last game. prev / prev2: last season's and
    the season before's per-game rates (load_prev_season, not the stand-in for a missed season); bios: {id: (birth date,
    draft overall, first NHL season)}; xg_prev: last season's MoneyPuck rows (load_xg)."""
    start_year = season // 10000
    rows = {}
    for pid, gl in by_player.items():
        if not gl:
            continue
        gl = sorted(gl, key=lambda g: (g["date"], g["game"]))
        last = gl[-1]
        team = last["team"]
        order = {gid: i for i, gid in enumerate(team_games.get(team, []))}
        tg = len(order)
        if tg == 0 or last["game"] not in order or season_games - tg < MIN_LEFT:
            continue
        same = sorted(order[g["game"]] for g in gl if g["team"] == team and g["game"] in order)
        gaps = [0] + [b - a - 1 for a, b in zip(same, same[1:])]
        rel = [tg - i for i in same]
        p, p2 = prev.get(pid) or {}, prev2.get(pid) or {}
        birth, draft, first = bios.get(pid, (None, None, None))
        nhl_yrs = start_year - first // 10000 if first else None
        x = {"tg": tg, "tleft": season_games - tg, "gp": len(gl), "out": tg - 1 - order[last["game"]],
             "d3": sum(r <= 3 for r in rel), "d10": sum(r <= 10 for r in rel) / max(min(tg, 10), 1),
             "toi": mean(g["toi"] for g in gl), "pptoi": mean(g.get("pptoi", 0) for g in gl), "fpg": mean(g["fp"] for g in gl),
             "l5toi": mean(g["toi"] for g in gl[-5:]), "l10fpg": mean(g["fp"] for g in gl[-10:]), "last_toi": last["toi"],
             "n_abs": sum(g >= 1 for g in gaps), "n_long": sum(g >= 6 for g in gaps), "max_abs": max(gaps),
             "isD": 1.0 if last.get("pos") == "D" else 0.0,
             # seasons since his first in the game logs, capped at 5 for the dress model: his NHL seasons stand in
             "exp": min(nhl_yrs, 5) if nhl_yrs is not None else (1 if p else 0),
             "prev_gp": p.get("gp"), "prev_fpg": p.get("fp"), "prev_toi": p.get("toi"), "prev_pptoi": p.get("pptoi"),
             "p2_gp": p2.get("gp"), "p2_fpg": p2.get("fp"), "dress_rate": len(gl) / max(tg, 1),
             "age": (today - birth).days / 365.25 if birth else None, "nhl_yrs": nhl_yrs,
             "draft": draft if draft else 250, "team": team,
             **{f"c_{k}": mean(g.get(k, 0) for g in gl) for k in STATS},
             **{f"p_{k}": p.get(k) for k in STATS},
             **mp_rates(xg_prev.get(pid))}
        rows[pid] = x
    # lineup crunch within team x position (D or F): regulars out now with more ice time than him, his ice-time rank
    # among those in the lineup (average ranks for ties), and the number of regulars (compact.py)
    groups = {}
    for pid, x in rows.items():
        groups.setdefault((x["team"], x["isD"]), []).append(pid)
    for pids in groups.values():
        regs = [q for q in pids if rows[q]["dress_rate"] >= 0.5]
        reg_out = [rows[q]["toi"] for q in regs if rows[q]["out"] >= 1]
        inl = [q for q in pids if rows[q]["out"] == 0]
        for pid in pids:
            x = rows[pid]
            x["out_above"] = sum(t > x["toi"] for t in reg_out)
            x["n_reg"] = len(regs)
            x["toi_rank"] = (sum(rows[q]["toi"] > x["toi"] for q in inl) + (sum(rows[q]["toi"] == x["toi"] for q in inl) + 1) / 2
                             if x["out"] == 0 else None)
    return rows


NEWS_FLAGS = ("season", "surgery", "ir", "ltir", "wtw", "dtd", "ret", "out", "practice", "gtd", "illness", "concussion")


def news_inputs(item, last_game, now_utc):
    """The Week model's injury-news inputs (handoff 413, model/train_boost.py news_inputs): his latest Daily Faceoff injury
    item (leads.fetch_news, with its hunt11b/news.py flags nf and stated timeline tl) counts when posted after 03:00 UTC on
    his last game's date; inj_age is days since it, inj_rem the timeline left (0-250)."""
    after = bool(item and last_game and item["ts"] > f"{last_game[:10]}T03:00")
    out = {f"inj_{k}": float(after and k in (item.get("nf") or ())) for k in NEWS_FLAGS}
    out["inj_after"] = float(after)
    out["inj_age"] = out["inj_rem"] = None
    if after:
        from datetime import datetime
        age = (now_utc.replace(tzinfo=None) - datetime.fromisoformat(item["ts"][:19])).total_seconds() / 86400
        out["inj_age"] = age
        if item.get("tl") is not None:
            out["inj_rem"] = min(max(item["tl"] - age, 0.0), 250.0)
    return out
