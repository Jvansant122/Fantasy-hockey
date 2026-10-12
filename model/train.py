"""Fit the Claude Rating models and write model/rating_model.json for scripts/rating.py.

Uses the research data (NHL stats API game logs plus MoneyPuck xG, 2017-18 to 2025-26, in hunt8/all; the skater
ridge trains on 2019-20 on, so every row has two seasons ago) from the project's research folder: pass its path as
the first argument (default /mnt/project-files/research/claude-rating).

Only inputs the daily workflow can rebuild cheaply are used: game-level NHL stats for this season
(last 5/10/20 games and season to date), season totals for last season, and MoneyPuck season xG.
"""
import json
import os
import sys

import numpy as np
import pandas as pd
from itertools import combinations

from sklearn.linear_model import LogisticRegression, Ridge, RidgeCV
from sklearn.preprocessing import StandardScaler

RESEARCH = sys.argv[1] if len(sys.argv) > 1 else "/mnt/project-files/research/claude-rating"
sys.path.insert(0, RESEARCH)
import common  # noqa: E402
from goalies import START_FEATS, start_table  # noqa: E402

OLD_START_FEATS = START_FEATS[:8]  # before rest and workload (findings section 11)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rating_model.json")
CACHE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "rating_snapshots_2017.parquet")
# game logs and xG back to 2017-18 (research hunt 8), so every skater ridge training row from 2019-20 has two seasons
# ago; the dress table and replacement level keep the 2021-26 rows they were fit on
HISTORY_DATA = os.path.join(RESEARCH, "hunt8", "all")
TRAIN_FROM, TABLES_FROM = 20192020, 20212022
GAME_RATES = ["fp", "toi", "pptoi", "shots", "iCF", "hits", "blockedShots", "goals", "assists", "ppPoints", "points"]
XG_RATES = ["ixg", "onice_xgf"]
# where he ranks in his team-game's PP time and ice time (findings section 201): this season's windows only
RANK_RATES = ["pp_rank", "toi_rank", "pp1", "top_line"]
# position x usage and squared ice time (findings section 201)
D_X = ["std_toi", "std_pptoi", "l5_toi", "l5_pptoi", "std_blockedShots", "std_hits"]
SQUARED = ["std_toi", "l5_toi", "std_pptoi"]
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
    f += [f"{w}_{r}" for w in ("l5", "l10", "l20", "std") for r in RANK_RATES]
    f += [f"D_x_{n}" for n in D_X] + [f"{n}2" for n in SQUARED]
    return f


BASE_FEATS = feature_names()
UX_ERA_FEATS = BASE_FEATS + UX_FEATS
# next week's ice time and PP time forecast from this season's windows (findings 310 (1)): the usage x efficiency
# inputs again with the forecasts in place of last-5 TOI and PP TOI, plus the forecasts themselves
TOI_FC_IN = ["last_toi", "toi_l3", "l5_toi", "l10_toi", "l20_toi", "std_toi", "prev_toi"]
PP_FC_IN = ["pp_l1", "pp_l3", "l5_pptoi", "l10_pptoi", "l20_pptoi", "std_pptoi", "prev_pptoi", "l5_pp_rank", "std_pp_rank"]
FC_EXTRA = ["no_prev", "is_D", "gpw"]
FC_FEATS = [f"fc_{n}" for n in UX_FEATS] + ["fc_toi", "fc_pptoi"]
# pairwise products of the 12 strongest inputs, a linear stand-in for the declined tree layer (findings 310 (2))
PAIR_IN = ["sx_ux", "sx_fp", "sx_pp", "l5_toi", "l5_pptoi", "std_fp", "prev_fp", "std_gp", "std_shots", "prev_shots", "std_ixg", "l5_fp"]
PAIR_FEATS = [f"{a}*{b}" for a, b in combinations(PAIR_IN, 2)]
# two seasons ago, per game, and its games played (findings 310 (4); the career rates there added nothing on top)
P2_RATES = ["fp", "toi", "pptoi", "shots", "hits", "blockedShots", "points"]
P2_FEATS = [f"p2_{r}" for r in P2_RATES] + ["p2_gp", "no_p2"] + [f"p2_{r}_w" for r in ("fp", "toi", "pptoi")]
PR110_FEATS = UX_ERA_FEATS + FC_FEATS + PAIR_FEATS
FEATS = PR110_FEATS + P2_FEATS


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


def ux_features(df, mu, t5=None, p5=None):
    """Same arithmetic as scripts/rating.py ux_features, vectorised. t5, p5: ice time and PP time to use in place of
    the last-5 averages (the forecasts)."""
    gp_s, gp_p = df.std_gp, df.prev_gp.fillna(0) * UX_PREV_W
    tot = lambda c: df[f"std_{c}"] * gp_s + df[f"prev_{c}"].fillna(0) * gp_p
    toi, pp = tot("toi"), tot("pptoi")
    m = lambda c: df.pos.map({p: v[c] for p, v in mu.items()})
    r = {c: (tot(c) + UX_PRIOR_MIN * m(c)) / (toi + UX_PRIOR_MIN) for c in list(UX_STATS) + ["ixg", "fp"]}
    r_ev = (tot("points") - tot("ppPoints") + UX_PRIOR_MIN * m("evpts")) / ((toi - pp) + UX_PRIOR_MIN)
    r_pp = (tot("ppPoints") + UX_PRIOR_PP_MIN * m("pp")) / (pp + UX_PRIOR_PP_MIN)
    t5 = df.l5_toi if t5 is None else t5
    p5 = df.l5_pptoi if p5 is None else p5
    out = pd.DataFrame(index=df.index)
    out["sx_ux"] = sum(w * r[c] for c, w in UX_STATS.items()) * t5 + 0.5 * r_pp * p5
    out["sx_ev"] = r_ev * (t5 - p5).clip(lower=0)
    out["sx_pp"] = r_pp * p5
    out["sx_fp"] = r["fp"] * t5
    for c in ("shots", "hits", "blockedShots", "ixg"):
        out[f"sx_{c}"] = r[c] * t5
    return out


def add_ranks(sk):
    """Per (game, team): rank of his PP TOI among the team's skaters and of his TOI among its forwards or defensemen
    (1 = most, ties share the smaller rank), PP1 (top 5 in PP TOI with over half a minute) and top line (top 3 F or
    top 2 D in TOI). Same as scripts/rating.py add_ranks."""
    g = sk.groupby(["gameId", "teamAbbrev"])
    sk["pp_rank"] = g.pptoi.rank(ascending=False, method="min")
    sk["toi_rank"] = sk.groupby(["gameId", "teamAbbrev", "pos"]).toi.rank(ascending=False, method="min")
    sk["pp1"] = ((sk.pp_rank <= 5) & (sk.pptoi > 0.5)).astype(float)
    sk["top_line"] = (sk.toi_rank <= np.where(sk.pos == "D", 2, 3)).astype(float)
    return sk


def add_recent(df, sk):
    """Last game's and the last 3 games' TOI and PP TOI this season before each Monday (the forecasts' inputs)."""
    sk = sk.sort_values(["playerId", "gameDate"]).copy()
    g = sk.groupby(["playerId", "season"])
    sk["toi_l3"] = g.toi.transform(lambda x: x.rolling(3, min_periods=1).mean())
    sk["pp_l3"] = g.pptoi.transform(lambda x: x.rolling(3, min_periods=1).mean())
    sk = sk.rename(columns={"toi": "last_toi", "pptoi": "pp_l1", "gameDate": "asof"})
    sk = sk[["playerId", "season", "asof", "last_toi", "toi_l3", "pp_l1", "pp_l3"]].sort_values("asof")
    keys = df[["playerId", "monday", "season"]].assign(asof=df.monday - pd.Timedelta(days=1)).sort_values("asof")
    m = pd.merge_asof(keys, sk, on="asof", by=["playerId", "season"], direction="backward")
    return df.drop(columns=["last_toi", "toi_l3", "pp_l1", "pp_l3"], errors="ignore").merge(
        m[["playerId", "monday", "last_toi", "toi_l3", "pp_l1", "pp_l3"]], on=["playerId", "monday"], how="left")


def fc_design(df, ins):
    X = df[ins].copy()
    X["no_prev"] = df.prev_gp.isna().astype(float)
    X["is_D"] = (df.pos == "D").astype(float)
    X["gpw"] = np.clip(df.std_gp / 20, 0, 1)
    return X


def fit_forecast(train):
    """Two small ridges (alpha 1, weighted by games next week): next week's TOI/G and PP TOI/G (findings 310 (1))."""
    tr = train[train.y_gp > 0]
    st = {}
    for tgt, ins in (("y_toi", TOI_FC_IN), ("y_pptoi", PP_FC_IN)):
        X = fc_design(tr, ins)
        fill = X.mean()
        m = Ridge(alpha=1.0).fit(X.fillna(fill), tr[tgt], sample_weight=tr.y_gp)
        st[tgt] = {"inputs": ins, "coef": m.coef_, "intercept": float(m.intercept_), "fill": fill}
    return st


def forecast(df, st):
    return {tgt: pd.Series(fc_design(df, m["inputs"]).fillna(m["fill"]).values @ m["coef"] + m["intercept"], index=df.index)
            for tgt, m in st.items()}


def add_p2(df, sk):
    """Two seasons ago: per-game means of each P2_RATES stat and games played (NaN when he didn't play then)."""
    p2 = sk.groupby(["playerId", "season"]).agg(**{f"p2_{r}": (r, "mean") for r in P2_RATES}, p2_gp=("fp", "size")).reset_index()
    p2["season"] = p2.season + 20002
    return df.drop(columns=[c for c in p2 if c.startswith("p2_")], errors="ignore").merge(p2, on=["playerId", "season"], how="left")


def snapshots():
    if os.path.exists(CACHE):
        return pd.read_parquet(CACHE)
    common.DATA = HISTORY_DATA
    sk = add_ranks(common.load_skaters())
    sk["iCF"] = sk["totalShotAttempts"]  # live data uses NHL shot attempts, so train on the same
    common.RATES = common.RATES + [r for r in RANK_RATES if r not in common.RATES]  # skater_snapshots windows these
    df = add_p2(add_recent(common.skater_snapshots(sk), sk), sk)
    df.to_parquet(CACHE)
    return df


def prep(df, feats=FEATS, mu=None, st=None):
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
    extra = {f"D_x_{n}": X.is_D * df[n].fillna(0) for n in D_X} | {f"{n}2": df[n] ** 2 for n in SQUARED}
    if "p2_fp" in df:
        w2 = np.clip(df.p2_gp.fillna(0) / 82, 0, 1)
        extra |= {f"p2_{r}": df[f"p2_{r}"] for r in P2_RATES} | {"p2_gp": df.p2_gp.fillna(0), "no_p2": df.p2_gp.isna().astype(float)}
        extra |= {f"p2_{r}_w": df[f"p2_{r}"].fillna(0) * w2 for r in ("fp", "toi", "pptoi")}
    X = pd.concat([X.drop(columns=[k for k in extra if k in X]), pd.DataFrame(extra, index=df.index)], axis=1)
    if st and "fc" in st:
        fc = forecast(df, st["fc"])
        u = ux_features(df, mu, fc["y_toi"], fc["y_pptoi"].clip(lower=0)).add_prefix("fc_")
        u["fc_toi"], u["fc_pptoi"] = fc["y_toi"], fc["y_pptoi"]
        X = pd.concat([X, u], axis=1)
    if st and "pair_fill" in st:
        filled = X[PAIR_IN].fillna(st["pair_fill"])
        X = pd.concat([X, pd.DataFrame({f"{a}*{b}": filled[a] * filled[b] for a, b in combinations(PAIR_IN, 2)}, index=df.index)], axis=1)
    return X[feats]


def fit_state(train, feats, mu):
    """What the forecast and product inputs need from the training rows: the two forecast ridges, and each product
    factor's training mean (a missing factor is filled with it before multiplying)."""
    tr = train[train.y_gp > 0]
    st = {}
    if any(c in FC_FEATS for c in feats):
        st["fc"] = fit_forecast(tr)
    if any(c in PAIR_FEATS for c in feats):
        st["pair_fill"] = prep(tr, UX_ERA_FEATS, mu)[PAIR_IN].mean()
    return st


def fit_linear(train, feats=FEATS, mu=None):
    """One ridge per scoring stat, summed with league weights and folded back to raw-unit coefficients."""
    tr = train[train.y_gp > 0]
    st = fit_state(tr, feats, mu)
    X = prep(tr, feats, mu, st)
    means = X.mean().fillna(0)
    X = X.fillna(means)
    sc = StandardScaler().fit(X)
    Z = sc.transform(X)
    coef, icpt = np.zeros(len(feats)), 0.0
    for c, wt in COMPONENTS.items():
        m = RidgeCV(alphas=np.logspace(-1, 5, 13)).fit(Z, tr[f"y_{c}"], sample_weight=tr.y_gp)
        coef += wt * m.coef_ / sc.scale_
        icpt += wt * (m.intercept_ - (m.coef_ * sc.mean_ / sc.scale_).sum())
    return coef, icpt, means, st


def predict(df, coef, icpt, means, st, feats=FEATS, mu=None):
    return prep(df, feats, mu, st).fillna(means).values @ coef + icpt


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
        te["before"] = predict(te, *fit_linear(tr, PR110_FEATS, mu), PR110_FEATS, mu)
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
    df = df[(df.std_gp >= 1) & (df.season >= TRAIN_FROM)].copy()
    skater_backtest(df)

    mu = ux_means(df)
    coef, icpt, means, sk_st = fit_linear(df, FEATS, mu)
    tables = df[df.season >= TABLES_FROM]
    wire = tables[tables.wire]
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
                   "toi_forecast": {tgt: {"inputs": m["inputs"] + FC_EXTRA, "coef": [round(float(x), 6) for x in m["coef"]],
                                          "intercept": round(m["intercept"], 6), "fill": {k: round(float(v), 4) for k, v in m["fill"].items()}}
                                    for tgt, m in sk_st["fc"].items()},
                   "pairs": {"inputs": PAIR_IN, "fill": {k: round(float(v), 4) for k, v in sk_st["pair_fill"].items()}},
                   "replacement_fpg": {k: round(float(v), 3) for k, v in repl.items()}},
        "dress": dress_table(tables),
        "goalie": {"features": START_FEATS, "coef": [round(float(x), 4) for x in lr.coef_[0]],
                   "intercept": round(float(lr.intercept_[0]), 4), "points_per_start": round(pts_per_start, 2)},
    }
    # P(dresses) logistic from lineup history, fit by the correlation research (findings section 6, handoff row 11)
    with open(os.path.join(os.path.dirname(RESEARCH.rstrip("/")), "correlation", "dress_logit.json")) as f:
        model["dress_logit"] = json.load(f)
    with open(OUT, "w") as f:
        json.dump(model, f, indent=1)
    print("wrote", OUT, "pts/start", model["goalie"]["points_per_start"], "dress", model["dress"])
    check_daily_arithmetic(df, coef, icpt, means, sk_st, mu)


def check_daily_arithmetic(df, coef, icpt, means, st, mu, n=2000):
    """scripts/rating.py rebuilds the forecast and product inputs itself: its FP/G must match this script's on the
    training rows, or the daily ratings would silently differ from the fitted model."""
    sys.path.insert(0, os.path.join(os.path.dirname(OUT), "..", "scripts"))
    import rating
    rows = df.sample(min(n, len(df)), random_state=0)
    want = np.maximum(predict(rows, coef, icpt, means, st, FEATS, mu), 0)
    base = prep(rows, UX_ERA_FEATS + P2_FEATS, mu)
    m = rating.Model()
    got = []
    for (_, r), (_, b) in zip(rows.iterrows(), base.iterrows()):
        f = {k: (None if pd.isna(v) else float(v)) for k, v in r.items() if isinstance(v, (int, float, np.number))}
        f |= {k: (None if pd.isna(v) else float(v)) for k, v in b.items()}
        got.append(m.fpg(f))
    gap = np.abs(np.array(got) - want).max()
    print(f"daily arithmetic check: largest FP/G gap {gap:.2e} over {len(rows)} rows")
    assert gap < 1e-3, "scripts/rating.py and model/train.py disagree on the skater inputs"


if __name__ == "__main__":
    main()
