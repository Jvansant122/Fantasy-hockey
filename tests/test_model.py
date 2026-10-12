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
