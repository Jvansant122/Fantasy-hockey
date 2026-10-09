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
    assert 1 < model.g["points_per_start"] < 5


def test_fpg_uses_fill_for_missing_and_is_never_negative(model):
    avg = model.fpg({})
    assert 0 <= avg < 5
    assert model.fpg({n: -100.0 for n in model.sk["features"]}) == 0.0


def test_dress_is_a_probability(model):
    for d3 in (0, 1 / 3, 2 / 3, 1):
        for d10 in (0, 0.3, 0.6, 0.9, 1):
            assert 0 <= model.dress(d3, d10) <= 1
    assert model.dress(1, 1) > model.dress(0, 0)


def test_p_start_is_a_probability(model):
    feats = {n: 0.5 for n in model.g["features"]}
    p = model.p_start(feats)
    assert 0 < p < 1 and not math.isnan(p)
