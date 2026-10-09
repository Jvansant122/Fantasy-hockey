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

OLD_START_FEATS = START_FEATS[:8]  # before rest and workload (findings section 11)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rating_model.json")
CACHE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "rating_snapshots.parquet")
GAME_RATES = ["fp", "toi", "pptoi", "shots", "iCF", "hits", "blockedShots", "goals", "assists", "ppPoints", "points"]
XG_RATES = ["ixg", "onice_xgf"]
COMPONENTS = common.COMPONENTS
TEST_SEASONS = [20232024, 20242025, 20252026]
# usage x efficiency (research/correlation/findings.md section 4, site-friendly version): per-60 rates from season to
# date plus half of last season, shrunk to the position mean over 300 minutes (60 PP minutes), times last-5 ice time
UX_PRIOR_MIN, UX_PRIOR_PP_MIN, UX_PREV_W = 300.0, 60.0, 0.5
UX_STATS = {"goals": 2, "assists": 1, "shots": 0.1, "hits": 0.1, "blockedShots": 0.5}
UX_FEATS = ["sx_ux", "sx_ev", "sx_pp", "sx_fp", "sx_shots", "sx_hits", "sx_blockedShots", "sx_ixg"]


def feature_names():
    f = [f"{w}_{r}" for w in ("l5", "l10", "l20", "std", "prev") for r in GAME_RATES]
    f += [f"{w}_{r}" for w in ("std", "prev") for r in XG_RATES]
    f += ["std_gp", "prev_gp", "is_D", "no_prev", "dressed3", "dressed10"]
    f += [f"std_{r}_w" for r in ("fp", "toi", "pptoi", "shots", "blockedShots", "hits")]
    return f


BASE_FEATS = feature_names()
FEATS = BASE_FEATS + UX_FEATS


def ux_means(df):
    """Per-minute position means the usage x efficiency rates shrink toward."""
    mu = {}
    for pos in ("F", "D"):
        x = df[df.pos == pos]
        toi, pp = (x.std_toi * x.std_gp).sum(), (x.std_pptoi * x.std_gp).sum()
        mu[pos] = {c: float((x[f"std_{c}"] * x.std_gp).sum() / toi) for c in list(UX_STATS) + ["ixg", "fp"]}
        mu[pos]["evpts"] = float(((x.std_points - x.std_ppPoints) * x.std_gp).sum() / (toi - pp))
        mu[pos]["pp"] = float((x.std_ppPoints * x.std_gp).sum() / pp)
    return mu


def ux_features(df, mu):
    """Same arithmetic as scripts/rating.py ux_features, vectorised."""
    gp_s, gp_p = df.std_gp, df.prev_gp.fillna(0) * UX_PREV_W
    tot = lambda c: df[f"std_{c}"] * gp_s + df[f"prev_{c}"].fillna(0) * gp_p
    toi, pp = tot("toi"), tot("pptoi")
    m = lambda c: df.pos.map({p: v[c] for p, v in mu.items()})
    r = {c: (tot(c) + UX_PRIOR_MIN * m(c)) / (toi + UX_PRIOR_MIN) for c in list(UX_STATS) + ["ixg", "fp"]}
    r_ev = (tot("points") - tot("ppPoints") + UX_PRIOR_MIN * m("evpts")) / ((toi - pp) + UX_PRIOR_MIN)
    r_pp = (tot("ppPoints") + UX_PRIOR_PP_MIN * m("pp")) / (pp + UX_PRIOR_PP_MIN)
    t5, p5 = df.l5_toi, df.l5_pptoi
    out = pd.DataFrame(index=df.index)
    out["sx_ux"] = sum(w * r[c] for c, w in UX_STATS.items()) * t5 + 0.5 * r_pp * p5
    out["sx_ev"] = r_ev * (t5 - p5).clip(lower=0)
    out["sx_pp"] = r_pp * p5
    out["sx_fp"] = r["fp"] * t5
    for c in ("shots", "hits", "blockedShots", "ixg"):
        out[f"sx_{c}"] = r[c] * t5
    return out


def snapshots():
    if os.path.exists(CACHE):
        return pd.read_parquet(CACHE)
    sk = common.load_skaters()
    sk["iCF"] = sk["totalShotAttempts"]  # live data uses NHL shot attempts, so train on the same
    df = common.skater_snapshots(sk)
    df.to_parquet(CACHE)
    return df


def prep(df, feats=FEATS, mu=None):
    X = pd.DataFrame(index=df.index)
    if mu is not None:
        X = ux_features(df, mu)
    for c in feats:
        if c in df:
            X[c] = df[c]
    X["prev_gp"] = df.prev_gp.fillna(0)
    X["is_D"] = (df.pos == "D").astype(float)
    X["no_prev"] = df.prev_gp.isna().astype(float)
    w = np.clip(df.std_gp / 20.0, 0, 1)
    for r in ("fp", "toi", "pptoi", "shots", "blockedShots", "hits"):
        X[f"std_{r}_w"] = df[f"std_{r}"] * w
    return X[feats]


def fit_linear(train, feats=FEATS, mu=None):
    """One ridge per scoring stat, summed with league weights and folded back to raw-unit coefficients."""
    tr = train[train.y_gp > 0]
    X = prep(tr, feats, mu)
    means = X.mean().fillna(0)
    X = X.fillna(means)
    sc = StandardScaler().fit(X)
    Z = sc.transform(X)
    coef, icpt = np.zeros(len(feats)), 0.0
    for c, wt in COMPONENTS.items():
        m = RidgeCV(alphas=np.logspace(-1, 3, 9)).fit(Z, tr[f"y_{c}"], sample_weight=tr.y_gp)
        coef += wt * m.coef_ / sc.scale_
        icpt += wt * (m.intercept_ - (m.coef_ * sc.mean_ / sc.scale_).sum())
    return coef, icpt, means


def predict(df, coef, icpt, means, feats=FEATS, mu=None):
    return prep(df, feats, mu).fillna(means).values @ coef + icpt


def dress_table(train):
    t = train.assign(d3=(train.dressed3.fillna(0).clip(0, 1) * 3).round().astype(int),
                     d10=pd.cut(train.dressed10.fillna(0).clip(0, 1), [-0.01, 0.5, 0.8, 1.0], labels=False).astype(int))
    g = t.groupby(["d3", "d10"])
    rate = (g.y_gp.sum() / g.team_games.sum())[g.team_games.sum() >= 500]  # drop near-empty cells
    return {f"{a}_{b}": round(float(v), 3) for (a, b), v in rate.items()}


def rho_by_week(d, col):
    vals = [g[col].rank().corr(g.y_fp.rank()) for _, g in d.groupby("monday") if len(g) >= 20]
    return float(np.nanmean(vals))


def weekly_rho(d, col):
    return d.groupby("monday").apply(lambda g: g[col].rank().corr(g.y_fp.rank()) if len(g) >= 20 else np.nan,
                                     include_groups=False)


def skater_backtest(df):
    """Rolling origin: each test season predicted by models fit on earlier seasons, with and without the new inputs."""
    parts = []
    for s in TEST_SEASONS:
        tr, te = df[df.season < s], df[(df.season == s) & (df.y_gp > 0) & (df.std_gp >= 3)].copy()
        mu = ux_means(tr)
        te["before"] = predict(te, *fit_linear(tr, BASE_FEATS), BASE_FEATS)
        te["after"] = predict(te, *fit_linear(tr, FEATS, mu), FEATS, mu)
        parts.append(te)
    t = pd.concat(parts)
    first = t.groupby("season").monday.min()
    t["early"] = (t.monday - t.season.map(first)).dt.days // 7 < 6
    for pool, d in (("all", t), ("waiver", t[t.wire]), ("first 6 weeks", t[t.early]), ("first 6 weeks waiver", t[t.early & t.wire])):
        b, a = weekly_rho(d, "before"), weekly_rho(d, "after")
        gain = (a - b).dropna()
        print(f"skaters {pool:21s} rho {b.mean():.4f} -> {a.mean():.4f}  gain {gain.mean():+.4f} ± {gain.std() / len(gain) ** .5:.4f}")


def goalie_backtest(st):
    """Fit on earlier seasons, test on the last: log loss, right starter, and error in each goalie's weekly starts."""
    test = st.season.max()
    tr, te = st[st.season < test], st[st.season == test].copy()
    te["monday"] = te.gameDate - pd.to_timedelta(te.gameDate.dt.weekday, unit="D")
    for name, feats in (("before", OLD_START_FEATS), ("after", START_FEATS)):
        p = LogisticRegression(max_iter=1000).fit(tr[feats], tr.started).predict_proba(te[feats])[:, 1]
        te["p"] = p / pd.Series(p, index=te.index).groupby([te.team, te.gameId]).transform("sum")  # as the site normalises
        right = te.loc[te.groupby(["team", "gameId"]).p.idxmax()].started.mean()
        wk = te.groupby(["playerId", "monday"])[["p", "started"]].sum()
        ll = -np.mean(np.log(np.clip(np.where(te.started == 1, te.p, 1 - te.p), 1e-6, 1)))
        print(f"goalies {name:6s} log loss {ll:.4f}  right starter {right:.1%}  weekly starts error {(wk.p - wk.started).abs().mean():.3f}")


def main():
    df = snapshots()
    df = df[df.std_gp >= 1].copy()
    skater_backtest(df)

    mu = ux_means(df)
    coef, icpt, means = fit_linear(df, FEATS, mu)
    wire = df[df.wire]
    repl = wire.groupby("pos").std_fp.mean()

    g = pd.read_parquet(os.path.join(RESEARCH, "data", "goalie_games.parquet"))
    tg = pd.read_parquet(os.path.join(RESEARCH, "data", "team_games.parquet"))
    st = start_table(g, tg)
    goalie_backtest(st)
    lr = LogisticRegression(max_iter=1000).fit(st[START_FEATS], st.started)
    pts_per_start = float(g[(g.gamesStarted == 1) & (g.season == g.season.max())].fp.mean())

    model = {
        "skater": {"features": FEATS, "coef": [round(float(x), 6) for x in coef], "intercept": round(float(icpt), 6),
                   "fill": {k: round(float(v), 4) for k, v in means.items()},
                   "ux_means": {p: {k: round(v, 6) for k, v in m.items()} for p, m in mu.items()},
                   "replacement_fpg": {k: round(float(v), 3) for k, v in repl.items()}},
        "dress": dress_table(df),
        "goalie": {"features": START_FEATS, "coef": [round(float(x), 4) for x in lr.coef_[0]],
                   "intercept": round(float(lr.intercept_[0]), 4), "points_per_start": round(pts_per_start, 2)},
    }
    # P(dresses) logistic from lineup history, fit by the correlation research (findings section 6, handoff row 11)
    with open(os.path.join(os.path.dirname(RESEARCH.rstrip("/")), "correlation", "dress_logit.json")) as f:
        model["dress_logit"] = json.load(f)
    with open(OUT, "w") as f:
        json.dump(model, f, indent=1)
    print("wrote", OUT, "pts/start", model["goalie"]["points_per_start"], "dress", model["dress"])


if __name__ == "__main__":
    main()
