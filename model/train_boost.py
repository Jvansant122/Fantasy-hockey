"""Handoff 332 (findings 360 (3)-(4)): train the two boosted models for the Season rating on 16 seasons of game logs
(the research's hunt11b compact panel, 2010-11 .. 2025-26, plus MoneyPuck last-season rates) and export them to
model/season_boost_*.json, which scripts/season_boost.py evaluates in plain Python. Offline only: needs pandas,
scikit-learn and the research data in /mnt/project-files; the daily run only reads the JSON.

Rebuild the panel first with the research scripts (hunt11b/compact.py, mp_prev.py, fetch_bios.py), then:
    python model/train_boost.py [out_dir] [--pickle]

  dress: rest-of-season dress share (x22 "boost (no news)"), seasons >= 2011-12, target y, weight tleft, features FD.
  fpg:   rest-of-season FP per game played (x19 "components + MoneyPuck"), rows with y_gp >= 1, target y_fp / y_gp, weight y_gp,
         features F + MP, compact merged with mp_prev on playerId + season.

JSON layout: {"features": [...], "baseline": b, "layout": ..., "trees": [[f, t, l, r, m], ...]} with one entry per node in each list:
  f = feature index (-1 = leaf), t = threshold (leaf: leaf value; null on an internal node = +inf),
  l / r = child node indices, m = 1 if NaN goes left.  Go left when x <= t; NaN goes left iff m.
  prediction = baseline + sum over trees of the leaf value reached (leaf values already include the learning rate).
"""
import json, math, pickle, sys, time
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

H = "/mnt/project-files/research/claude-rating/hunt11b/data"
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
OUT = ARGS[0] if ARGS else str(__import__("pathlib").Path(__file__).resolve().parent)

FD = ["tg", "tleft", "gp", "out", "d3", "d10", "toi", "pptoi", "fpg", "l5toi", "last_toi", "n_abs", "n_long", "max_abs", "isD", "exp", "prev_gp", "prev_fpg", "prev_toi", "p2_gp", "dress_rate", "age", "nhl_yrs", "draft", "out_above", "toi_rank", "n_reg"]
K = ["goals", "assists", "shots", "hits", "blockedShots", "ppPoints"]
MP = ["mp_ixg60", "mp_hdxg60", "mp_oxg", "mp_gs", "mp_a160", "mp_hits60", "mp_take60", "mp_oz", "mp_toi_mp", "mp_ppxg60", "mp_pptoi"]
F = ["tg", "tleft", "gp", "out", "d10", "toi", "pptoi", "fpg", "l5toi", "l10fpg", "isD", "prev_gp", "prev_fpg", "prev_toi", "prev_pptoi", "p2_gp", "p2_fpg", "age", "nhl_yrs", "draft", "toi_rank"] + [f"{p}_{k}" for p in "cp" for k in K]
FF = F + MP
SET = dict(max_iter=400, learning_rate=0.04, max_leaf_nodes=31, min_samples_leaf=300, l2_regularization=1.0, early_stopping=False, random_state=0)


def sig7(v):
    return float(f"{v:.7g}")


def export(m, feats):
    assert m.n_trees_per_iteration_ == 1 and len(m._predictors) == SET["max_iter"]
    trees = []
    for (p,) in m._predictors:
        n = p.nodes
        assert not n["is_categorical"].any()
        f, t, l, r, ms = [], [], [], [], []
        for x in n:
            if x["is_leaf"]:
                f.append(-1); t.append(sig7(x["value"])); l.append(0); r.append(0); ms.append(0)
            else:
                th = float(x["num_threshold"])
                f.append(int(x["feature_idx"])); t.append(None if math.isinf(th) else th)
                assert not (math.isinf(th) and th < 0)
                l.append(int(x["left"])); r.append(int(x["right"])); ms.append(int(x["missing_go_to_left"]))
        trees.append([f, t, l, r, ms])
    base = np.asarray(m._baseline_prediction).ravel()
    return {"features": feats, "baseline": float(base[0]), "n_trees": len(trees),
            "layout": "tree=[f,t,l,r,m]; f=-1 leaf with value t; else go l if x<=t (t null=+inf), NaN goes l iff m; pred=baseline+sum(leaf)",
            "trees": trees}


def main():
    c = pd.read_parquet(f"{H}/compact.parquet")
    # dress (x22): exp clipped at 5, seasons 2011-12 on
    cd = c.copy(); cd["exp"] = cd.exp.clip(upper=5); cd = cd[cd.season >= 20112012]
    t0 = time.time()
    md = HistGradientBoostingRegressor(**SET).fit(cd[FD].astype(float).values, cd.y.values, sample_weight=cd.tleft.values)
    td = time.time() - t0
    # FP/G (x19): compact + MoneyPuck last season, rows with y_gp >= 1 (x19 does not clip exp; exp is not in F)
    cf = c.merge(pd.read_parquet(f"{H}/mp_prev.parquet"), on=["playerId", "season"], how="left"); cf = cf[cf.y_gp >= 1]
    t0 = time.time()
    mf = HistGradientBoostingRegressor(**SET).fit(cf[FF].astype(float).values, (cf.y_fp / cf.y_gp).values, sample_weight=cf.y_gp.values)
    tf = time.time() - t0
    print(f"dress: {len(cd)} rows, {td:.1f}s | fpg: {len(cf)} rows, {tf:.1f}s")
    for name, m, feats in [("dress", md, FD), ("fpg", mf, FF)]:
        js = export(m, feats)
        with open(f"{OUT}/season_boost_{name}.json", "w") as fh:
            json.dump(js, fh, separators=(",", ":"), allow_nan=False)
        if "--pickle" in sys.argv:  # sklearn copies, only for checking the JSON against .predict
            with open(f"{OUT}/model_{name}.pkl", "wb") as fh:
                pickle.dump(m, fh)
        print(name, "baseline", js["baseline"], "nodes", sum(len(t[0]) for t in js["trees"]))


if __name__ == "__main__":
    main()
