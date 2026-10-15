"""Handoff 668 (findings 464, ideas 1559 / 1566; hunt14/fpg/t6_toifc.py): boosted forecasts of next week's TOI/G and PP TOI/G
for the Season FP/G ridge, exported to model/season_boost_toi.json and model/season_boost_pptoi.json, which
scripts/rating.py walks with season_boost.predict (Season path only; the Week ridge keeps its linear forecasts).

Inputs: the Week ridge's design (model/train.py prep with WEEK_FEATS, but with the Season ridge's narrower forecast
ridges, fit on the same rows as rating_model.json) without the hinge (h_*) and pair (a*b) columns and constant columns,
plus team_games (his team's games that Monday-Sunday week). Rows 2019-20 on that played next week, weight = next week's games. Offline only (pandas,
scikit-learn, the research data in /mnt/project-files); the daily run only reads the JSON.

    python model/train_toi_boost.py [research dir]

Set TMPDIR to a folder holding train.py's snapshot cache (rating_snapshots_2017_dec.parquet) to skip rebuilding it.
"""
import json
import os
import sys

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import train as T  # noqa: E402  (reads the research folder from sys.argv[1] like train.py)
from train_boost import export  # noqa: E402

SET = dict(max_iter=400, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=100, l2_regularization=1.0, random_state=0)
TARGETS = {"toi": "y_toi", "pptoi": "y_pptoi"}


def design(df):
    """Training rows, their boosted-model inputs and the fitted state (forecast ridges as rating_model.json's)."""
    mu = T.ux_means(df)
    tr = df[df.y_gp > 0]
    st = T.fit_state(tr, T.WEEK_FEATS, mu, wide_fc=False)  # the narrower forecast ridges, as the Season ridge's (handoff 669)
    X = T.prep(tr, T.WEEK_FEATS, mu, st)
    keep = [c for c in X if not c.startswith("h_") and "*" not in c and X[c].nunique() > 1]
    X = X[keep].astype(float).copy()
    X["team_games"] = tr.team_games.astype(float).values
    return tr, X, mu


def check_daily_arithmetic(df, models, n=2000):
    """scripts/rating.py rebuilds these inputs from its own feature dict: its walk of the exported trees must match
    sklearn's predictions on training rows."""
    sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
    import pandas as pd
    import rating
    import season_boost
    rows = df[df.y_gp > 0].sample(n, random_state=0)
    m = rating.Model()
    base = T.prep(rows, T.UX_ERA_FEATS + T.P2_FEATS, T.ux_means(df))
    tr, X, _ = design(df)
    gap = {k: 0.0 for k in models}
    for (i, r), (_, b) in zip(rows.iterrows(), base.iterrows()):
        f = {k: (None if pd.isna(v) else float(v)) for k, v in r.items() if isinstance(v, (int, float, np.number))}
        f |= {k: (None if pd.isna(v) else float(v)) for k, v in b.items()}
        x = {**f, **rating.forecast_features(f, m.sk, season=True)}
        for k, (sk_model, js) in models.items():
            want = float(sk_model.predict(X.loc[[i]].values)[0])
            gap[k] = max(gap[k], abs(season_boost.predict(js, x) - want))
    print("daily arithmetic check (boosted ice time): largest gap", {k: f"{v:.2e}" for k, v in gap.items()})
    assert max(gap.values()) < 0.05, "scripts/rating.py and model/train_toi_boost.py disagree on the boosted inputs"


def main():
    df = T.snapshots()
    df = df[(df.std_gp >= 1) & (df.season >= T.TRAIN_FROM)].copy()
    tr, X, _ = design(df)
    w = tr.y_gp.values.astype(float)
    models = {}
    for name, y in TARGETS.items():
        m = HistGradientBoostingRegressor(**SET).fit(X.values, tr[y].values, sample_weight=w)
        js = export(m, list(X.columns))
        with open(os.path.join(HERE, f"season_boost_{name}.json"), "w") as fh:
            json.dump(js, fh, separators=(",", ":"), allow_nan=False)
        models[name] = (m, js)
        print(name, len(X), "rows", X.shape[1], "inputs, trees", js["n_trees"], "nodes", sum(len(t[0]) for t in js["trees"]), flush=True)
    check_daily_arithmetic(df, models)


if __name__ == "__main__":
    main()
