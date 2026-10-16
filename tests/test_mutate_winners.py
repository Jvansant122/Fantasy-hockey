"""Hunt 14 mutate stream: pins for the Predicted winners spreads, tonight after puck drop, and depth dress
(copy into tests/)."""
import pytest

import winners


def test_depth_dress_by_the_team_games_he_played():
    """A skater the site doesn't rate dresses DEPTH_DRESS[0] after playing the team's last game, [1] after one of its
    last 5, [2] otherwise, and the three run downward. Kills: W06."""
    team = [1, 2, 3, 4, 5, 6]
    assert winners.DEPTH_DRESS[0] > winners.DEPTH_DRESS[1] > winners.DEPTH_DRESS[2] > 0
    assert winners.depth_dress([{"game": 6}], team) == winners.DEPTH_DRESS[0]
    assert winners.depth_dress([{"game": 3}], team) == winners.DEPTH_DRESS[1]
    assert winners.depth_dress([{"game": 1}], team) == winners.DEPTH_DRESS[2]


def test_tonight_drops_out_once_his_game_starts():
    """A player whose game tonight has started has nothing left tonight in the matchup projection. Kills: W09."""
    p = {"pos": "C", "cr": 4.0, "cr_fpg": 2.0, "cr_dress": 1.0, "nights": [16, 17]}
    assert set(winners.night_values(p, 16, False)) == {16, 17}
    assert set(winners.night_values(p, 16, True)) == {17}


def test_night_variance_uses_the_shipped_spreads():
    """Skater sd = SKATER_SD[0] + SKATER_SD[1] x FP a dressed game (both > 0); a goalie's start adds GOALIE_SD^2.
    Kills: W11, W12."""
    assert winners.SKATER_SD[0] > 0 and winners.SKATER_SD[1] > 0 and winners.GOALIE_SD > 2
    sk = {"pos": "C", "cr": 2.0, "cr_fpg": 2.0, "cr_dress": 1.0, "nights": [16]}
    assert winners.night_values(sk, 16, False)[16][1] == pytest.approx((winners.SKATER_SD[0] + 2.0 * winners.SKATER_SD[1]) ** 2)
    assert winners.night_values(sk, 16, False)[16][1] == pytest.approx(1.4 ** 2, abs=0.2)  # 0.4 + 0.5 x 2 (findings 451)
    g = {"pos": "G", "cr": 3.0, "cr_games": 1.0, "cr_g_nights": {"16": 1.0}, "nights": [16]}
    assert winners.night_values(g, 16, False)[16][1] == pytest.approx(10.0, abs=1.5)  # 3.33^2 for a sure start
