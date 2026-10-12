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
CACHE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "rating_snapshots_2017_dec.parquet")
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
# lineup context (he missed the team's last game; same-position regulars above him in ice time, or above him in PP
# time, who missed it; the gaps last game vs his last 10) and last-game / last-3 role (findings 320 (1))
LINEUP_TOI = ["missed_last", "out_above_toi", "ml_gap", "oa_gap"]
LINEUP_PP = ["missed_last", "out_above_pp", "ml_ppgap", "oap_gap"]
ROLE_TOI = ["r1_toi_rank", "r3_toi_rank", "r1_top_line", "r3_top_line"]
ROLE_PP = ["r1_pp_share", "r3_pp_share", "r1_pp_rank", "r3_pp_rank", "r1_pp1", "r3_pp1"]
TOI_FC_IN = TOI_FC_IN + LINEUP_TOI + ROLE_TOI
PP_FC_IN = PP_FC_IN + LINEUP_PP + ROLE_PP
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
# Week rating only: hinge curves max(0, x - knot) on 12 strong inputs, knots at their training 10/30/50/70/90th
# percentiles; the Season rating keeps the ridge without them, where they cost -0.0003 (findings 320 (2))
HINGE_IN = ["sx_ux", "sx_fp", "std_fp", "prev_fp", "l5_toi", "fc_toi", "fc_sx_ux", "p2_fp", "fc_pptoi", "fc_sx_pp", "sx_pp", "std_shots"]
HINGE_Q = [0.1, 0.3, 0.5, 0.7, 0.9]
HINGE_FEATS = [f"h_{c}_{i}" for c in HINGE_IN for i in range(len(HINGE_Q))]
# Week rating only (findings 320 (4)): finishing luck (goals below / above individual xG, this season weighted by games
# and last season), last-game role and ice-time jumps, and fresh top-line / PP1 promotions
LUCK_FEATS = ["luck_neg", "luck_pos", "luck_negp", "luck_posp", "luck_sh"]
ROLE_FEATS = ["r1_top_line", "r3_top_line", "r1_pp1", "r3_pp1", "r1_toi_rank", "r1_pp_share", "r3_pp_share", "j1_up", "j1_dn",
              "new_top", "new_pp1"]
# ... and age bends plus return-from-absence terms (findings 320 (5)): missed the team's last game, his run of missed team
# games (to 10), back after missing the one before, each also x season FP/G
AGE_RET_FEATS = ["ag_y", "ag_o", "r_ml", "r_orun", "r_ml_fp", "r_ml_sx", "r_orun_fp", "r_back", "r_back_fp"]
WEEK_FEATS = FEATS + HINGE_FEATS + LUCK_FEATS + ROLE_FEATS + AGE_RET_FEATS
GOAL_BLEND = 0.8  # the Week ridge's goals target: 0.8 x goals + 0.2 x individual xG per game (goals where xG is missing)
# Season rating only: decomposed scoring forecasts (findings 320 (3)). Per-minute rate forecasts (small ridges on the
# last 5/10/20, season, last season and two seasons ago per-minute rates) alone and times the TOI forecast; expected
# points = his share of on-ice goals (IPP) x on-ice xGF per minute x forecast EV and PP minutes, each shrunk to the
# position; a short-handed TOI forecast and blocks / hits x forecast PK and EV minutes
RATE_STATS = ["shots", "hits", "blockedShots", "goals", "assists", "fp"]
RATE_WIN = ["l5", "l10", "l20", "std", "prev", "p2"]
SH_IN = ["sh_l1", "sh_l5", "sh_l10", "sh_std", "sh_prev"]
IPP_K, IPP_EV_MIN, IPP_PP_MIN = 20.0, 300.0, 60.0
ONICE = ["points", "ppPoints", "toi", "pptoi", "a_ogf", "a_oxgf", "pp_ogf", "pp_oxgf"]
DEC_FEATS = [f"{p}_{c}" for c in RATE_STATS for p in ("rf", "rr")] + ["ipp_ev", "ipp_pp", "xev", "xpp", "xpts_ev", "xpts_pp", "xpts"] \
    + ["fc_sh", "fc_ev", "blk_x_sh", "blk_x_ev", "hit_x_ev"]
SEASON_FEATS = FEATS + DEC_FEATS


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
    top = g.pptoi.transform("max")
    sk["pp_share"] = (sk.pptoi / top.where(top > 0)).fillna(0)  # his PP time over the team's most that game
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


def add_context(df, sk):
    """Lineup context and last-game role before each Monday (findings 320 (1)), same as scripts/rating.py
    lineup_context and skater_features."""
    days = sk[["teamAbbrev", "season", "gameDate"]].drop_duplicates().rename(columns={"teamAbbrev": "team", "gameDate": "asof"})
    keys = df[["team", "season", "monday"]].drop_duplicates().assign(asof=lambda d: d.monday - pd.Timedelta(days=1))
    keys = pd.merge_asof(keys.sort_values("asof"), days.sort_values("asof").assign(team_last=lambda d: d["asof"]),
                         on="asof", by=["team", "season"], direction="backward")
    df = df.drop(columns=LINEUP_TOI + LINEUP_PP[1:] + ROLE_TOI + ROLE_PP, errors="ignore")
    df = df.merge(keys[["team", "season", "monday", "team_last"]], on=["team", "season", "monday"], how="left")
    df["missed_last"] = (df.last_game < df.team_last).astype(float)
    df["out_above_toi"], df["out_above_pp"] = 0.0, 0.0
    reg = df[df.std_gp >= 5]
    for _, g in reg.groupby(["team", "monday", "pos"]):
        o = g.std_toi[g.missed_last > 0].values
        if len(o):
            df.loc[g.index, "out_above_toi"] = (o[None, :] > g.std_toi.values[:, None]).sum(1)
    for _, g in reg.groupby(["team", "monday"]):
        o = g.std_pptoi[(g.missed_last > 0) & (g.std_pptoi > 0.5)].values
        if len(o):
            df.loc[g.index, "out_above_pp"] = (o[None, :] > g.std_pptoi.values[:, None]).sum(1)
    sk = sk.sort_values(["playerId", "gameDate"]).copy()
    g = sk.groupby(["playerId", "season"])
    r = pd.DataFrame({"playerId": sk.playerId, "season": sk.season, "asof": sk.gameDate})
    for c in ("pp_share", "pp_rank", "toi_rank", "pp1", "top_line"):
        r[f"r1_{c}"] = sk[c]
        r[f"r3_{c}"] = g[c].transform(lambda x: x.rolling(3, min_periods=1).mean())
    keys = df[["playerId", "monday", "season"]].assign(asof=df.monday - pd.Timedelta(days=1)).sort_values("asof")
    m = pd.merge_asof(keys, r.sort_values("asof"), on="asof", by=["playerId", "season"], direction="backward")
    return df.drop(columns="team_last").merge(m.drop(columns=["asof", "season"]), on=["playerId", "monday"], how="left")


def context_gaps(df):
    """The last-game vs last-10 gaps the lineup context scales (same as scripts/rating.py forecast_features)."""
    out = pd.DataFrame(index=df.index)
    out["ml_gap"] = df.missed_last * (df.l10_toi - df.last_toi)
    out["ml_ppgap"] = df.missed_last * (df.l10_pptoi - df.pp_l1)
    out["oa_gap"] = df.out_above_toi.clip(upper=3) * (df.last_toi - df.l10_toi)
    out["oap_gap"] = df.out_above_pp.clip(upper=3) * (df.pp_l1 - df.l10_pptoi)
    return out


def fc_design(df, ins):
    X = pd.concat([df, context_gaps(df)], axis=1)[ins].copy() if "missed_last" in df else df[ins].copy()
    X["no_prev"] = df.prev_gp.isna().astype(float)
    X["is_D"] = (df.pos == "D").astype(float)
    X["gpw"] = np.clip(df.std_gp / 20, 0, 1)
    return X


def week_extras(df, X):
    """The Week ridge's luck, role, age and return inputs (same as scripts/rating.py week_extras); missing luck and role
    factors stay missing, a missing age reads 27 and missing return flags or FP/G read 0."""
    gw = np.clip(df.std_gp / 20, 0, 1)
    lk, lkp = df.std_goals - df.std_ixg, df.prev_goals - df.prev_ixg
    out = pd.DataFrame({"luck_neg": (-lk).clip(lower=0) * gw, "luck_pos": lk.clip(lower=0) * gw,
                        "luck_negp": (-lkp).clip(lower=0), "luck_posp": lkp.clip(lower=0)}, index=df.index)
    out["luck_sh"] = out.luck_neg * df.std_shots
    for c in ("r1_top_line", "r3_top_line", "r1_pp1", "r3_pp1", "r1_toi_rank", "r1_pp_share", "r3_pp_share"):
        out[c] = df[c]
    out["j1_up"], out["j1_dn"] = (df.last_toi - df.l10_toi).clip(lower=0), (df.l10_toi - df.last_toi).clip(lower=0)
    out["new_top"], out["new_pp1"] = df.r1_top_line * (1 - df.std_top_line), df.r1_pp1 * (1 - df.std_pp1)
    age = df.age_m.fillna(27)
    out["ag_y"], out["ag_o"] = np.maximum(0, 24 - age), np.maximum(0, age - 29)
    ml, orun, bk = df.missed_last.fillna(0), df.out_run.fillna(0).clip(upper=10), df.back_last.fillna(0)
    fp, sx = df.std_fp.fillna(0), X.sx_fp.fillna(0)
    out["r_ml"], out["r_orun"], out["r_ml_fp"], out["r_ml_sx"] = ml, orun, ml * fp, ml * sx
    out["r_orun_fp"], out["r_back"], out["r_back_fp"] = orun * fp, bk, bk * fp
    return out


def add_age(df):
    """Age on the Monday, from the NHL bios birth dates (research data/birthdates.json); NaN when unknown."""
    with open(os.path.join(RESEARCH, "data", "birthdates.json")) as f:
        bd = pd.Series(json.load(f))
    bd.index = bd.index.astype(int)
    return df.assign(age_m=(df.monday - df.playerId.map(pd.to_datetime(bd))).dt.days / 365.25)


def add_returns(df, sk):
    """His run of missed team games before each Monday (counting back from the team's last game, up to 10) and whether he
    played the team's last game after missing the one before (same as scripts/rating.py lineup_context)."""
    played = set(zip(sk.playerId, sk.teamAbbrev, sk.gameDate))
    tl = sk.groupby(["teamAbbrev", "season"]).gameDate.apply(lambda x: np.sort(x.unique()))
    keys = df[["team", "season", "monday"]].drop_duplicates()
    last10 = {}
    for r in keys.itertuples():
        ds = tl.get((r.team, r.season), np.array([], dtype="datetime64[ns]"))
        i = np.searchsorted(ds, np.datetime64(r.monday))
        last10[(r.team, r.season, r.monday)] = [pd.Timestamp(d) for d in ds[max(0, i - 10):i][::-1]]
    run, back = [], []
    for r in df[["playerId", "team", "season", "monday"]].itertuples():
        ds = last10[(r.team, r.season, r.monday)]
        k = next((j for j, d in enumerate(ds) if (r.playerId, r.team, d) in played), len(ds))
        run.append(k)
        back.append(float(len(ds) >= 2 and (r.playerId, r.team, ds[0]) in played and (r.playerId, r.team, ds[1]) not in played))
    return df.assign(out_run=run, back_last=back)


def add_y_ixg(df, sk):
    """Next week's individual xG per game (the Week ridge's smoother goals target)."""
    s = sk[["playerId", "gameDate", "ixg"]].copy()
    s["monday"] = s.gameDate - pd.to_timedelta(s.gameDate.dt.weekday, unit="D")
    y = s.groupby(["playerId", "monday"]).agg(y_ixg=("ixg", "mean")).reset_index()
    return df.drop(columns="y_ixg", errors="ignore").merge(y, on=["playerId", "monday"], how="left")


def fit_forecast(train):
    """Two small ridges (alpha 1, weighted by games next week): next week's TOI/G and PP TOI/G (findings 310 (1))."""
    tr = train[train.y_gp > 0]
    st = {}
    for tgt, ins in zip(("y_toi", "y_pptoi"), (TOI_FC_IN, PP_FC_IN)):
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
    p2 = sk.groupby(["playerId", "season"]).agg(**{f"p2_{r}": (r, "mean") for r in P2_RATES + ["goals", "assists"]},
                                                 p2_gp=("fp", "size")).reset_index()
    p2["season"] = p2.season + 20002
    return df.drop(columns=[c for c in p2 if c.startswith("p2_")], errors="ignore").merge(p2, on=["playerId", "season"], how="left")


def add_season_inputs(df, sk):
    """Raw inputs of the Season rating's decomposed forecasts before each Monday (findings 320 (3)): this season's and
    last season's totals of points, PP points, TOI, PP TOI and MoneyPuck on-ice goals / xG for (all situations, 5 on 4);
    short-handed TOI last game, last 5 and 10, season and last season per game; next week's SH TOI per game."""
    mp = pd.read_parquet(os.path.join(RESEARCH, "ridge-inputs", "data", "mp_games.parquet"))[["playerId", "gameId", "a_ogf", "a_oxgf", "pp_ogf", "pp_oxgf"]]
    mp["gameId"] = mp.gameId.astype(int)
    g = sk[["playerId", "gameId", "gameDate", "season", "points", "ppPoints", "toi", "pptoi", "shTimeOnIce"]].merge(mp, on=["playerId", "gameId"], how="left")
    g = g.sort_values(["playerId", "gameDate"])
    g["sh"] = g.shTimeOnIce.fillna(0) / 60
    by = g.groupby(["playerId", "season"])
    r = pd.concat([g[["playerId", "season", "gameDate"]].rename(columns={"gameDate": "asof"}),
                   by[ONICE].cumsum().add_prefix("c_")], axis=1)
    r["sh_l1"] = g.sh
    for n, w in ((5, "sh_l5"), (10, "sh_l10")):
        r[w] = by.sh.transform(lambda x: x.rolling(n, min_periods=1).mean())
    r["sh_std"] = by.sh.transform(lambda x: x.expanding().mean())
    keys = df[["playerId", "monday", "season"]].assign(asof=df.monday - pd.Timedelta(days=1)).sort_values("asof")
    m = pd.merge_asof(keys, r.sort_values("asof"), on="asof", by=["playerId", "season"], direction="backward")
    prev = by[ONICE].sum().add_prefix("pv_").join(by.sh.mean().rename("sh_prev")).reset_index()
    prev["season"] += 10001
    m = m.merge(prev, on=["playerId", "season"], how="left").drop(columns=["asof", "season"])
    bad = (m.pv_a_oxgf.fillna(0) == 0) & (m.pv_toi > 100)  # no MoneyPuck games that season
    m.loc[bad, ["pv_a_ogf", "pv_a_oxgf", "pv_pp_ogf", "pv_pp_oxgf"]] = np.nan
    g["monday"] = g.gameDate - pd.to_timedelta(g.gameDate.dt.weekday, unit="D")
    y = g.groupby(["playerId", "monday"]).sh.mean().rename("y_sh").reset_index()
    cols = [c for c in m if c not in ("playerId", "monday")] + ["y_sh"]
    return df.drop(columns=cols, errors="ignore").merge(m, on=["playerId", "monday"], how="left").merge(y, on=["playerId", "monday"], how="left")


def rate_design(df, c):
    """Per-minute rate of stat c over each window, plus the forecast ridges' flags."""
    X = pd.DataFrame({f"{w}_{c}_pm": df[f"{w}_{c}"] / df[f"{w}_toi"].where(df[f"{w}_toi"] > 3) for w in RATE_WIN}, index=df.index)
    X["no_prev"] = df.prev_gp.isna().astype(float)
    X["is_D"] = (df.pos == "D").astype(float)
    X["gpw"] = np.clip(df.std_gp / 20, 0, 1)
    X["no_p2"] = df.p2_gp.isna().astype(float)
    return X


def onice_parts(df):
    """This season + half of last season, each counted only when MoneyPuck has that season for him."""
    has_c, has_p = df.c_a_ogf.notna(), df.pv_a_ogf.notna()
    t = lambda c: df[f"c_{c}"].where(has_c, 0).fillna(0) + 0.5 * df[f"pv_{c}"].where(has_p, 0).fillna(0)
    return dict(ev_pts=t("points") - t("ppPoints"), pp_pts=t("ppPoints"), ev_ogf=t("a_ogf") - t("pp_ogf"), pp_ogf=t("pp_ogf"),
                ev_oxgf=t("a_oxgf") - t("pp_oxgf"), pp_oxgf=t("pp_oxgf"), evt=t("toi") - t("pptoi"), ppt=t("pptoi"))


def fit_decomposed(tr):
    """What the decomposed inputs need from the training rows: one rate ridge per stat (inputs clipped to the training
    0.5-99.5th percentiles, missing filled with the training mean), the position means IPP and on-ice xGF shrink to,
    and the SH TOI forecast ridge."""
    st = {"rates": {}, "ipp_mu": {}}
    for c in RATE_STATS:
        X = rate_design(tr, c)
        fill, lo, hi = X.mean(), X.quantile(0.005), X.quantile(0.995)
        y = tr[f"y_{c}"] / tr.y_toi.where(tr.y_toi > 3)
        ok = y.notna()
        m = Ridge(alpha=1.0).fit(X.clip(lo, hi, axis=1).fillna(fill)[ok], y[ok].clip(upper=y.quantile(0.995)), sample_weight=(tr.y_gp * tr.y_toi)[ok])
        st["rates"][c] = {"inputs": list(X.columns), "coef": m.coef_, "intercept": float(m.intercept_), "fill": fill, "lo": lo, "hi": hi}
    p = onice_parts(tr)
    for pos in ("F", "D"):
        s = {k: v[tr.pos == pos].sum() for k, v in p.items()}
        st["ipp_mu"][pos] = {"ipp_ev": s["ev_pts"] / s["ev_ogf"], "ipp_pp": s["pp_pts"] / s["pp_ogf"], "xev": s["ev_oxgf"] / s["evt"], "xpp": s["pp_oxgf"] / s["ppt"]}
    X = fc_design(tr, SH_IN)
    fill, ok = X.mean(), tr.y_sh.notna()
    m = Ridge(alpha=1.0).fit(X.fillna(fill)[ok], tr.y_sh[ok], sample_weight=tr.y_gp[ok])
    st["sh"] = {"inputs": SH_IN, "coef": m.coef_, "intercept": float(m.intercept_), "fill": fill}
    return st


def decomposed(df, X, st):
    """The Season rating's decomposed inputs (same arithmetic as scripts/rating.py season_features)."""
    o = pd.DataFrame(index=df.index)
    for c, m in st["rates"].items():
        r = rate_design(df, c).clip(m["lo"], m["hi"], axis=1).fillna(m["fill"]).values @ m["coef"] + m["intercept"]
        o[f"rf_{c}"], o[f"rr_{c}"] = r * X.fc_toi, r
    p = onice_parts(df)
    mu = lambda k: df.pos.map({q: v[k] for q, v in st["ipp_mu"].items()})
    o["ipp_ev"] = (p["ev_pts"] + IPP_K * mu("ipp_ev")) / (p["ev_ogf"] + IPP_K)
    o["ipp_pp"] = (p["pp_pts"] + IPP_K * mu("ipp_pp")) / (p["pp_ogf"] + IPP_K)
    o["xev"] = (p["ev_oxgf"] + IPP_EV_MIN * mu("xev")) / (p["evt"] + IPP_EV_MIN)
    o["xpp"] = (p["pp_oxgf"] + IPP_PP_MIN * mu("xpp")) / (p["ppt"] + IPP_PP_MIN)
    pp = X.fc_pptoi.clip(lower=0)
    o["xpts_ev"] = o.ipp_ev * o.xev * (X.fc_toi - pp).clip(lower=0)
    o["xpts_pp"] = o.ipp_pp * o.xpp * pp
    o["xpts"] = o.xpts_ev + o.xpts_pp
    sh = pd.Series(fc_design(df, SH_IN).fillna(st["sh"]["fill"]).values @ st["sh"]["coef"] + st["sh"]["intercept"], index=df.index).clip(lower=0)
    ev = (X.fc_toi - pp - sh).clip(lower=0)
    tot = df.std_toi * df.std_gp + df.prev_toi.fillna(0) * df.prev_gp.fillna(0) * 0.5
    rate = lambda c, prior: (df[f"std_{c}"] * df.std_gp + df[f"prev_{c}"].fillna(0) * df.prev_gp.fillna(0) * 0.5 + 300 * prior) / (tot + 300)
    blk, hit = rate("blockedShots", 0.06), rate("hits", 0.08)
    o["fc_sh"], o["fc_ev"], o["blk_x_sh"], o["blk_x_ev"], o["hit_x_ev"] = sh, ev, blk * sh, blk * ev, hit * ev
    return o


def snapshots():
    common.DATA = HISTORY_DATA
    if os.path.exists(CACHE):
        df = pd.read_parquet(CACHE)
        if "y_ixg" not in df:  # a cache from before the smoother goals target
            df = add_y_ixg(df, common.load_skaters())
            df.to_parquet(CACHE)
        if "out_run" not in df:  # ... and before the return-from-absence terms
            df = add_returns(df, common.load_skaters())
            df.to_parquet(CACHE)
        return add_age(df)
    sk = add_ranks(common.load_skaters())
    sk["iCF"] = sk["totalShotAttempts"]  # live data uses NHL shot attempts, so train on the same
    common.RATES = common.RATES + [r for r in RANK_RATES if r not in common.RATES]  # skater_snapshots windows these
    df = add_y_ixg(add_season_inputs(add_context(add_p2(add_recent(common.skater_snapshots(sk), sk), sk), sk), sk), sk)
    df = add_returns(df, sk)
    df.to_parquet(CACHE)
    return add_age(df)


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
    if any(c in LUCK_FEATS + ROLE_FEATS + AGE_RET_FEATS for c in feats):
        E = week_extras(df, X)
        X = pd.concat([X.drop(columns=[c for c in E if c in X]), E], axis=1)
    if st and "fc" in st:
        fc = forecast(df, st["fc"])
        u = ux_features(df, mu, fc["y_toi"], fc["y_pptoi"].clip(lower=0)).add_prefix("fc_")
        u["fc_toi"], u["fc_pptoi"] = fc["y_toi"], fc["y_pptoi"]
        X = pd.concat([X, u], axis=1)
    if st and "pair_fill" in st:
        filled = X[PAIR_IN].fillna(st["pair_fill"])
        X = pd.concat([X, pd.DataFrame({f"{a}*{b}": filled[a] * filled[b] for a, b in combinations(PAIR_IN, 2)}, index=df.index)], axis=1)
    if st and "dec" in st:
        X = pd.concat([X, decomposed(df, X, st["dec"])], axis=1)
    if st and "knots" in st:  # a missing x leaves its hinges missing, filled with their training means like any input
        X = pd.concat([X, pd.DataFrame({f"h_{c}_{i}": np.maximum(X[c] - q, 0) for c, k in st["knots"].items() for i, q in enumerate(k)},
                                       index=df.index)], axis=1)
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
    if any(c in DEC_FEATS for c in feats):
        st["dec"] = fit_decomposed(tr)
    if any(c in HINGE_FEATS for c in feats):
        X = prep(tr, [c for c in feats if c not in HINGE_FEATS], mu, st)
        st["knots"] = {c: X[c].quantile(HINGE_Q).values for c in HINGE_IN}
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
        y = tr[f"y_{c}"]
        if c == "goals" and any(f in LUCK_FEATS for f in feats):  # the Week ridge (findings 320 (4))
            y = (GOAL_BLEND * tr.y_goals + (1 - GOAL_BLEND) * tr.y_ixg).fillna(tr.y_goals)
        m = RidgeCV(alphas=np.logspace(-1, 5, 13)).fit(Z, y, sample_weight=tr.y_gp)
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


def weekly_rho(d, col):
    return d.groupby("monday").apply(lambda g: g[col].rank().corr(g.y_fp.rank()) if len(g) >= 20 else np.nan,
                                     include_groups=False)


def skater_backtest(df):
    """Rolling origin: each test season predicted by models fit on earlier seasons. Week: the ridge with the hinge curves
    without and with the luck / role inputs and the smoother goals target, weekly rank correlation. Season: without and with the decomposed inputs, rest-of-season rank
    correlation of max(FP/G + age adjustment, 0) x the shipped Season dress share (research season-rating panel)."""
    parts = []
    for s in TEST_SEASONS:
        tr, te = df[df.season < s], df[df.season == s].copy()
        mu = ux_means(tr)
        te["before"] = predict(te, *fit_linear(tr, FEATS, mu), FEATS, mu)
        te["hinge"] = predict(te, *fit_linear(tr, FEATS + HINGE_FEATS, mu), FEATS + HINGE_FEATS, mu)
        te["after"] = predict(te, *fit_linear(tr, WEEK_FEATS, mu), WEEK_FEATS, mu)
        te["season_fpg"] = predict(te, *fit_linear(tr, SEASON_FEATS, mu), SEASON_FEATS, mu)
        parts.append(te)
    t = pd.concat(parts)
    w = t[(t.y_gp > 0) & (t.std_gp >= 3)].copy()
    first = w.groupby("season").monday.min()
    w["early"] = (w.monday - w.season.map(first)).dt.days // 7 < 6
    for pool, d in (("all", w), ("waiver", w[w.wire]), ("first 6 weeks", w[w.early]), ("first 6 weeks waiver", w[w.early & w.wire])):
        b, a = weekly_rho(d, "hinge"), weekly_rho(d, "after")
        gain = (a - b).dropna()
        print(f"skaters {pool:21s} rho {b.mean():.4f} -> {a.mean():.4f}  gain {gain.mean():+.4f} ± {gain.std() / len(gain) ** .5:.4f}")
    panel = os.path.join(os.path.dirname(RESEARCH.rstrip("/")), "season-rating", "data", "ablate_now.parquet")
    if not os.path.exists(panel):
        return
    ab = pd.read_parquet(panel)[["playerId", "monday", "d_site_friendly+mk", "y_ros_tg", "r_team_games", "age"]]
    r = t.merge(ab.rename(columns={"d_site_friendly+mk": "dress"}), on=["playerId", "monday"])
    r = r[(r.r_team_games >= 10) & r.dress.notna() & r.y_ros_tg.notna()]
    adj = 0.01 * np.maximum(0, 24 - r.age.fillna(27)) - 0.009 * np.maximum(0, r.age.fillna(27) - 29)
    def ros(col):
        v = r.assign(v=np.maximum(r[col] + adj, 0) * r.dress)
        return v.groupby(["season", "monday"]).apply(lambda g: g.v.rank().corr(g.y_ros_tg.rank()), include_groups=False)
    b, a = ros("before"), ros("season_fpg")
    gain = (a - b).dropna()
    by = gain.groupby(level=0).mean().round(4).tolist()
    print(f"Season rest-of-season rho {b.mean():.4f} -> {a.mean():.4f}  gain {gain.mean():+.4f} ± {gain.std() / len(gain) ** .5:.4f} by season {by}")


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
    coef, icpt, means, sk_st = fit_linear(df, WEEK_FEATS, mu)
    s_coef, s_icpt, s_means, s_st = fit_linear(df, SEASON_FEATS, mu)  # the Season rating's ridge: no hinges, decomposed inputs
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
        "skater": {"features": WEEK_FEATS, "coef": [round(float(x), 8) for x in coef], "intercept": round(float(icpt), 8),
                   "fill": {k: round(float(v), 8) for k, v in means.items()},
                   "season": {"features": SEASON_FEATS, "coef": [round(float(x), 8) for x in s_coef], "intercept": round(float(s_icpt), 8),
                              "fill": {k: round(float(v), 8) for k, v in s_means.items()}, **decomposed_json(s_st["dec"])},
                   "hinges": {c: [round(float(q), 6) for q in k] for c, k in sk_st["knots"].items()},
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
    check_daily_arithmetic(df, coef, icpt, means, sk_st, mu, WEEK_FEATS)
    check_daily_arithmetic(df, s_coef, s_icpt, s_means, s_st, mu, SEASON_FEATS, season=True)


def decomposed_json(st):
    r6 = lambda v: round(float(v), 8)
    lin = lambda m: {"inputs": list(m["inputs"]), "coef": [r6(x) for x in m["coef"]], "intercept": r6(m["intercept"]),
                     "fill": {k: r6(v) for k, v in m["fill"].items()}}
    return {"rates": {c: lin(m) | {"lo": {k: r6(v) for k, v in m["lo"].items()}, "hi": {k: r6(v) for k, v in m["hi"].items()}}
                      for c, m in st["rates"].items()},
            "ipp_mu": {p: {k: r6(v) for k, v in m.items()} for p, m in st["ipp_mu"].items()},
            "sh_forecast": lin(st["sh"]) | {"inputs": st["sh"]["inputs"] + FC_EXTRA}}


def check_daily_arithmetic(df, coef, icpt, means, st, mu, feats, season=False, n=2000):
    """scripts/rating.py rebuilds the forecast and product inputs itself: its FP/G must match this script's on the
    training rows, or the daily ratings would silently differ from the fitted model."""
    sys.path.insert(0, os.path.join(os.path.dirname(OUT), "..", "scripts"))
    import rating
    rows = df.sample(min(n, len(df)), random_state=0)
    want = np.maximum(predict(rows, coef, icpt, means, st, feats, mu), 0)
    base = prep(rows, UX_ERA_FEATS + P2_FEATS, mu)
    m = rating.Model()
    got = []
    for (_, r), (_, b) in zip(rows.iterrows(), base.iterrows()):
        f = {k: (None if pd.isna(v) else float(v)) for k, v in r.items() if isinstance(v, (int, float, np.number))}
        f |= {k: (None if pd.isna(v) else float(v)) for k, v in b.items()}
        got.append(m.fpg(f, season=season))
    gap = np.abs(np.array(got) - want).max()
    print(f"daily arithmetic check ({'Season' if season else 'Week'}): largest FP/G gap {gap:.2e} over {len(rows)} rows")
    assert gap < 1e-3, "scripts/rating.py and model/train.py disagree on the skater inputs"


if __name__ == "__main__":
    main()
