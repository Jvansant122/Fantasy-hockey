"""Fit the Claude Rating models and write model/rating_model.json for scripts/rating.py.

Uses the research data (NHL stats API game logs plus MoneyPuck xG, 2021-22 to 2025-26) from the project's
research folder: pass its path as the first argument (default /mnt/project-files/research/claude-rating).

Only inputs the daily workflow can rebuild cheaply are used: game-level NHL stats for this season
(last 5/10/20 games and season to date), season totals for last season, and MoneyPuck season xG.
"""
import json
import os
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, RidgeCV
from sklearn.preprocessing import StandardScaler

RESEARCH = sys.argv[1] if len(sys.argv) > 1 else "/mnt/project-files/research/claude-rating"
sys.path.insert(0, RESEARCH)
import common  # noqa: E402
from goalies import START_FEATS, start_table  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rating_model.json")
CACHE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "rating_snapshots.parquet")
GAME_RATES = ["fp", "toi", "pptoi", "shots", "iCF", "hits", "blockedShots", "goals", "assists", "ppPoints", "points"]
XG_RATES = ["ixg", "onice_xgf"]
COMPONENTS = common.COMPONENTS
TEST_SEASONS = [20232024, 20242025, 20252026]


def feature_names():
    f = [f"{w}_{r}" for w in ("l5", "l10", "l20", "std", "prev") for r in GAME_RATES]
    f += [f"{w}_{r}" for w in ("std", "prev") for r in XG_RATES]
    f += ["std_gp", "prev_gp", "is_D", "no_prev", "dressed3", "dressed10"]
    f += [f"std_{r}_w" for r in ("fp", "toi", "pptoi", "shots", "blockedShots", "hits")]
    return f


FEATS = feature_names()


def snapshots():
    if os.path.exists(CACHE):
        return pd.read_parquet(CACHE)
    sk = common.load_skaters()
    sk["iCF"] = sk["totalShotAttempts"]  # live data uses NHL shot attempts, so train on the same
    df = common.skater_snapshots(sk)
    df.to_parquet(CACHE)
    return df


def prep(df):
    X = pd.DataFrame(index=df.index)
    for c in FEATS:
        if c in df:
            X[c] = df[c]
    X["prev_gp"] = df.prev_gp.fillna(0)
    X["is_D"] = (df.pos == "D").astype(float)
    X["no_prev"] = df.prev_gp.isna().astype(float)
    w = np.clip(df.std_gp / 20.0, 0, 1)
    for r in ("fp", "toi", "pptoi", "shots", "blockedShots", "hits"):
        X[f"std_{r}_w"] = df[f"std_{r}"] * w
    return X[FEATS]


def fit_linear(train):
    """One ridge per scoring stat, summed with league weights and folded back to raw-unit coefficients."""
    tr = train[train.y_gp > 0]
    X = prep(tr)
    means = X.mean().fillna(0)
    X = X.fillna(means)
    sc = StandardScaler().fit(X)
    Z = sc.transform(X)
    coef, icpt = np.zeros(len(FEATS)), 0.0
    for c, wt in COMPONENTS.items():
        m = RidgeCV(alphas=np.logspace(-1, 3, 9)).fit(Z, tr[f"y_{c}"], sample_weight=tr.y_gp)
        coef += wt * m.coef_ / sc.scale_
        icpt += wt * (m.intercept_ - (m.coef_ * sc.mean_ / sc.scale_).sum())
    return coef, icpt, means


def predict(df, coef, icpt, means):
    return prep(df).fillna(means).values @ coef + icpt


def dress_table(train):
    t = train.assign(d3=(train.dressed3.fillna(0).clip(0, 1) * 3).round().astype(int),
                     d10=pd.cut(train.dressed10.fillna(0).clip(0, 1), [-0.01, 0.5, 0.8, 1.0], labels=False).astype(int))
    g = t.groupby(["d3", "d10"])
    rate = (g.y_gp.sum() / g.team_games.sum())[g.team_games.sum() >= 500]  # drop near-empty cells
    return {f"{a}_{b}": round(float(v), 3) for (a, b), v in rate.items()}


def rho_by_week(d, col):
    vals = [g[col].rank().corr(g.y_fp.rank()) for _, g in d.groupby("monday") if len(g) >= 20]
    return float(np.nanmean(vals))


def main():
    df = snapshots()
    df = df[df.std_gp >= 1].copy()
    # check the reduced feature set still beats shrunk FP/G on wire players, rolling origin
    for s in TEST_SEASONS:
        tr, te = df[df.season < s], df[(df.season == s) & df.wire & (df.y_gp > 0) & (df.std_gp >= 3)].copy()
        coef, icpt, means = fit_linear(tr)
        te["ridge"] = predict(te, coef, icpt, means)
        repl = tr[tr.wire].groupby("pos").std_fp.mean()
        prior = te.prev_fp.fillna(te.pos.map(repl))
        te["shrunk"] = (te.std_fp * te.std_gp + prior * 20) / (te.std_gp + 20)
        print(s, {pos: (round(rho_by_week(te[te.pos == pos], "ridge"), 3), round(rho_by_week(te[te.pos == pos], "shrunk"), 3))
                  for pos in ("F", "D")})

    coef, icpt, means = fit_linear(df)
    wire = df[df.wire]
    repl = wire.groupby("pos").std_fp.mean()

    g = pd.read_parquet(os.path.join(RESEARCH, "data", "goalie_games.parquet"))
    tg = pd.read_parquet(os.path.join(RESEARCH, "data", "team_games.parquet"))
    st = start_table(g, tg)
    lr = LogisticRegression(max_iter=1000).fit(st[START_FEATS], st.started)
    pts_per_start = float(g[(g.gamesStarted == 1) & (g.season == g.season.max())].fp.mean())

    model = {
        "skater": {"features": FEATS, "coef": [round(float(x), 6) for x in coef], "intercept": round(float(icpt), 6),
                   "fill": {k: round(float(v), 4) for k, v in means.items()},
                   "replacement_fpg": {k: round(float(v), 3) for k, v in repl.items()}},
        "dress": dress_table(df),
        "goalie": {"features": START_FEATS, "coef": [round(float(x), 4) for x in lr.coef_[0]],
                   "intercept": round(float(lr.intercept_[0]), 4), "points_per_start": round(pts_per_start, 2)},
    }
    with open(OUT, "w") as f:
        json.dump(model, f, indent=1)
    print("wrote", OUT, "pts/start", model["goalie"]["points_per_start"], "dress", model["dress"])


if __name__ == "__main__":
    main()
