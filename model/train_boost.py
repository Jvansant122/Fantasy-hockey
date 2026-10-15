"""Handoff 332 (findings 360 (3)-(4)): train the two boosted models for the Season rating on 16 seasons of game logs
(the research's hunt11b compact panel, 2010-11 .. 2025-26, plus MoneyPuck last-season rates) and export them to
model/season_boost_*.json, which scripts/season_boost.py evaluates in plain Python. Offline only: needs pandas,
scikit-learn and the research data in /mnt/project-files; the daily run only reads the JSON.

Rebuild the panel first with the research scripts (hunt11b/compact.py, mp_prev.py, fetch_bios.py), then:
    python model/train_boost.py [out_dir] [--pickle] [--models=dress,fpg,week]

  dress: rest-of-season dress share (x22 "boost (no news)"), seasons >= 2011-12, target y, weight tleft, features FD;
         each season's first two weeks added (handoff 414).
  fpg:   rest-of-season FP per game played (x19 "components + MoneyPuck"), rows with y_gp >= 1, target y_fp / y_gp, weight y_gp,
         features F + MP, compact merged with mp_prev on playerId + season.

  week:  next Monday-Sunday dress share (handoffs 411, 413, 414; ideas 1142, 1162, 1168; hunt13/p_boost_wk_news.py, early1.py):
         games he played that week / his team's games that week, weight = team games, features FW = FD (exp clipped at 5 as
         the site builds it) + the latest Daily Faceoff injury item's flags (news_inputs, its timeline read by the site's
         leads.news_timeline, handoff 439), all 16 seasons plus early weeks,
         HistGBM up to 400 x 0.05 (sklearn's default early stopping, as the research ran it), 31 leaves, 200 rows a leaf. Needs hunt13/data/wk_targets.parquet and hunt13/feats.py.
         Exported to season_boost_week.json.

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
R = "/mnt/project-files/research/claude-rating"
WEEK_SET = dict(max_iter=400, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200, random_state=0)
SET = dict(max_iter=400, learning_rate=0.04, max_leaf_nodes=31, min_samples_leaf=300, l2_regularization=1.0, early_stopping=False, random_state=0)


def sig7(v):
    return float(f"{v:.7g}")


def export(m, feats):
    assert m.n_trees_per_iteration_ == 1 and len(m._predictors) == m.n_iter_
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


NW = ["season", "surgery", "ir", "ltir", "wtw", "dtd", "ret", "out", "practice", "gtd", "illness", "concussion"]
FW = FD + [f"inj_{k}" for k in NW] + ["inj_after", "inj_age", "inj_rem"]


def news_inputs(c):
    """Handoff 413 (hunt11b/feats_news_compact.py): per row, the latest Daily Faceoff injury item before Monday 09:00 UTC as
    hunt11b/news.py parses it; its flags count only when it came after his last game (posted after 03:00 UTC that day)."""
    sys.path.insert(0, f"{R}/hunt11b")
    import news  # research parser for the 2011-26 Daily Faceoff archive
    # the stated timeline as the site reads it (handoff 439: leads.news_timeline, which skips stat lines since handoff 428)
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"))
    import leads
    research_parse = news.parse

    def parse(txt):
        out, tl = research_parse(txt), leads.news_timeline(txt or "")
        out["tl_days"] = np.nan if tl is None else tl
        return out
    news.parse = parse
    a = pd.read_parquet(f"{R}/data/skater_games.parquet", columns=["playerId", "gameDate"])
    b = pd.read_parquet(f"{H}/skater_games_old.parquet", columns=["playerId", "gameDate"])
    g = pd.concat([a, b]); g["gameDate"] = pd.to_datetime(g.gameDate); g = g.sort_values("gameDate")
    k = c[["playerId", "monday"]].drop_duplicates().sort_values("monday")
    k = pd.merge_asof(k, g.rename(columns={"gameDate": "last_game"}), left_on="monday", right_on="last_game", by="playerId",
                      direction="backward", allow_exact_matches=False)
    n = pd.concat([news.load(("injuries",)), news.load(("injuries_old",))]).drop_duplicates("id").sort_values("ts")
    q = n[["playerId", "ts"] + NW + ["tl_days"]].rename(columns={x: "inj_" + x for x in NW + ["tl_days"]})
    k["cut"] = k.monday + pd.Timedelta(hours=9)
    k = pd.merge_asof(k.sort_values("cut"), q, left_on="cut", right_on="ts", by="playerId", direction="backward")
    k["inj_after"] = ((k.ts > k.last_game + pd.Timedelta(hours=3)) & k.ts.notna()).astype(float)
    age = (k.cut - k.ts).dt.total_seconds() / 86400
    for x in NW:
        k["inj_" + x] = k["inj_" + x].fillna(False).astype(float) * k.inj_after
    k["inj_rem"] = np.where(k.inj_after == 1, np.clip(k.inj_tl_days - age, 0, 250), np.nan)
    k["inj_age"] = np.where(k.inj_after == 1, age, np.nan)
    return c.merge(k[["playerId", "monday"] + FW[len(FD):]], on=["playerId", "monday"], how="left")


def week_model(c):
    """Handoffs 411, 413, 414: the next-week dress share model on every compact-panel row (and each season's first two
    weeks, hunt13/compact_early.py) whose team plays that week, with the injury-news inputs (hunt13/early1.py targets)."""
    sys.path.insert(0, f"{R}/hunt13")
    import feats  # research helper: the 16 seasons' game logs and team schedules
    c = c.copy(); c["monday"] = pd.to_datetime(c.monday)
    g = feats.games_all(("playerId", "gameDate", "teamAbbrev", "season")); ts = feats.team_sched()
    mons = np.sort(c.monday.unique())

    def wk(df, key):
        d = df.copy(); d["monday"] = mons[np.clip(np.searchsorted(mons, d.gameDate.values, side="right") - 1, 0, None)]
        d = d[(d.gameDate - d.monday).dt.days.between(0, 6)]
        return d.groupby(key + ["monday"]).size()
    c = c.merge(wk(g, ["playerId"]).rename("w_gp").reset_index(), on=["playerId", "monday"], how="left")
    c = c.merge(wk(ts, ["teamAbbrev"]).rename("w_tg").reset_index().rename(columns={"teamAbbrev": "team"}), on=["team", "monday"], how="left")
    c["w_gp"] = c.w_gp.fillna(0); c = c[c.w_tg > 0]; c["exp"] = c.exp.clip(upper=5)
    c = news_inputs(c)
    y = (c.w_gp / c.w_tg).clip(0, 1)
    return HistGradientBoostingRegressor(**WEEK_SET).fit(c[FW].astype(float).values, y.values, sample_weight=c.w_tg.values), len(c)


def save(name, m, feats):
    js = export(m, feats)
    with open(f"{OUT}/season_boost_{name}.json", "w") as fh:
        json.dump(js, fh, separators=(",", ":"), allow_nan=False)
    if "--pickle" in sys.argv:  # sklearn copies, only for checking the JSON against .predict
        with open(f"{OUT}/model_{name}.pkl", "wb") as fh:
            pickle.dump(m, fh)
    print(name, "baseline", js["baseline"], "trees", js["n_trees"], "nodes", sum(len(t[0]) for t in js["trees"]))


def main():
    only = next((a.split("=", 1)[1].split(",") for a in sys.argv[1:] if a.startswith("--models=")), ["dress", "fpg", "week"])
    late = pd.read_parquet(f"{H}/compact.parquet")
    # dress and week also train on each season's first two weeks (handoff 414, hunt13/compact_early.py): the site applies
    # them from game 1; FP/G stays on the later Mondays
    both = pd.concat([late, pd.read_parquet(f"{R}/hunt13/data/compact_early.parquet")], ignore_index=True)
    if "dress" in only:  # (x22): exp clipped at 5, seasons 2011-12 on
        cd = both.copy(); cd["exp"] = cd.exp.clip(upper=5); cd = cd[cd.season >= 20112012]
        t0 = time.time()
        save("dress", HistGradientBoostingRegressor(**SET).fit(cd[FD].astype(float).values, cd.y.values, sample_weight=cd.tleft.values), FD)
        print(f"dress: {len(cd)} rows, {time.time() - t0:.1f}s")
    if "fpg" in only:  # (x19): compact + MoneyPuck last season, rows with y_gp >= 1 (x19 does not clip exp; exp is not in F)
        cf = late.merge(pd.read_parquet(f"{H}/mp_prev.parquet"), on=["playerId", "season"], how="left"); cf = cf[cf.y_gp >= 1]
        t0 = time.time()
        save("fpg", HistGradientBoostingRegressor(**SET).fit(cf[FF].astype(float).values, (cf.y_fp / cf.y_gp).values, sample_weight=cf.y_gp.values), FF)
        print(f"fpg: {len(cf)} rows, {time.time() - t0:.1f}s")
    if "week" in only:
        t0 = time.time()
        mw, n = week_model(both)
        save("week", mw, FW)
        print(f"week: {n} rows, {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
