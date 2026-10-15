"""model/rating_model.json is well formed and Model gives sane outputs."""
import math

import pytest

import rating


@pytest.fixture(scope="module")
def model():
    return rating.Model()


def test_weights_line_up(model):
    assert len(model.sk["features"]) == len(model.sk["coef"])
    assert set(model.sk["features"]) <= set(model.sk["fill"])
    assert len(model.g["features"]) == len(model.g["coef"])
    assert set(model.sk["replacement_fpg"]) == {"F", "D"}
    # the Season rating's ridge: no hinge curves, which only the Week rating uses (findings 320 (2))
    season = model.sk["season"]
    assert len(season["features"]) == len(season["coef"]) and set(season["features"]) <= set(season["fill"])
    assert not any(n.startswith("h_") for n in season["features"])
    hinges = [n for n in model.sk["features"] if n.startswith("h_")]
    assert len(hinges) == sum(len(k) for k in model.sk["hinges"].values()) == 60
    assert 1 < model.g["points_per_start"] < 5


def test_fpg_uses_fill_for_missing_and_is_never_negative(model):
    avg = model.fpg({})
    assert 0 <= avg < 5
    low = rating.Model()
    low.sk = dict(model.sk, intercept=-1e6, season=dict(model.sk["season"], intercept=-1e6))  # clamped at 0, not negative
    assert low.fpg({}) == 0.0 and low.fpg({}, season=True) == 0.0
    assert 0 <= model.fpg({}, season=True) < 5


def test_hinges_bend_the_week_rating_only(model):
    """A strong scorer: the hinge inputs are past their knots in the Week ridge and absent from the Season ridge."""
    f = {"std_fp": 3.5, "prev_fp": 3.3, "sx_fp": 3.4, "sx_ux": 3.0, "l5_toi": 21.0, "std_shots": 3.5, "std_gp": 20}
    out = rating.forecast_features(f, model.sk)
    knots = model.sk["hinges"]["std_fp"]
    assert out["h_std_fp_0"] == pytest.approx(3.5 - knots[0]) and all(out[f"h_std_fp_{i}"] >= 0 for i in range(5))
    assert rating.forecast_features({}, model.sk)["h_std_fp_0"] is None  # missing input: its hinges take their fills
    assert model.fpg(f) != model.fpg(f, season=True)


def test_dress_is_a_probability(model):
    for d3 in (0, 1 / 3, 2 / 3, 1):
        for d10 in (0, 0.3, 0.6, 0.9, 1):
            assert 0 <= model.dress(d3, d10) <= 1
    assert model.dress(1, 1) > model.dress(0, 0)


def test_p_start_is_a_probability(model):
    feats = {n: 0.5 for n in model.g["features"]}
    p = model.p_start(feats)
    assert 0 < p < 1 and not math.isnan(p)


def test_week_forecast_ridges_are_wider_than_the_seasons(model):
    """Handoff 669: the Week's TOI / PP TOI forecast ridges add squared and D x ice time and more windows; the Season ridge
    keeps the narrower ones it was fit with."""
    week, season = model.sk["toi_forecast"], model.sk["season"]["toi_forecast"]
    for tgt in ("y_toi", "y_pptoi"):
        w, s = week[tgt]["inputs"], season[tgt]["inputs"]
        assert set(s) < set(w) and {"std_toi_sq", "D_std_toi", "age_m", "sh_std"} <= set(w) and len(w) == len(week[tgt]["coef"])
        assert not any(k.endswith("_sq") for k in s) and len(s) == len(season[tgt]["coef"])
    f = {"is_D": 1.0, "std_gp": 20, "prev_gp": 80, "no_prev": 0.0, "std_toi": 23.0, "l5_toi": 24.0, "l10_toi": 23.5,
         "last_toi": 25.0, "std_pptoi": 2.5, "l5_pptoi": 2.6, "pp_l1": 2.8}
    wk, se = rating.forecast_features(f, model.sk), rating.forecast_features(f, model.sk, season=True)
    assert 18 < wk["fc_toi"] < 28 and 18 < se["fc_toi"] < 28 and wk["fc_toi"] != se["fc_toi"]
    # a missing age reads 27 in the wide ridges, as trained
    assert rating.forecast_features(dict(f, age_m=27.0), model.sk)["fc_toi"] == pytest.approx(wk["fc_toi"])


def test_boosted_ice_time_forecasts_load_and_feed_the_season_only(model):
    """Handoff 668: the boosted next-week TOI/G and PP TOI/G (model/season_boost_toi.json, _pptoi.json) give sane minutes
    and replace the linear forecasts in the Season ridge only."""
    import season_boost
    assert model.toi_boost and set(model.toi_boost) == {"toi", "pptoi"}
    for m in model.toi_boost.values():
        assert 50 <= len(m["trees"]) <= 400 and {"fc_toi", "fc_pptoi", "team_games"} <= set(m["features"])
        assert not any(n.startswith("h_") or "*" in n for n in m["features"])
    top = {"std_gp": 20, "is_D": 0.0, "no_prev": 0.0, "prev_gp": 80, "team_games": 4}
    for w in ("l5", "l10", "l20", "std", "prev"):
        top |= {f"{w}_toi": 21.0, f"{w}_pptoi": 3.2, f"{w}_fp": 2.6, f"{w}_shots": 3.5}
    top |= {"last_toi": 21.5, "toi_l3": 21.2, "pp_l1": 3.3, "pp_l3": 3.2}
    depth = dict(top)
    for w in ("l5", "l10", "l20", "std", "prev"):
        depth |= {f"{w}_toi": 11.0, f"{w}_pptoi": 0.1, f"{w}_fp": 0.9, f"{w}_shots": 1.2}
    depth |= {"last_toi": 10.5, "toi_l3": 10.8, "pp_l1": 0.0, "pp_l3": 0.1}
    x_top = {**top, **rating.forecast_features(top, model.sk)}
    x_depth = {**depth, **rating.forecast_features(depth, model.sk)}
    toi_top, toi_depth = (season_boost.predict(model.toi_boost["toi"], x) for x in (x_top, x_depth))
    pp_top, pp_depth = (season_boost.predict(model.toi_boost["pptoi"], x) for x in (x_top, x_depth))
    assert 17 < toi_top < 25 and 7 < toi_depth < 15 and toi_top > toi_depth + 5
    assert 1.5 < pp_top < 4.5 and -0.2 < pp_depth < 1.0
    assert 5 < season_boost.predict(model.toi_boost["toi"], {}) < 25
    # the Season's inputs carry the boosted minutes; the Week's keep the linear ones
    boosted = rating.forecast_features(top, model.sk, model.toi_boost)
    assert boosted["fc_toi"] == pytest.approx(toi_top) and boosted["fc_pptoi"] == pytest.approx(pp_top)
    assert x_top["fc_toi"] != pytest.approx(toi_top)
    plain = rating.Model()
    plain.toi_boost = None
    assert model.fpg(top) == plain.fpg(top) and model.fpg(top, season=True) != plain.fpg(top, season=True)
